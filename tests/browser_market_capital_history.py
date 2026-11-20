"""独立夹具检查资金历史与 ETF 区间刷新；不采集实盘数据。"""
import datetime as dt
import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect
from market_browser_harness import HEALTH, URL, market_page

from analysis.market_capital import capital_report, etf_rows, margin_report, participant_history

DATES = ["2026-11-16", "2026-11-17", "2026-11-18"]
finance = margin_report({"as_of": DATES[-1], "calendar": DATES})
finance.update(history=[{"date": d, "rzye": 1e11 + i*1e8, "net_buy": 1e8} for i, d in enumerate(DATES)],
               disclosure="独立测试夹具")
fixture = {"calendar": DATES, "as_of": DATES[-1], "etf_previous_date": "2026-11-13",
           "sources": [{"key": "etf", "as_of": DATES[-1], "rows": 6, "status": "独立测试夹具"}],
           "etf_meta": [{"ts_code": "158000.SZ", "extname": "零申赎样本ETF", "index_name": "测试指数"},
                        {"ts_code": "158001.SZ", "extname": "部分净值样本ETF", "index_name": "测试指数"}],
           "etf": [{"ts_code": "158000.SZ", "trade_date": d, "total_share": 2292.5835, "nav": 1.0326} for d in DATES[1:]] +
                  [{"ts_code": "158001.SZ", "trade_date": d, "total_share": share, "nav": nav} for d, share, nav in
                   [("2026-11-13", 90, 1), (DATES[0], 100, 1), (DATES[1], 110, 1), (DATES[2], 115, None)]]}
REFRESH_START, REFRESH_END = "2026-10-15", "2026-11-19"
refresh_params = {"participant": "etf", "start": REFRESH_START, "end": REFRESH_END}
first, last = dt.date.fromisoformat(REFRESH_START), dt.date.fromisoformat(REFRESH_END)
refresh_days = [d.isoformat() for n in range((last - first).days + 1)
                if (d := first + dt.timedelta(days=n)).weekday() < 5]
refreshed_fixture = {**fixture, "calendar": refresh_days, "as_of": REFRESH_END, "etf_previous_date": "2026-10-14",
                     "etf": [*fixture["etf"], {"ts_code": "158001.SZ", "trade_date": REFRESH_END, "total_share": 120, "nav": 1}]}
refresh_result = {"processed_days": len(refresh_days),
                  "amount_days": len({r["date"] for r in etf_rows(refreshed_fixture) if r["net"] is not None})}


