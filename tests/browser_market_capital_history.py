"""独立夹具检查资金历史的零值、缺日、区间和请求竞争；不采集实盘数据。"""
import json
from pathlib import Path
import sys
import urllib.request
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect, Error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.market_capital import capital_report, participant_history

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7098"
OUT = ROOT / "results/market_capital_preview"
OUT.mkdir(parents=True, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
snapshot = json.load(opener.open(URL + "/api/market-health", timeout=30))
snapshot.update(source="browser_read_only")
snapshot["fusion"]["updated_at"] = snapshot["as_of"]
DATES = ["2026-11-16", "2026-11-17", "2026-11-18"]
fixture = {"calendar": DATES, "as_of": DATES[-1], "etf_previous_date": "2026-11-13",
           "sources": [{"key": "etf", "as_of": DATES[-1], "rows": 6, "status": "独立测试夹具"}],
           "etf_meta": [{"ts_code": "158000.SZ", "extname": "零申赎样本ETF", "index_name": "测试指数"},
                        {"ts_code": "158001.SZ", "extname": "部分净值样本ETF", "index_name": "测试指数"}],
           "etf": [{"ts_code": "158000.SZ", "trade_date": d, "total_share": 2292.5835, "nav": 1.0326} for d in DATES[1:]] +
                  [{"ts_code": "158001.SZ", "trade_date": d, "total_share": share, "nav": nav} for d, share, nav in
                   [("2026-11-13", 90, 1), (DATES[0], 100, 1), (DATES[1], 110, 1), (DATES[2], 115, None)]]}


with sync_playwright() as p:
    try:
        browser = p.chromium.launch(headless=True)
    except Error:
        browser = p.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.set_default_timeout(15000)
    errors, prices, history_requests = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: prices.append(request.url) if "/api/stock-kline/" in request.url else None)
    page.route("**/api/market-health?*", lambda route: route.fulfill(json=snapshot))
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(json={"job": {"status": "failed"}}))

    def participants(route):
        q = {k: v[0] for k, v in parse_qs(urlsplit(route.request.url).query).items()}
        result = capital_report(fixture, participant=q.get("participant", "main"), start=q.get("start"), end=q.get("end"))
        route.fulfill(json={**result, "data_as_of": DATES[-1], "today": "2026-11-19", "disclosure": "独立测试夹具"})

    def history(route):
        q = {k: v[0] for k, v in parse_qs(urlsplit(route.request.url).query).items()}
        history_requests.append(q)
        route.fulfill(json=participant_history(fixture, **q))

    page.route("**/api/market-health/participants?*", participants)
    page.route("**/api/market-health/participants/history?*", history)
    page.goto(URL + "/desktop/market_health?section=participants", wait_until="domcontentloaded")
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()
    page.locator('[data-mp-type="etf"]').click()
    expect(page.locator("#mpCount")).to_contain_text("当前 2 项")
    zero = page.locator('[data-mp-history="158000.SZ"]').first
    zero_row = zero.locator("xpath=ancestor::tr")
    expect(zero_row.locator("td").nth(1)).to_contain_text("0.00元")
    expect(zero_row.locator("td").nth(2)).to_contain_text("有效 1/1 日")
    page.locator("#mpStart").fill("2026-11-16")  # 尚未应用。
    zero.click()
    expect(page.locator("#mpHistoryStatus")).to_contain_text("查询已完成")
    assert history_requests[-1]["start"] == DATES[-1]
    expect(page.locator("#mpHistorySummary")).to_contain_text("0.00元")
    expect(page.locator("#mpHistoryChart [data-candle]")).to_have_count(1)
    assert float(page.locator("[data-candle]").get_attribute("height")) >= 2
    page.locator("#mpHistoryModal").screenshot(path=str(OUT / "history_etf_zero.png"))
    before = page.locator("#mpCount").inner_text()
    page.locator("#mpHistoryStart").fill(DATES[0])
    page.locator("#mpHistoryQuery button").click()
    expect(page.locator("#mpHistoryRows tbody tr")).to_have_count(3)
    expect(page.locator("#mpHistoryRows tbody tr").first).to_contain_text("未采集 / 未披露")
    expect(page.locator("#mpHistoryChart [data-candle]")).to_have_count(1)
    assert page.locator("#mpCount").inner_text() == before
    page.locator("#mpHistoryClose").click()

    # 榜单有效小计、方向过滤和排序不能重新丢掉部分覆盖的金额。
    page.locator("#mpQuery button[type=submit]").click()
    expect(page.locator("#mpMeta")).to_contain_text(DATES[0])
    partial_row = page.locator('[data-mp-history="158001.SZ"]').first.locator("xpath=ancestor::tr")
    expect(partial_row.locator("td").nth(1)).to_contain_text("+20.00万")
    expect(partial_row.locator("td").nth(1)).to_contain_text("有效 2/3 日")
    expect(partial_row.locator("td").nth(2)).to_contain_text("250,000")
    page.locator("#mpDirection").select_option("buy")
    expect(page.locator("#mpCount")).to_contain_text("当前 1 项")
    page.locator("#mpDirection").select_option("all")
    page.locator("#mpStocks th").nth(1).click()
    expect(page.locator("#mpStocks [data-mp-history]").first).to_have_attribute("data-mp-history", "158000.SZ")
    page.locator("#mpStocks th").nth(1).click()
    expect(page.locator("#mpStocks [data-mp-history]").first).to_have_attribute("data-mp-history", "158001.SZ")

    # 模拟已返回但延后交付的旧请求，覆盖仅 AbortController 无法解决的竞争。
    page.evaluate("""() => {
      window.__originalFetch=window.fetch;window.__holdNext=true;
      window.fetch=async (...args)=>{
        const response=await window.__originalFetch(...args);
        if(String(args[0]).includes('/participants/history?')&&window.__holdNext){
          window.__holdNext=false;
          const body=await response.text();
          return new Promise(resolve=>{window.__releaseOld=()=>resolve(new Response(body,{status:200,headers:{'Content-Type':'application/json'}}));});
        }
        return response;
      };
    }""")
    zero.click()
    page.wait_for_function("typeof window.__releaseOld==='function'")
    page.locator("#mpHistoryClose").click()
    page.locator('[data-mp-history="158001.SZ"]').first.click()
    expect(page.locator("#mpHistoryStatus")).to_contain_text("查询已完成")
    expect(page.locator("#mpHistoryTitle")).to_contain_text("部分净值样本ETF")
    expect(page.locator("#mpHistorySummary")).to_contain_text("+20.00万")
    page.evaluate("async()=>{window.__releaseOld();await new Promise(resolve=>setTimeout(resolve,0));}")
    expect(page.locator("#mpHistoryMeta")).to_contain_text("158001.SZ")
    expect(page.locator("#mpHistoryRows tbody tr").last).to_contain_text("缺当日有效净值")
    expect(page.locator("#mpHistoryChart [data-candle]")).to_have_count(2)
    page.locator("#mpHistoryModal").screenshot(path=str(OUT / "history_etf_partial.png"))
    page.set_viewport_size({"width": 800, "height": 900})
    assert page.locator("#mpHistoryModal").evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    page.locator("#mpHistoryStart").fill("2026-11-14")
    page.locator("#mpHistoryEnd").fill("2026-11-14")
    page.locator("#mpHistoryQuery button").click()
    expect(page.locator("#mpHistoryStatus")).to_contain_text("查询已完成")
    expect(page.locator("#mpHistoryPoint")).to_contain_text("当前区间没有记录")
    assert not errors and not prices, (errors, prices)
    print(json.dumps({"checks": "real zero, missing day, missing NAV, partial totals, sorting, independent range, stale response, empty range, 800px layout", "errors": errors}, ensure_ascii=False))
    browser.close()
