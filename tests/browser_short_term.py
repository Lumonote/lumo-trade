"""真实聚合结果的短线大屏交互验收，快照只读；支持隔离服务地址。"""
import csv
import io
import json
from pathlib import Path
import sys
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright, expect, Error

URL=sys.argv[1] if len(sys.argv)>1 else "http://127.0.0.1:7116"
OUT=Path(__file__).resolve().parents[1]/"results/short_term_preview"
OUT.mkdir(parents=True,exist_ok=True)
opener=build_opener(ProxyHandler({}))
snapshot=json.load(opener.open(URL+"/api/short-term/overview",timeout=30))
assert snapshot["stocks"] and snapshot["sectors"] and snapshot["calendar"],"需要先更新隔离服务中的真实数据"
# 浏览器检查保持真实聚合快照，避免重复触发网络更新。
snapshot["job"]=None
snapshot["updated_at"]=snapshot.get("updated_at") or snapshot["generated_at"]

with sync_playwright() as p:
    expect.set_options(timeout=25000)
    try:browser=p.chromium.launch(headless=True,args=["--enable-unsafe-swiftshader"])
    except Error:browser=p.chromium.launch(headless=True,channel="chrome",args=["--enable-unsafe-swiftshader"])
    page=browser.new_page(viewport={"width":1920,"height":1080},accept_downloads=True)
    page.set_default_timeout(25000)
    errors=[]
    page.on("pageerror",lambda e:errors.append(str(e)))
    page.route("**/api/short-term/overview*",lambda route:route.fulfill(json=snapshot))
    page.goto(URL+"/desktop/short_term",wait_until="networkidle",timeout=60000)
    expect(page.locator("#stScreen")).to_have_attribute("aria-busy","false")
    assert page.locator("#stStockCount").inner_text()==str(len(snapshot["stocks"]))
    assert page.locator("#stSectorCount").inner_text()==str(len(snapshot["sectors"]))
    assert page.locator("#stNodes button").count()>8
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--st-bg').trim()") == "#040609"
    assert page.locator(".st-header,.st-brand,.st-session,#stSources,#stExport,.st-controlbar nav").count()==0
    assert page.locator(".st-workspace").bounding_box()["y"]<=55
    assert page.evaluate("document.documentElement.scrollHeight <= innerHeight+1")
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
        if kind in ("limit_up","limit_down"):
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
    if not snapshot["core_news"]:
        assert page.locator(".st-core-panel").bounding_box()["height"]<=110
    expect(page.locator("#stUpAsOf")).to_contain_text(snapshot["limit_as_of"] or "待更新")
    page.screenshot(path=str(OUT/"reference-1920.png"),full_page=True)
    print("compact header and reference palette: passed",flush=True)
    sector_with_members=next(r for r in snapshot["sectors"] if r["stock_codes"])
    page.locator('#stNodes [data-st-node="'+sector_with_members["id"]+'"]').click()
    expect(page.locator("#stDrawer")).to_be_visible()
    expect(page.locator("#stDrawerBody")).to_contain_text("板块发生了什么")
    page.locator("#stDrawerBody [data-st-kind=stock]").first.click()
    expect(page.locator("#stDrawerBody")).to_contain_text("异动说明与实际时点")
    page.screenshot(path=str(OUT/"stock-evidence.png"))
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
    page.locator("#stCalendar [data-st-kind=event]").first.click()
    expect(page.locator("#stDrawerBody")).to_contain_text("短线观察与验证点")
    expect(page.locator("#stDrawerBody [data-st-external]")).to_be_visible()
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
    assert not {r["id"] for r in snapshot["core_news"]} & {r["id"] for r in snapshot["short_news"]}
    for width,height in ((1440,900),(390,844)):
        page.set_viewport_size({"width":width,"height":height})
        page.screenshot(path=str(OUT/f"reference-{width}.png"),full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1"),width
    # 全局入口作为原页面上的dialog进入，可退出全屏并返回，原表单保持。
    print("details, filtering, export and responsive layouts: passed",flush=True)
    # 入口测试不触发原页面无关的行情网络请求，独立大屏继续使用同一真实快照。
    def launch_api(route):
        if "/api/short-term/overview" in route.request.url:route.fulfill(json=snapshot)
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
    print(json.dumps({"browser":"passed","stocks":len(snapshot["stocks"]),"sectors":len(snapshot["sectors"]),"news":len(snapshot["news"]),"events":len(snapshot["calendar"]),"console_errors":errors},ensure_ascii=False))
    browser.close()
