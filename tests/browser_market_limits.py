"""隔离WebUI上的涨跌停交互验收；合成数据只写入显式指定的临时测试数据库。"""
import csv
import io
import json
import os
from pathlib import Path
import sys
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright, expect, Error

URL = sys.argv[1] if len(sys.argv)>1 else "http://127.0.0.1:7109"
OUT = Path(__file__).resolve().parents[1]/"results/market_limits_preview"
OUT.mkdir(parents=True,exist_ok=True)
if not os.environ.get("KRONOS_SQLITE_PATH", "").startswith("/private/tmp/"):
    raise RuntimeError("浏览器测试必须显式指定/private/tmp/内的隔离数据库")

from analysis.market_limits import build_report
from data_store import kv_repo
from webui.services.market_limits_service import NAMESPACE

dates=["2026-10-01","2026-10-02"]
boards={"BK0001":{"name":"测试行业","type":"行业"},"BK0002":{"name":"测试概念","type":"概念"}}
days,memberships,patterns=[],{},{}
tag={"tags":[{"key":"ma_bull_align","label":"N线多头排列","evidence":"浏览器测试形态"}],"as_of":dates[-1],"status":"已计算"}
patterns.update({"sector:"+c:tag for c in boards})
for index,date in enumerate(dates):
    events=[]
    for i in range(75):
        code=f"{600000+i:06d}"
        kind="U" if index==0 or i%2==0 else "D"
        events.append({"date":date,"code":code,"ts_code":code+".SH","name":"测试股票"+str(i),
                       "kind":kind,"label":"涨停" if kind=="U" else "跌停","close":11 if kind=="U" else 9,
                       "pct":10 if kind=="U" else -10,"up_limit":11,"down_limit":9,"one_price":False,
                       "first_time":93224,"last_time":"145500","open_times":i%3,"fd_amount":10000000+i,
                       "amount":200000000,"turnover_ratio":5,"up_stat":"2/2","source_streak":2,
                       "limit_amount":None,"float_mv":1e10,"total_mv":2e10})
        memberships[f"{date}:{code}"]=[{"code":c} for c in boards]
        patterns["stock:"+code]=tag if i%2==0 else {"tags":[],"as_of":dates[-1],"status":"已计算"}
    days.append({"date":date,"events":events,"covered":75,"unverified":0,
                 "moves":{r["code"]: -1 if r["kind"]=="D" else 1 for r in events}})
report=build_report(days,dates,dates[0],dates[1],memberships,patterns,boards)
report.update(updated_at="2026-11-01T12:00:00+08:00",notes=[],available=True,pattern_basis="合成浏览器测试数据")
kv_repo.set_(NAMESPACE,f"report:{dates[0]}:{dates[1]}",report)

opener=build_opener(ProxyHandler({}))
snapshot=json.load(opener.open(URL+"/api/market-health",timeout=30))
snapshot["source"]="browser_read_only"
snapshot["fusion"]["updated_at"]=snapshot["as_of"]

with sync_playwright() as p:
    try:
        browser=p.chromium.launch(headless=True)
    except Error:
        browser=p.chromium.launch(headless=True,channel="chrome")
    page=browser.new_page(viewport={"width":1440,"height":1080},accept_downloads=True)
    page.set_default_timeout(20000)
    errors=[]
    page.on("pageerror",lambda e:errors.append(str(e)))
    page.route("**/api/market-health?*",lambda route:route.fulfill(json=snapshot))
    page.route("**/api/market-health/refresh",lambda route:route.fulfill(json={"job":{"status":"finished"}}))
    page.goto(URL+f"/desktop/market_health?section=limits&limitStart={dates[0]}&limitEnd={dates[1]}",wait_until="networkidle",timeout=60000)
    if page.locator("#riskDisclaimerModal").is_visible():page.locator("#riskDisclaimerAckBtn").click()
    expect(page.locator("#mlStockCount")).to_have_text("75只股票")
    expect(page.locator("#mlEvents tbody tr").first.locator("td").nth(7)).to_contain_text("09:32:24")
    assert page.locator("#mlStocks tbody tr").count()==50
    page.locator('[data-ml-page="stocks:2"]').click()
    assert page.locator("#mlStocks tbody tr").count()==25
    page.locator("#mlSectors [data-ml-code='BK0001']").click()
    expect(page.locator("#mlDetail h3").first).to_contain_text("测试行业")
    assert page.locator("#mlDetailEvents tbody tr").count()==50
    page.locator('[data-ml-page="detailEvents:2"]').click()
    expect(page.locator("#mlDetailEvents .ml-pager")).to_contain_text("2/3页")
    page.locator("#mlDetailStocks [data-ml-code='600000']").first.click()
    expect(page.locator("#mlDetail h3").first).to_contain_text("测试股票0")
    assert page.locator("#mlDetailEvents tbody tr").count()==2
    assert page.locator("#mlDetail .ml-related [data-ml-kind='sector']").count()==2
    page.locator("[data-ml-close]").click()
    page.locator("#mlKind").select_option("D")
    expect(page.locator("#mlStockCount")).to_have_text("37只股票")
    # 筛选后的板块家数必须跟着变化，不能沿用全区间的75家。
    assert page.locator("#mlSectors tbody tr").first.locator("td").nth(2).inner_text()=="37 / 37家"
    with page.expect_download() as download:
        page.locator("#mlCsv").click()
    records=list(csv.DictReader(io.StringIO(Path(download.value.path()).read_text(encoding="utf-8-sig"))))
    assert len(records)==37 and all(r["事件"]=="跌停" for r in records)
    assert all("测试行业" in r["当天板块"] for r in records)
    page.locator("#mlKind").select_option("")
    page.locator("#mlStreak").select_option("2")
    expect(page.locator("#mlStockCount")).to_have_text("38只股票")
    page.locator("#mlStreak").select_option("0")
    page.locator("#mlSearch").fill("600001")
    expect(page.locator("#mlStockCount")).to_have_text("1只股票")
    page.locator("#mlSearch").fill("测试概念")
    expect(page.locator("#mlStockCount")).to_have_text("75只股票")
    page.locator("#mlSearch").fill("")
    page.locator("#mlType").select_option("概念")
    expect(page.locator("#mlSectorCount")).to_have_text("1个板块")
    page.locator("#mlType").select_option("")
    page.locator("#mlPattern").select_option("ma_bull_align")
    expect(page.locator("#mlStockCount")).to_have_text("75只股票")
    with page.expect_download() as download:
        page.locator("#mlExport").click()
    exported=json.loads(Path(download.value.path()).read_text())
    assert len(exported["events"])==150 and len(exported["sectors"])==2
    page.screenshot(path=str(OUT/"browser-desktop.png"),full_page=True)
    page.set_viewport_size({"width":390,"height":844})
    page.screenshot(path=str(OUT/"browser-mobile.png"),full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
    assert errors==[],errors
    print(json.dumps({"browser":"passed","records":len(exported["events"]),"console_errors":errors},ensure_ascii=False))
    browser.close()
