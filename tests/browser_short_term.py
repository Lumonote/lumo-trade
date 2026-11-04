"""真实聚合结果的短线大屏交互验收，快照只读；支持隔离服务地址。"""
import csv
import io
import json
import re
from pathlib import Path
import sys
from urllib.parse import urlsplit, parse_qs
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright, expect, Error

URL=sys.argv[1] if len(sys.argv)>1 else "http://127.0.0.1:7116"
OUT=Path(__file__).resolve().parents[1]/"results/short_term_preview"
OUT.mkdir(parents=True,exist_ok=True)
opener=build_opener(ProxyHandler({}))
snapshot=json.load(opener.open(URL+"/api/short-term/overview",timeout=30))
assert snapshot["stocks"] and snapshot["sectors"],"需要先更新隔离服务中的真实数据"
orders=json.load(opener.open(URL+"/api/short-term/orders",timeout=30))
orders["refreshing"]=False
calls={"orders":0,"refresh":0,"feeds":{}}
order_update={"inject":False,"added":False}
news_update={"inject":False,"code":""}
sector_update={"inject":False}
# 浏览器检查使用真实快照，定时轮询路由只计数，不重复采集外部来源。
snapshot["job"]=None
snapshot["updated_at"]=snapshot.get("updated_at") or snapshot["generated_at"]

