"""板块排名的交互回归：切换口径、行情增量重排、缺值与无归属板块。"""
import copy
import csv
import io
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analysis.short_term_screen import build_report, rank_sectors

url=sys.argv[1] if len(sys.argv)>1 else "http://127.0.0.1:7116"
day="2026-11-04"
inputs={"radar":{"fallback_date":day,"stocks":[]},"sectors":{"as_of":day,"rows":[]}}
for i in range(24):
    inputs["radar"]["stocks"].append({"code":str(600100+i),"name":f"标的{i}","industry":f"板块{i:02}","activity":100-i,"change_pct":1,"main_net_inflow":None if i==23 else (i-1)*1e6})
    inputs["sectors"]["rows"].append({"code":f"BK{i:04}","name":f"板块{i:02}","type":"行业","pct":1,"net":None if i==23 else (i-1)*1e6})
for i in range(40):
    inputs["radar"]["stocks"].append({"code":str(601000+i),"name":f"大板块成分{i}","industry":"大而冷的板块","activity":1,"change_pct":0,"main_net_inflow":1e9})
inputs["sectors"]["rows"].append({"code":"BKFLOW","name":"仅有资金报价","type":"行业","net":1e8,"pct":2})
inputs["sectors"]["rows"].append({"code":"BKCONCEPT","name":"大额重叠概念","type":"概念","net":1e12,"pct":2})
initial=build_report(inputs,day,day+"T10:00:00+08:00")
updated_inputs=copy.deepcopy(inputs)
for row in updated_inputs["radar"]["stocks"]:
    if row["industry"]=="大而冷的板块":row["activity"]=110
radar_update=build_report(updated_inputs,day,day+"T10:01:00+08:00")
flow_update=copy.deepcopy(radar_update)
new_leader=next(row for row in flow_update["sectors"] if row["name"]=="板块23")
new_leader["quote"]["net"]=2e8
new_leader["quote"]["net_at"]=day+"T10:02:00+08:00"


def list_ids(page):
    return page.locator("#stSectorList .st-scroll-content > button").evaluate_all("nodes=>nodes.map(n=>n.dataset.stId)")


def node_ids(page):
    return page.locator("#stNodes .st-node-sector").evaluate_all("nodes=>nodes.map(n=>n.dataset.stNode)")


