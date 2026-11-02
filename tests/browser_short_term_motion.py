"""用实际场景绘制次数验收流星连续性；可选传入优化前脚本做同机对比。"""
import argparse
import json
from pathlib import Path
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright, expect


parser=argparse.ArgumentParser()
parser.add_argument("url",nargs="?",default="http://127.0.0.1:7116")
parser.add_argument("--baseline-scene")
parser.add_argument("--baseline-fallback")
parser.add_argument("--canvas-only",action="store_true")
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
snapshot=json.load(build_opener(ProxyHandler({})).open(args.url+"/api/short-term/overview",timeout=30))
assert snapshot["stocks"] and snapshot["sectors"]
snapshot["job"]=None


def instrument(source, fallback=False):
    name="LumoShortTermSceneFallback" if fallback else "LumoShortTermScene"
    method="draw" if fallback else "render"
    replacement=f"""window.{name}=class extends ShortTermScene {{
      constructor(...args){{super(...args);window.__motionScene=this;}}
      {method}(...args){{const start=performance.now();super.{method}(...args);if(window.__measureMotion)window.__drawTimes.push([performance.now(),performance.now()-start]);}}
    }};"""
    return source.replace("window."+name+"=ShortTermScene;",replacement)


def sample(page):
    version=page.evaluate("()=>window.__motionScene.packets?.geometry.attributes.position.version??null")
    page.evaluate("""() => {window.__drawTimes=[];window.__nativeTimes=[];window.__measureMotion=true;
      function frame(t){if(!window.__measureMotion)return;window.__nativeTimes.push(t);requestAnimationFrame(frame);}requestAnimationFrame(frame);}""")
    page.wait_for_timeout(3500)
    data=page.evaluate("()=>{window.__measureMotion=false;return {draws:window.__drawTimes,native:window.__nativeTimes,version:window.__motionScene.packets?.geometry.attributes.position.version??null};}")
    draws=[row[0] for row in data["draws"]]
    gaps=[b-a for a,b in zip(draws,draws[1:])]
    assert len(draws)>5
    return {"fps":round((len(draws)-1)*1000/(draws[-1]-draws[0]),1),
            "native_fps":round((len(data["native"])-1)*1000/(data["native"][-1]-data["native"][0]),1),
            "render_ratio":round(len(draws)/max(1,len(data["native"])),3),
            "frame_p95_ms":round(sorted(gaps)[int(len(gaps)*.95)],2),
            "cpu_p95_ms":round(sorted(row[1] for row in data["draws"])[int(len(draws)*.95)],2),
            "position_uploads":None if version is None else data["version"]-version}