with sync_playwright() as p:
    expect.set_options(timeout=25000)
    try:browser=p.chromium.launch(headless=True,args=["--enable-unsafe-swiftshader"])
    except Error:browser=p.chromium.launch(headless=True,channel="chrome",args=["--enable-unsafe-swiftshader"])
    page=browser.new_page(viewport={"width":1920,"height":1080},accept_downloads=True)
    page.add_init_script("window.__stScheduled=[];window.__stCallbacks={};const originalTimeout=window.setTimeout;window.setTimeout=(fn,delay,...args)=>{window.__stScheduled.push(delay);const id=originalTimeout(fn,delay,...args);if([1600,4200,7800,11500,17300].includes(delay))window.__stCallbacks[delay]=()=>{clearTimeout(id);return fn(...args);};return id;};")
    page.set_default_timeout(25000)
    errors=[]
    page.on("pageerror",lambda e:errors.append(str(e)))
    page.on("console",lambda message:errors.append(message.text) if message.type=="error" and ("Shader Error" in message.text or "VALIDATE_STATUS" in message.text) else None)
    page.route("**/api/short-term/overview*",lambda route:route.fulfill(json=snapshot))
    def order_route(route):
        calls["orders"]+=1
        if order_update["inject"] and not order_update["added"] and orders["big_buys"]:
            # 只用于验收增量插入，线上来源没有此合成事件。
            orders["big_buys"].insert(0,{**orders["big_buys"][0],"id":"browser-scroll-insertion"})
            order_update["added"]=True
        route.fulfill(json=orders)
    def refresh_route(route):calls["refresh"]+=1;route.fulfill(json={"job":{"id":"browser","status":"finished","progress":100}})
    page.route("**/api/short-term/orders*",order_route)
    page.route("**/api/short-term/refresh",refresh_route)
    def feed_route(route):
        kind=parse_qs(urlsplit(route.request.url).query)["kind"][0]
        calls["feeds"][kind]=calls["feeds"].get(kind,0)+1
        if kind=="news" and news_update["inject"]:
            route.fulfill(json={"kind":kind,"version":"test-news","revision":1,"changed":True,"running":False,"payload":{
                "news":[{**snapshot["news"][0],"id":"browser-news-insertion","title":"浏览器分区隔离验收","stock_codes":[news_update["code"]]},*snapshot["news"]],
                "stocks":[{**snapshot["stocks"][0],"name":"其他面板不应同步重绘"},*snapshot["stocks"][1:]],"sectors":snapshot["sectors"]}})
        elif kind=="sectors" and sector_update["inject"]:
            updated=snapshot["sectors"][1]
            changed_sectors=[{**updated,"pct":float(updated.get("pct") or 0)+.1,
                              "trading_heat":max(float(r.get("trading_heat") or 0) for r in snapshot["sectors"])+10,
                              "quote":{**updated["quote"],"pct":float(updated["quote"].get("pct") or 0)+.1}},snapshot["sectors"][0],*snapshot["sectors"][2:]]
            route.fulfill(json={"kind":kind,"version":"test-sectors","revision":2,"changed":True,"running":False,"payload":{"sectors":changed_sectors}})
        else:route.fulfill(json={"kind":kind,"version":"","changed":False,"running":False,"payload":None})
    page.route("**/api/short-term/feed*",feed_route)
    page.goto(URL+"/desktop/short_term",wait_until="networkidle",timeout=60000)
    try:expect(page.locator("#stScreen")).to_have_attribute("aria-busy","false")
    except AssertionError:
        print(json.dumps({"initial_errors":errors,"status":page.locator("#stError").inner_text()},ensure_ascii=False),flush=True)
        page.screenshot(path=str(OUT/"initial-error.png"))
        raise
    print("screen loaded",flush=True)
    assert all(delay in page.evaluate("window.__stScheduled") for delay in (1600,4200,7800,11500,17300)),"首屏须立即错峰连接各来源"
    page.locator("#stSectorSort").select_option("heat")
    heat_sectors=[r for r in snapshot["sectors"] if r.get("trading_heat") is not None]
    page.locator("#stScrollToggle").click()
    assert page.locator("#stStockCount").inner_text()==str(len(snapshot["stocks"]))
    assert page.locator("#stSectorCount").inner_text()==str(len(heat_sectors))
    for row in heat_sectors[:18]:
        quote=row.get("quote")
        assert quote,"板块须保留展示值与统计口径"
        node=page.locator('#stSectorList .st-scroll-content > button').filter(has=page.locator('strong',has_text=row['name'])).first
        assert quote["pct_label"] in node.inner_text() and quote["net_label"] in node.inner_text()
    assert page.locator("#stNodes button").count()>8
    expected_boards=[row for row in snapshot["sectors"] if row["stock_codes"]][:12]
    expect(page.locator("#stNodes .st-node-sector")).to_have_count(len(expected_boards))
    expect(page.locator("#stNodes .st-node-stock")).to_have_count(min(24,len({code for row in expected_boards for code in row["stock_codes"]})))
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--st-bg').trim()") == "#040609"
    assert page.locator(".st-header,.st-brand,.st-session,#stSources,#stExport,.st-controlbar nav").count()==0
    assert page.locator(".st-workspace").bounding_box()["y"]<=55
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight+1")
    # 独立核验 WebGL 场景确实持续重绘，避免只测到列表滚动和 CSS 动画。
    assert page.locator("#stMotion").get_attribute("aria-pressed")=="true"
    scene_before=page.locator("#stScene").screenshot()
    page.wait_for_timeout(700)
    assert page.locator("#stScene").screenshot()!=scene_before,"关联场景没有持续运动"
    assert page.locator("#stScene").evaluate("el=>el.__stDrawCalls")<125
    assert page.locator("#stScene").evaluate("el=>el.__stMeteorCount")>8
    page.locator("#stMotion").click()
    for kind,list_id,count_id in (("limit_up","stUpList","stUpCount"),("limit_down","stDownList","stDownCount"),("hot_stocks","stHotList","stHotCount"),("popularity_rises","stRapidList","stRapidCount")):
        rows=snapshot[kind]
        if kind=="popularity_rises":rows=[r for r in rows if r["hot_rank"]<=100 and r["hot_rank_change"]>=20]
        expect(page.locator("#"+count_id)).to_have_text(str(len(rows)))
        expect(page.locator("#"+list_id+" [data-st-kind=stock]")).to_have_count(len(rows))
        if rows:
            page.locator("#"+list_id+" [data-st-kind=stock]").first.click()
            expect(page.locator("#stDrawerBody")).to_contain_text(rows[0]["code"])
            page.locator("#stDrawerClose").click()
        page.locator("[data-st-list="+kind+"]").click()
        expect(page.locator(".st-pager")).to_contain_text(str(len(rows))+"条")
        if kind in ("limit_up","limit_down") and snapshot["limit_as_of"]:
            expect(page.locator("#stDrawerTitle")).to_contain_text(snapshot["limit_as_of"])
        if rows:
            with page.expect_download() as downloaded:page.locator("[data-st-export-list]").click()
            exported=list(csv.DictReader(io.StringIO(Path(downloaded.value.path()).read_text(encoding="utf-8-sig"))))
            assert len(exported)==len(rows) and set(r["code"] for r in exported)==set(r["code"] for r in rows)
        page.locator("#stDrawerClose").click()
    expected_rises=[r for r in snapshot["popularity_rises"] if r["hot_rank"]<=50 and r["hot_rank_change"]>=50]
    page.locator("#stRankCeiling").select_option("50")
    page.locator("#stRankGain").select_option("50")
    expect(page.locator("#stRapidCount")).to_have_text(str(len(expected_rises)))
    if expected_rises:
        expect(page.locator("#stRapidList .st-rank-row").first).to_contain_text("↑"+str(expected_rises[0]["hot_rank_change"])+"名")
    page.locator("#stRankCeiling").select_option("100")
    page.locator("#stRankGain").select_option("20")
    assert page.locator("#stCoreNews,#stCalendar,#stRefresh,#stAuto").count()==0
    assert calls["refresh"]==0,"页面不应再轮询整屏聚合任务"
    for kind,host,count in (("big_buys","stBuyList","stBuyCount"),("big_sells","stSellList","stSellCount")):
        expect(page.locator("#"+count)).to_have_text(str(len(orders[kind])))
        if orders[kind]:
            page.locator("#"+host+" [data-st-kind=order]").first.click()
            expect(page.locator("#stDrawerBody")).to_contain_text("来源原始信息")
            page.locator("#stDrawerBody [data-st-kind=stock]").click()
            expect(page.locator("#stDrawerBody")).to_contain_text("异动说明与实际时点")
            page.locator("#stDrawerClose").click()
    expect(page.locator("#stUpAsOf")).to_contain_text(snapshot["limit_as_of"] or "待更新")
    page.screenshot(path=str(OUT/"reference-1920.png"),full_page=True)
    print("compact header and reference palette: passed",flush=True)
    sector_with_members=next(r for r in snapshot["sectors"] if r["stock_codes"])
    page.locator('#stNodes [data-st-node="'+sector_with_members["id"]+'"]').click()
    expect(page.locator("#stDrawer")).to_be_visible()
    expect(page.locator("#stDrawerBody")).to_contain_text("板块发生了什么")
    expect(page.locator("#stDrawerBody")).to_contain_text(sector_with_members["quote"]["pct_label"])
    if sector_with_members["quote"]["note"]:
        expect(page.locator("#stDrawerBody")).to_contain_text(sector_with_members["quote"]["note"])
    page.locator("#stDrawerBody [data-st-kind=stock]").first.click()
    expect(page.locator("#stDrawerBody")).to_contain_text("异动说明与实际时点")
    page.screenshot(path=str(OUT/"stock-evidence.png"))
    page.locator("#stDrawerClose").click()
    page.locator("[data-st-list=sectors]").click()
    with page.expect_download() as downloaded:page.locator("[data-st-export-list]").click()
    sector_records=list(csv.DictReader(io.StringIO(Path(downloaded.value.path()).read_text(encoding="utf-8-sig"))))
    assert len(sector_records)==len(heat_sectors)
    exported=next(r for r in sector_records if r["name"]==sector_with_members["name"] and r["type"]==sector_with_members["type"])
    assert exported["display_pct"]=="" if sector_with_members["quote"]["pct"] is None else float(exported["display_pct"])==sector_with_members["quote"]["pct"]
    assert exported["net_label"]==sector_with_members["quote"]["net_label"]
    page.locator("#stDrawerClose").click()
    page.locator("[data-st-list=stocks]").click()
    expect(page.locator("#stDrawerTitle")).to_have_text("全部个股异动")
    if len(snapshot["stocks"])>30:
        page.locator("[data-st-page='2']").click()
        expect(page.locator(".st-pager")).to_contain_text("2/")
    search_code=snapshot["stocks"][0]["code"]
    page.locator("#stDetailSearch").fill(search_code)
    expect(page.locator(".st-detail-table tbody tr")).to_have_count(1)
    with page.expect_download() as downloaded:page.locator("[data-st-export-list]").click()
    records=list(csv.DictReader(io.StringIO(Path(downloaded.value.path()).read_text(encoding="utf-8-sig"))))
    assert len(records)==1 and records[0]["code"]==search_code
    page.locator("#stDrawerClose").click()
    if snapshot.get("short_news"):
        page.locator("#stNews [data-st-kind=news]").first.click()
        expect(page.locator("#stDrawerBody")).to_contain_text("消息内容")
        page.locator("#stDrawerClose").click()
    page.locator("#stSearch").fill("不存在的查询词xyz")
    expect(page.locator("#stStockCount")).to_have_text("0")
    expect(page.locator("#stNodes button")).to_have_count(0)
    expect(page.locator("#stUpCount")).to_have_text("0")
    expect(page.locator("#stDownCount")).to_have_text("0")
    expect(page.locator("#stHotCount")).to_have_text("0")
    expect(page.locator("#stRapidCount")).to_have_text("0")
    page.locator("#stSearch").fill("")
    # 所有列表持续滚动，悬停及全局暂停保持当前位置；盘口无按钮触发也会轮询。
    page.locator("#stScrollToggle").click()
    page.mouse.move(2,2)
    host=page.locator("#stDownList")
    before=host.evaluate("el=>el.__stScrollOffset||0")
    page.wait_for_function("before => document.getElementById('stDownList').__stScrollOffset > before+4",arg=before,timeout=3000)
    assert host.evaluate("el=>el.querySelector('.st-scroll-track').getAnimations()[0].effect.getTiming().easing")=="linear"
    host.evaluate("el=>{const animation=el.querySelector('.st-scroll-track').getAnimations()[0];animation.currentTime=animation.effect.getTiming().duration-250;}")
    page.wait_for_function("document.getElementById('stDownList').__stScrollOffset<10",timeout=2000)
    host.hover()
    page.wait_for_function("document.getElementById('stDownList').dataset.scrolling==='paused'")
    paused=host.evaluate("el=>el.__stScrollOffset");page.wait_for_timeout(350)
    assert abs(host.evaluate("el=>el.__stScrollOffset")-paused)<1
    page.mouse.move(2,2)
    page.locator("#stScrollToggle").click()
    paused=host.evaluate("el=>el.__stScrollOffset");page.wait_for_timeout(350)
    assert abs(host.evaluate("el=>el.__stScrollOffset")-paused)<1
    page.locator("#stScrollToggle").click()
    anchor=page.locator("#stBuyList .st-scroll-content [data-st-kind=order]").nth(3).element_handle() if len(orders["big_buys"])>3 else None
    anchor_before=anchor.evaluate("el=>({y:el.getBoundingClientRect().top,t:performance.now()})") if anchor else None
    order_update["inject"]=True
    page.wait_for_timeout(15500)
    assert calls["orders"]>=2,calls
    if anchor_before and order_update["added"]:
        anchor_after=anchor.evaluate("el=>({y:el.getBoundingClientRect().top,t:performance.now()})")
        assert abs(anchor_after["y"]-anchor_before["y"]+(anchor_after["t"]-anchor_before["t"])*.018)<6,"新事件插入导致正在观看的行跳动"
    # 消息响应包含最新关联实体，也只能更新消息面板。
    page.locator("#stMotion").click()
    stock_markup=page.locator("#stStockList .st-scroll-content").inner_html()
    nodes=page.locator("#stNodes [data-st-node]").element_handles()
    news_update["code"]=page.locator("#stNodes .st-node-stock").first.get_attribute("data-st-node")
    news_update["inject"]=True
    page.evaluate("()=>window.__stCallbacks[4200]()")
    expect(page.locator("#stNews .st-scroll-content")).to_contain_text("浏览器分区隔离验收")
    assert page.locator("#stStockList .st-scroll-content").inner_html()==stock_markup
    assert all(node.evaluate("el=>el.isConnected") for node in nodes),"新数据动效不得重建关系图节点"
    assert news_update["code"] in page.locator("#stScene").evaluate("el=>el.__stLastPulse.stocks")
    news_host=page.locator("#stNews");news_host.hover();news_host.evaluate("el=>el.scrollTop=0")
    arrived=page.locator('#stNews .st-scroll-content [data-st-id="browser-news-insertion"]')
    expect(arrived).to_have_class(re.compile("st-data-arrival"))
    page.wait_for_timeout(700)
    soft=arrived.evaluate("el=>Number(getComputedStyle(el,'::before').opacity)")
    page.wait_for_timeout(1300)
    bright=arrived.evaluate("el=>Number(getComputedStyle(el,'::before').opacity)")
    assert bright>soft+.1,"新数据应从柔和逐渐发亮"
    assert page.locator(".st-data-arrival").count()<=8
    page.screenshot(path=str(OUT/"aurora-meteor-1920.png"),full_page=True)
    page.wait_for_function("document.querySelectorAll('.st-data-arrival').length===0",timeout=12000)
    last_pulse=page.locator("#stScene").evaluate("el=>el.__stLastPulse.at")
    sector_update["inject"]=True
    page.evaluate("()=>window.__stCallbacks[11500]()")
    assert page.locator("#stNodes .st-node-sector").first.get_attribute("data-st-node")==snapshot["sectors"][1]["id"]
    assert page.locator("#stScene").evaluate("el=>el.__stLastPulse.at")>last_pulse,"关系图重排后仍应触发新路径的陨石动效"
    page.mouse.move(2,2)
    assert calls["refresh"]==0,calls

    for width,height in ((1440,900),(390,844)):
        page.set_viewport_size({"width":width,"height":height})
        page.screenshot(path=str(OUT/f"reference-{width}.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1"),width
    # 全局入口作为原页面上的dialog进入，可退出全屏并返回，原表单保持。
    print("details, filtering, export and responsive layouts: passed",flush=True)
    # 入口测试不触发原页面无关的行情网络请求，独立大屏继续使用同一真实快照。
    def launch_api(route):
        if "/api/short-term/overview" in route.request.url:route.fulfill(json=snapshot)
        elif "/api/short-term/orders" in route.request.url:route.fulfill(json=orders)
        elif "/api/short-term/refresh" in route.request.url:route.fulfill(json={"job":{"id":"browser","status":"finished"}})
        else:route.fulfill(json={"success":True,"stocks":[],"rows":[],"alerts":[],"items":[],"data":{}})
    page.route("**/api/**",launch_api)
    page.set_viewport_size({"width":1440,"height":900})
    page.goto(URL+"/desktop/watchlist",wait_until="domcontentloaded",timeout=60000)
    if page.locator("#riskDisclaimerModal").is_visible():page.locator("#riskDisclaimerAckBtn").click()
    page.evaluate("window.__shortTermPreservedValue='original-page'")
    page.locator("#shortTermScreenBtn").click()
    expect(page.locator("dialog iframe[title='短线作战大屏']")).to_be_visible()
    frame=page.frame_locator("iframe[title='短线作战大屏']")
    try:
        expect(frame.locator("#stScreen")).to_have_attribute("aria-busy","false")
    except AssertionError:
        print(json.dumps({"frame_errors":errors,"frame_text":frame.locator("body").inner_text()[:500]},ensure_ascii=False),flush=True)
        page.screenshot(path=str(OUT/"iframe-failure.png"))
        raise
    assert page.evaluate("Boolean(document.fullscreenElement)")
    frame.locator("#stFullscreen").click()
    page.wait_for_function("!document.fullscreenElement")
    frame.locator("#stReturn").click()
    expect(page.locator("dialog iframe[title='短线作战大屏']")).to_have_count(0)
    assert page.evaluate("window.__shortTermPreservedValue")=="original-page"
    assert errors==[],errors
    print(json.dumps({"browser":"passed","stocks":len(snapshot["stocks"]),"sectors":len(snapshot["sectors"]),"news":len(snapshot["news"]),"buy_events":len(orders["big_buys"]),"sell_events":len(orders["big_sells"]),"polls":calls,"console_errors":errors},ensure_ascii=False))
    browser.close()
