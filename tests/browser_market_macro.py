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
    for name in ("correlations", "sectors", "gold", "events", "fiscal", "debt", "calendar"):
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
    # 月频自动切换有效窗口与领先期，并使用真实API。
    page.locator('#mmFrequency').select_option('monthly')
    page.locator('#mmFactor').select_option('m2')
    with page.expect_response(lambda r: '/macro?' in r.url and 'factor=m2' in r.url):
        page.locator('#mmQueryButton').click()
    expect(page.locator('#mmTrend h3')).to_contain_text('M2同比')
    expect(page.locator('#mmQueryButton')).to_be_enabled()
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