with sync_playwright() as p:
    browser=p.chromium.launch(channel="chrome",headless=True,args=["--enable-unsafe-swiftshader"])
    for fallback in (False,True):
        page=browser.new_page(viewport={"width":1920,"height":1080},accept_downloads=True)
        errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
        page.add_init_script("window.__rankingCallbacks={};const original=setTimeout;window.setTimeout=(fn,delay,...args)=>{if([1600,4200,7800,11500,17300].includes(delay)){window.__rankingCallbacks[delay]=()=>fn(...args);return original(()=>{},delay);}return original(fn,delay,...args);};")
        if fallback:page.add_init_script("const original=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(kind,...args){return String(kind).includes('webgl')?null:original.call(this,kind,...args);};")
        page.route("**/api/short-term/overview*",lambda route:route.fulfill(json=initial))
        page.route("**/api/short-term/orders*",lambda route:route.fulfill(json={"big_buys":[],"big_sells":[],"order_meta":{}}))
        def feed(route):
            kind=parse_qs(urlsplit(route.request.url).query)["kind"][0]
            report=radar_update if kind=="radar" else flow_update
            payload={"sectors":report["sectors"]}
            if kind=="radar":payload["stocks"]=report["stocks"]
            route.fulfill(json={"changed":True,"running":False,"revision":1 if kind=="radar" else 2,"version":kind,"payload":payload})
        page.route("**/api/short-term/feed*",feed)
        page.goto(url+"/desktop/short_term",wait_until="domcontentloaded")
        expect(page.locator("#stScreen")).to_have_attribute("aria-busy","false",timeout=60000)
        page.locator("#stSectorSort").select_option("heat")
        assert list_ids(page)==[r["id"] for r in initial["sectors"] if r.get("trading_heat") is not None][:18]
        assert node_ids(page)==[r["id"] for r in initial["sectors"] if r.get("trading_heat") is not None][:12]
        expect(page.locator(".st-sector-panel .st-feed-clock")).to_have_text("1分")
        assert page.locator(".st-workspace").bounding_box()["y"]<=55

        page.locator("#stSectorSort").select_option("flow")
        expect(page.locator("#stSceneSort")).to_have_value("flow")
        funds=rank_sectors([r for r in initial["sectors"] if r["type"]=="行业" and r["quote"]["net_label"]=="板块净额" and r["quote"]["net"] is not None],"flow")
        assert list_ids(page)==[r["id"] for r in funds[:18]]
        assert node_ids(page)==[r["id"] for r in funds[:12]],"缺少股票归属不得隐藏资金榜首"
        expect(page.locator(".st-sector-panel .st-feed-clock")).to_have_text("2分")
        page.locator("#stSectorSort").select_option("concept_flow")
        expect(page.locator("#stNodes .st-node-sector").first).to_contain_text("大额重叠概念")
        page.locator("#stSectorSort").select_option("component_flow")
        expect(page.locator("#stNodes .st-node-sector").first).to_contain_text("大而冷的板块")

        page.locator("#stSceneSort").select_option("heat")
        expect(page.locator("#stSectorSort")).to_have_value("heat")
        assert list_ids(page)==[r["id"] for r in initial["sectors"] if r.get("trading_heat") is not None][:18]
        news=page.locator("#stNews").inner_html()
        buys=page.locator("#stBuyList").inner_html()
        page.evaluate("()=>window.__rankingCallbacks[1600]()")
        assert list_ids(page)==[r["id"] for r in radar_update["sectors"] if r.get("trading_heat") is not None][:18]
        assert node_ids(page)==[r["id"] for r in radar_update["sectors"] if r.get("trading_heat") is not None][:12]
        expect(page.locator("#stNodes .st-node-sector").first).to_contain_text("大而冷的板块")
        expect(page.locator("#stNodes .st-node-sector").first).to_contain_text("#1 热度110")
        assert page.locator("#stNews").inner_html()==news and page.locator("#stBuyList").inner_html()==buys

        page.locator("#stSectorSort").select_option("flow")
        page.evaluate("()=>window.__rankingCallbacks[11500]()")
        funds=rank_sectors([r for r in flow_update["sectors"] if r["type"]=="行业" and r["quote"]["net_label"]=="板块净额" and r["quote"]["net"] is not None],"flow")
        assert list_ids(page)==[r["id"] for r in funds[:18]]
        assert node_ids(page)==[r["id"] for r in funds[:12]]
        expect(page.locator("#stNodes .st-node-sector").first).to_contain_text("板块23")
        expect(page.locator("#stNodes .st-node-sector").first).to_contain_text("#1 +2亿")
        page.locator('[data-st-list="sectors"]').click()
        expect(page.locator('.st-detail-table tbody tr').first).to_contain_text("板块23")
        with page.expect_download() as download:page.locator('[data-st-export-list]').click()
        exported=list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text(encoding="utf-8-sig"))))
        assert exported[0]["name"]=="板块23" and exported[0]["rank"]=="1"
        assert float(exported[0]["display_net"])==2e8 and float(exported[0]["trading_heat"])==77
        page.locator('#stDrawerClose').click()
        page.locator('#stNodes .st-node-sector').first.click()
        expect(page.locator('#stDrawerBody')).to_contain_text("交易热度")
        expect(page.locator('#stDrawerBody')).to_contain_text("有效成分 1只")
        assert not errors,errors
        page.close()
        print(json.dumps({"ranking":"passed","canvas":fallback,"checks":["heat","flow","controls_synced","radar_reorders","funds_reorder","other_panels_unchanged"],"errors":errors}),flush=True)
    browser.close()