with market_page() as (page, read, OUT):
    snapshot = read(HEALTH)
    page.set_default_timeout(15000)
    errors, prices, history_requests = [], [], []
    participant_requests, refresh_requests, refresh_polls = [], [], []
    financing_refreshes, financing_polls = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: prices.append(request.url) if "/api/stock-kline/" in request.url else None)
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(json={"job": {"status": "failed"}}))

    def financing(route):
        job = None
        if financing_refreshes:
            financing_polls.append(len(financing_refreshes))
            done = financing_polls.count(len(financing_refreshes)) > 1
            job = {"status": "finished" if done else "running", "progress": 100 if done else 50,
                   "result": {"as_of": DATES[-1]} if done else None}
        route.fulfill(json={**finance, "job": job})

    def refresh_financing(route):
        financing_refreshes.append(route.request.post_data_json)
        route.fulfill(status=202, json={"job": {"status": "queued"}})

    page.route("**/api/market-health/financing?*", financing)
    page.route("**/api/market-health/financing/refresh", refresh_financing)

    def participants(route):
        q = {k: v[0] for k, v in parse_qs(urlsplit(route.request.url).query).items()}
        participant_requests.append(q)
        global published
        job = None
        if refresh_requests:
            if published is fixture:
                refresh_polls.append(q)
            job = {"id": "test-etf-range", "params": refresh_params, "status": "running", "progress": 50}
            if len(refresh_polls) > 1:
                published = refreshed_fixture
                job.update(status="finished", progress=100, result=refresh_result)
        result = capital_report(published, participant=q.get("participant", "main"), start=q.get("start"), end=q.get("end"))
        route.fulfill(json={**result, "job": job, "data_as_of": published["as_of"], "today": REFRESH_END, "disclosure": "独立测试夹具"})

    def refresh(route):
        refresh_requests.append(route.request.post_data_json)
        route.fulfill(status=202, json={"job": {"id": "test-etf-range", "status": "queued", "params": refresh_params}})

    def history(route):
        q = {k: v[0] for k, v in parse_qs(urlsplit(route.request.url).query).items()}
        history_requests.append(q)
        route.fulfill(json=participant_history(published, **q))

    published = fixture
    page.route("**/api/market-health/participants?*", participants)
    page.route("**/api/market-health/participants/refresh", refresh)
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
    candle_chart = page.locator("#mpHistoryChart .mh-date-chart").first
    date_label = candle_chart.locator(".mh-date-label")
    date_guide = candle_chart.locator(".mh-date-guide")
    point_detail = page.locator("#mpHistoryPoint")
    for date in DATES[:2]:
        candle = candle_chart.locator(f'[data-candle="{date}"]')
        candle.hover()
        expect(date_label).to_be_visible()
        expect(date_label).to_have_text(date)
        expect(date_guide).to_have_css("visibility", "visible")
        expect(point_detail).to_contain_text(f"交易日：{date}")
        expect(point_detail).to_contain_text("净申购估算：+10.00万")
        expect(point_detail).to_contain_text("净赎回估算：0.00元")
    page.locator("#mpHistoryModal").screenshot(path=str(OUT / "history_etf_partial.png"))
    page.locator("#mpHistoryTitle").hover()
    expect(date_label).to_be_hidden()
    expect(date_guide).to_have_css("visibility", "hidden")
    page.set_viewport_size({"width": 800, "height": 900})
    assert page.locator("#mpHistoryModal").evaluate("el=>el.scrollWidth<=el.clientWidth+1")
    keyboard_point = candle_chart.locator(f'[data-mh-chart-date="{DATES[0]}"]')
    keyboard_point.focus()
    expect(keyboard_point).to_be_focused()
    expect(date_label).to_be_visible()
    expect(date_label).to_have_text(DATES[0])
    expect(point_detail).to_contain_text(f"交易日：{DATES[0]}")
    page.locator("#mpHistoryStart").fill("2026-11-14")
    page.locator("#mpHistoryEnd").fill("2026-11-14")
    page.locator("#mpHistoryQuery button").click()
    expect(page.locator("#mpHistoryStatus")).to_contain_text("查询已完成")
    expect(page.locator("#mpHistoryPoint")).to_contain_text("当前区间没有记录")
    expect(date_label).to_have_count(0)
    page.locator("#mpHistoryClose").click()

    # 榜单查询与历史区间搜索保持只读；未点查询的新日期直接用于 ETF 刷新。
    assert participant_requests[-1]["start"] == DATES[0] and not refresh_requests
    page.locator("#mpStart").fill(REFRESH_START)
    page.locator("#mpEnd").fill(REFRESH_END)
    page.locator("#mpRefresh").click()
    expect(page.locator("#mpStatus")).to_contain_text(f"ETF {REFRESH_START} 至 {REFRESH_END} · 更新中")
    expect(page.locator("#mpRefresh")).to_be_disabled()
    expect(page.locator("#mpStatus")).to_contain_text("已处理 26 个交易日，其中 4 日有可估算净额")
    expect(page.locator("#mpRefresh")).to_be_enabled()
    expect(page.locator("#mpMeta")).to_contain_text(f"查询 {REFRESH_START} 至 {REFRESH_END}")
    expect(partial_row.locator("td").nth(1)).to_contain_text("+25.00万")
    assert refresh_requests == [refresh_params] and all(q == refresh_params for q in refresh_polls)
    for start, end, amount in ((REFRESH_START, REFRESH_END, "+25.00万"), (DATES[0], DATES[-1], "+20.00万")):
        if start != REFRESH_START:
            page.locator("#mpStart").fill(start)
            page.locator("#mpEnd").fill(end)
            page.locator("#mpQuery button[type=submit]").click()
        expect(partial_row.locator("td").nth(1)).to_contain_text(amount)
        partial_row.locator("[data-mp-history]").click()
        expect(page.locator("#mpHistoryStatus")).to_contain_text("查询已完成")
        expect(page.locator("#mpHistorySummary")).to_contain_text(amount)
        assert (history_requests[-1]["start"], history_requests[-1]["end"]) == (start, end)
        page.locator("#mpHistoryClose").click()
    # 两融个股样本为空时首次自动补数，之后仍可手动更新。
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.locator("#mhTab-financing").click()
    for attempt in (1, 2):
        if attempt == 2:
            page.locator("#mfRefresh").click()
        expect(page.locator("#mfStatus")).to_contain_text("更新中 50%")
        expect(page.locator("#mfRefresh")).to_be_disabled()
        expect(page.locator("#mfStatus")).to_contain_text(f"更新完成 · 数据截至 {DATES[-1]}")
        expect(page.locator("#mfRefresh")).to_be_enabled()
        assert len(financing_refreshes) == attempt
    finance_chart = page.locator("#mfTrend .mh-date-chart")
    finance_chart.locator(f'[data-mh-chart-date="{DATES[1]}"]').hover()
    expect(finance_chart.locator(".mh-date-label")).to_have_text(DATES[1])
    page.locator("#mhTab-indices").click()
    index_chart = page.locator("#mhIndexChart .lp-frame")
    index_date = next(row for row in snapshot["indices"] if row["symbol"] == "sh000001")["series"][-2]["date"]
    index_chart.locator("svg").press("End")
    index_chart.locator("svg").press("ArrowLeft")
    expect(index_chart.locator(".lp-hover-date")).to_have_text(index_date)
    assert not errors and not prices, (errors, prices)
    print(json.dumps({"checks": "real zero, missing day, missing NAV, partial totals, sorting, independent range, stale response, empty range, 800px layout, read-only queries, 26-day range refresh, candle dates and amounts, financing auto-fill and manual refresh, financing hover, index keyboard dates", "errors": errors}, ensure_ascii=False))