results=[]
with sync_playwright() as p:
    browser=p.chromium.launch(channel="chrome",headless=True,args=["--enable-unsafe-swiftshader"])
    variants=[("current",False),("current",True)]
    if args.baseline_scene:variants.insert(0,("baseline",False))
    if args.baseline_fallback:variants.insert(2,("baseline",True))
    if args.canvas_only:variants=[variant for variant in variants if variant[1]]
    for label,fallback in variants:
        scene=Path(args.baseline_scene).read_text() if label=="baseline" and not fallback else (root/"webui/static/lumo_short_term_scene.js").read_text()
        canvas=Path(args.baseline_fallback).read_text() if label=="baseline" and fallback else (root/"webui/static/lumo_short_term_scene_fallback.js").read_text()
        page=browser.new_page(viewport={"width":1920,"height":1080})
        errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        page.on("console",lambda message:errors.append(message.text) if "Shader Error" in message.text or "INVALID_OPERATION" in message.text else None)
        if fallback:page.add_init_script("const original=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(kind,...args){return String(kind).includes('webgl')?null:original.call(this,kind,...args);};")
        page.route("**/lumo_short_term_scene.js*",lambda route:route.fulfill(body=instrument(scene),content_type="text/javascript"))
        page.route("**/lumo_short_term_scene_fallback.js*",lambda route:route.fulfill(body=instrument(canvas,True),content_type="text/javascript"))
        page.route("**/api/short-term/overview*",lambda route:route.fulfill(json=snapshot))
        page.route("**/api/short-term/orders*",lambda route:route.fulfill(json={"big_buys":snapshot.get("big_buys",[]),"big_sells":snapshot.get("big_sells",[]),"refreshing":False}))
        page.route("**/api/short-term/feed*",lambda route:route.fulfill(json={"changed":False,"running":False}))
        page.goto(args.url+"/desktop/short_term?motion-check=1",wait_until="domcontentloaded")
        expect(page.locator("#stScreen")).to_have_attribute("aria-busy","false",timeout=60000)
        page.wait_for_function("window.__motionScene?.labels.length>=30")
        page.wait_for_timeout(1000)
        stats=sample(page)
        if label=="current":
            assert stats["render_ratio"]>.9,stats
            assert page.locator("#stScene").get_attribute("data-flow-engine")=="compositor-timeline"
            tracks=page.evaluate("""()=>{const flow=window.__motionScene.flow;return {count:flow.tracks.length,playing:flow.tracks.every(t=>t.normal.playState==='running'),frames:flow.tracks[0].normal.effect.getKeyframes().length,composite:flow.tracks[0].normal.effect.getKeyframes().every(k=>Object.keys(k).every(key=>['offset','computedOffset','easing','composite','transform','opacity'].includes(key)))};}""")
            assert tracks["count"]>=36 and tracks["playing"] and tracks["frames"]>=90 and tracks["composite"],tracks
            pulse=page.evaluate("""()=>{const scene=window.__motionScene,code=scene.labels.find(row=>row.kind==='stock').row.code,normal=scene.flow.tracks.map(t=>t.normal);scene.pulseUpdates({stockCodes:[code]});return {normalSame:scene.flow.tracks.every((t,i)=>t.normal===normal[i]),eventStarted:scene.flow.tracks.some(t=>t.event!==null)};}""")
            assert pulse["normalSame"] and pulse["eventStarted"],pulse
            if not fallback:
                assert stats["position_uploads"]==0,stats
                versions=page.evaluate("""()=>{const scene=window.__motionScene,attr=scene.packets.geometry.attributes.aPulseAt;
                  const normal=Array.from(attr.array).filter((_,i)=>i%48<24),code=scene.labels.find(row=>row.kind==='stock').row.code;
                  const hits=scene.pulseUpdates({stockCodes:[code]}),after=Array.from(attr.array).filter((_,i)=>i%48<24);return {normalSame:normal.every((value,i)=>value===after[i]),eventStarted:Array.from(attr.array).some((value,i)=>i%48>=24&&value>-10000),hits:hits.length};}""")
                assert versions["normalSame"] and versions["eventStarted"] and versions["hits"]>0,versions
                page.evaluate("data=>{const scene=window.__motionScene,codes=scene.labels.filter(row=>row.kind==='stock').map(row=>row.row.code),set=new Set(codes);data.graph={sector_ids:scene.items.map(row=>row.id),stock_codes:codes,columns:8,edges:scene.items.flatMap(row=>row.stock_codes.filter(code=>set.has(code)).map(code=>({source:row.id,target:'stock:'+code})))};window.__textureBefore=scene.renderer.info.memory.textures;scene.setData(data);scene.setData(data);window.__textureAfter=scene.renderer.info.memory.textures;}",snapshot)
                assert page.evaluate("window.__textureBefore===window.__textureAfter"),"场景重建泄漏曲线纹理"
            page.locator("#stMotion").click();page.wait_for_timeout(100)
            assert page.evaluate("window.__motionScene.flow.tracks.every(t=>t.normal.playState==='paused')")
            page.evaluate("window.__drawTimes=[];window.__measureMotion=true")
            page.wait_for_timeout(400)
            assert page.evaluate("window.__measureMotion=false;window.__drawTimes.length")==0
            page.locator("#stMotion").click()
            page.locator("#stScene").evaluate("el=>el.style.transform='translateX(-10000px)'")
            page.wait_for_function("window.__motionScene.visible===false")
            assert page.evaluate("window.__motionScene.flow.tracks.every(t=>t.normal.playState==='paused')")
            page.evaluate("window.__drawTimes=[];window.__measureMotion=true");page.wait_for_timeout(400)
            assert page.evaluate("window.__measureMotion=false;window.__drawTimes.length")==0
            page.locator("#stScene").evaluate("el=>el.style.transform=''")
            page.wait_for_function("window.__motionScene.visible===true")
            out=Path("/private/tmp/kronos-meteor-smooth");out.mkdir(exist_ok=True)
            page.screenshot(path=str(out/("canvas.png" if fallback else "webgl.png")))
        assert not errors,errors
        result={"version":label,"canvas":fallback,**stats,"errors":errors};results.append(result);print(json.dumps(result),flush=True)
        page.close()
    browser.close()
print(json.dumps({"motion":"passed","checks":results}))
