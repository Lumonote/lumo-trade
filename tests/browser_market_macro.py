"""真实宏观API与桌面/移动浏览器交互验证，不触发外部全量采集。"""
import csv
import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import Error, expect, sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7110"
OUT = Path(__file__).resolve().parents[1] / "results/market_macro_preview"
OUT.mkdir(parents=True, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
snapshot = json.load(opener.open(URL + "/api/market-health", timeout=30))
snapshot["source"] = "browser_read_only"
snapshot.setdefault("fusion", {})["updated_at"] = snapshot["as_of"]
report = json.load(opener.open(URL + "/api/market-health/macro", timeout=30))

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(headless=True)
    except Error:
        browser = p.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    page.set_default_timeout(30000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/market-health?*", lambda route: route.fulfill(json=snapshot))
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(json={"job": {"status": "failed", "error": "只读检查"}}))
    page.goto(URL + "/desktop/market_health?section=macro", wait_until="domcontentloaded", timeout=60000)
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()
    expect(page.locator("#mmMeta")).to_contain_text(report["as_of"], timeout=45000)
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    assert page.locator("#mmMetrics .mm-metric").count() == len(report["metrics"])
    assert page.locator("#mmSectors tbody tr").count() == 31
    assert page.locator("#mmTrend svg").count() == 1
    assert page.locator("#mmScatter svg").count() == 1
    assert "样本 R²" in page.locator("#mmScatter").inner_text()
    # 每张表的表头都可排序，数值而非格式化字符串进行比较。
    for name in ("valuation-comparison", "correlations", "sectors", "gold", "events", "fiscal", "debt", "calendar"):
        header = page.locator(f'[data-mm-table="{name}"] th').first
        header.click()
        expect(header).to_have_attribute("aria-sort", "ascending")
        header.click()
        expect(header).to_have_attribute("aria-sort", "descending")
    header = page.locator('#mmSectors th').nth(1)
    header.click(); header.click()
    expected = max(r["rho"] for r in report["sector_sensitivity"] if r["rho"] is not None)
    assert float(page.locator('#mmSectors tbody tr').first.locator('td').nth(1).inner_text()) == round(expected, 2)
    page.locator('#marketHealthRoot').evaluate('(el)=>{for(let p=el.parentElement;p;p=p.parentElement)p.scrollTop=0;window.scrollTo(0,0);}')
    page.screenshot(path=str(OUT / "macro_top.png"))
    page.screenshot(path=str(OUT / "macro_full.png"), full_page=True)

    def analyze_factor(factor):
        with page.expect_response(lambda r: '/macro?' in r.url and f'factor={factor}' in r.url) as response:
            page.locator('#mmQueryButton').click()
        assert response.value.ok, (response.value.status, response.value.url)
        expect(page.locator('#mmQueryButton')).to_be_enabled()
        assert page.locator('#mmError').is_hidden(), page.locator('#mmError').text_content()
        return response.value.json()

    # 联动双轴保留；独立指标 K 线不丢弃单点、平值，也不把无报价区间补成数据。
    page.locator('#mmFactor').select_option('lpr5y')
    lpr = analyze_factor('lpr5y')
    expect(page.locator('#mmTrend h3')).to_have_text('沪深300 × 5年期LPR')
    expect(page.locator('#mmTrend .mm-legend')).to_contain_text('左轴')
    expect(page.locator('#mmTrend .mm-legend')).to_contain_text('右轴')
    expect(page.locator('#mmFactorHistoryTitle')).to_contain_text('5年期LPR')
    assert page.locator('#mmCandlePlot svg').count() == 1
    # 双轴按真实观测日命中：指数有交易但 LPR 无新报价，不借用另一日期的值。
    asset_dates = [row['date'] for row in lpr['chart']['asset']]
    factor_dates = {row['date'] for row in lpr['chart']['factor'] if asset_dates[0] <= row['date'] <= asset_dates[-1]}
    index_only = next(date for date in asset_dates[len(asset_dates)//2:] if date not in factor_dates)
    trend = page.locator('#mmTrend .mh-date-chart')
    expect(trend.locator('[data-mh-chart-date]')).to_have_count(len(set(asset_dates) | factor_dates))
    trend_hit = trend.locator(f'[data-mh-chart-date="{index_only}"]')
    trend_hit.hover()
    expect(trend.locator('.mh-date-label')).to_have_text(index_only)
    assert '5年期LPR' not in trend_hit.locator('title').text_content()

    def candle_range(start, end):
        page.locator('#mmCandleStart').fill(start)
        page.locator('#mmCandleEnd').fill(end)
        page.locator('#mmCandleQuery button[type="submit"]').click()

    candle_range('2026-07-20', '2026-07-20')
    expect(page.locator('[data-mm-candle]')).to_have_count(1)
    expect(page.locator('#mmCandleDetail')).to_contain_text('开 3.50')
    assert float(page.locator('[data-mm-candle] rect').last.get_attribute('height')) >= 2
    candle_range('2026-07-20', '2026-08-20')
    expect(page.locator('[data-mm-candle]')).to_have_count(2)
    expect(page.locator('#mmCandleDetail')).to_contain_text('收 3.50')
    candle_plot = page.locator('#mmCandlePlot')
    candle_label = candle_plot.locator('.mh-date-label')
    guide = candle_plot.locator('.mh-date-guide')
    for i, date in enumerate(('2026-07-20', '2026-08-20')):
        mark = candle_plot.locator('[data-mm-candle]').nth(i)
        mark.locator('rect').last.hover()
        expect(candle_label).to_be_visible()
        expect(candle_label).to_have_text(date)
        expect(guide).to_have_css('visibility', 'visible')
        expect(guide).to_have_attribute('x1', mark.get_attribute('data-mh-chart-x'))
        expect(page.locator('#mmCandleDetail')).to_contain_text(date)
    candle_plot.screenshot(path=str(OUT / 'macro_candle_hover.png'))
    page.locator('#mmCandleStart').hover()
    expect(candle_label).to_be_hidden()
    expect(guide).to_have_css('visibility', 'hidden')

    page.locator('#mmCandleBucket').select_option('quarter')
    candle_range('2026-07-20', '2026-08-20')
    expect(candle_plot.locator('[data-mm-candle]')).to_have_count(1)
    page.set_viewport_size({'width':390, 'height':844})
    page.wait_for_function("""() => {
      const plot = document.querySelector('#mmCandlePlot');
      return plot.querySelector('svg')?.viewBox.baseVal.width === plot.clientWidth;
    }""")
    candle_plot.locator('[data-mm-candle] rect').last.hover()
    expect(candle_label).to_be_visible()
    expect(candle_label).to_have_text('2026-07-20 至 2026-08-20')
    assert candle_label.evaluate('''label => {
      const box = label.getBoundingClientRect(), plot = label.parentElement.getBoundingClientRect();
      return box.left >= Math.max(0, plot.left) && box.right <= Math.min(innerWidth, plot.right)
        && box.top >= plot.top && box.bottom <= plot.bottom;
    }''')
    candle_plot.screenshot(path=str(OUT / 'macro_candle_hover_mobile.png'))
    candle_range('2026-07-21', '2026-08-19')
    expect(page.locator('#mmCandlePlot')).to_contain_text('所选区间没有实际观测')
    assert page.locator('#mmCandlePlot svg').count() == 0
    expect(candle_label).to_have_count(0)
    page.locator('#mmCandleBucket').select_option('month')
    candle_range('2026-07-20', '2026-08-20')
    page.locator('#mmCandleReset').click()
    expect(page.locator('#mmCandlePlot svg')).to_have_count(1)
    page.set_viewport_size({'width':1440, 'height':1000})

    page.locator('#mmFactor').select_option('equity_bond_spread')
    analyze_factor('equity_bond_spread')
    expect(page.locator('#mmTrend h3')).to_have_text('沪深300 × 股债利差')
    comparison = page.locator('#mmValuationComparison tbody tr')
    expect(comparison).to_have_count(5)
    expect(comparison.filter(has_text='沪深300').locator('td').nth(4)).to_have_text('6.09')
    expect(comparison.filter(has_text='创业板指').locator('.mm-low-mark')).to_have_text('历史低估')
    expect(page.locator('#mmFactorHistoryTitle')).to_contain_text('沪深300 股债利差')
    page.locator('#mmEquity').select_option('csi500')
    page.locator('#mmFrequency').select_option('monthly')
    expect(page.locator('#mmFactor')).to_have_value('equity_bond_spread')
    analyze_factor('equity_bond_spread')
    expect(page.locator('#mmTrend h3')).to_have_text('中证500 × 股债利差')
    expect(comparison.filter(has_text='中证500').first).to_contain_text('研究指数')
    expect(page.locator('#mmFactorHistoryTitle')).to_contain_text('中证500 股债利差')
    expect(page.locator('#mmCandlePlot svg')).to_have_count(1)
    page.locator('#mmValuationComparison').screenshot(path=str(OUT / 'macro_spread_valuation.png'))
    # 月频自动切换有效窗口与领先期，并使用真实API。
    page.locator('#mmFrequency').select_option('monthly')
    page.locator('#mmFactor').select_option('m2')
    analyze_factor('m2')
    expect(page.locator('#mmTrend h3')).to_contain_text('M2同比')
    assert '月频' in page.locator('#mmScatter').inner_text()
    # 黄金卡片点击和政策检索。
    with page.expect_response(lambda r: '/macro?' in r.url and 'factor=gold' in r.url):
        page.locator('[data-mm-factor="gold"]').first.click()
    expect(page.locator('#mmTrend h3')).to_contain_text('Au99.99')
    expect(page.locator('#mmQueryButton')).to_be_enabled()
    page.locator('#mmKeyword').fill('化债')
    with page.expect_response(lambda r: '/macro?' in r.url and 'keyword=' in r.url):
        page.locator('#mmEventQuery button').click()
    expect(page.locator('#mmEvents')).to_contain_text('隐性债务')
    expect(page.locator('#mmQueryButton')).to_be_enabled()
    with page.expect_download() as event_download:
        page.locator('#mmEventsCsv').click()
    events = list(csv.DictReader(Path(event_download.value.path()).read_text(encoding='utf-8-sig').splitlines()))
    assert events and '隐性债务' in events[0]['事件']
    page.locator('#mmEarnings').fill('10')
    page.locator('#mmPe').fill('-10')
    expect(page.locator('#mmScenarioResult')).to_contain_text('-1.00%')
    with page.expect_download() as download:
        page.locator('#mmExport').click()
    saved = json.loads(Path(download.value.path()).read_text())
    assert saved['scenario']['identity_pct'] < 0
    assert saved['as_of'] == report['as_of']
    # 服务出错时保留已有研究和日期；无需刷新或清空结果。
    original = page.locator('#mmMeta').inner_text()
    page.route('**/api/market-health/macro?*', lambda route: route.fulfill(status=503, json={'error':'模拟通道失败'}))
    page.locator('#mmQueryButton').click()
    expect(page.locator('#mmError')).to_contain_text('原日期保留')
    assert page.locator('#mmMeta').inner_text() == original
    page.unroute('**/api/market-health/macro?*')
    # 更新复用任务，轮询失败不覆盖旧数据；拦截请求，不实际采集。
    page.route('**/api/market-health/macro/refresh', lambda route: route.fulfill(status=202,json={'job':{'status':'running','progress':30}}))
    page.locator('#mmRefresh').click()
    expect(page.locator('#mmRefresh')).to_be_disabled()
    expect(page.locator('#mmJob')).to_contain_text('30%')
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#mhTab-macro').scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / 'macro_mobile.png'), full_page=True)
    page.locator('#marketHealthRoot').evaluate('(el)=>{for(let p=el.parentElement;p;p=p.parentElement)p.scrollTop=0;window.scrollTo(0,0);}')
    page.screenshot(path=str(OUT / 'macro_mobile_top.png'))
    page.locator('.mm-intro').evaluate("el=>el.scrollIntoView({block:'start'})")
    page.screenshot(path=str(OUT / 'macro_mobile_panel.png'))
    overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
    if overflow:
        print(json.dumps(page.evaluate("Array.from(document.querySelectorAll('body *')).map(e=>({id:e.id,cls:e.className,width:Math.round(e.getBoundingClientRect().width),right:Math.round(e.getBoundingClientRect().right),overflow:getComputedStyle(e).overflowX})).filter(r=>r.right>innerWidth+1&&r.width>0).slice(0,30)"),ensure_ascii=False),flush=True)
    assert not overflow
    assert not errors, errors
    browser.close()
print(json.dumps({'status':'passed','metrics':len(report['metrics']),'industries':31,'errors':errors,'screenshots':str(OUT)},ensure_ascii=False))
