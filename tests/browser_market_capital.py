"""在独立运行的WebUI上验证真实两融/资金API与浏览器交互，不启动数据采集。"""
import json
from pathlib import Path
import sys
import urllib.request

from playwright.sync_api import sync_playwright, expect, Error

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7098"
OUT = Path(__file__).resolve().parents[1] / "results/market_capital_preview"
OUT.mkdir(parents=True, exist_ok=True)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
snapshot = json.load(opener.open(URL + "/api/market-health", timeout=30))
snapshot["source"] = "browser_read_only"
snapshot["fusion"]["updated_at"] = snapshot["as_of"]
finance_snapshot = json.load(opener.open(URL + "/api/market-health/financing", timeout=30))
capital_snapshot = json.load(opener.open(URL + "/api/market-health/participants", timeout=30))

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(headless=True)
    except Error:
        browser = p.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # 现有大盘自动行情和自动全量采集使用只读快照，新增API保持真实请求。
    page.route("**/api/market-health?*", lambda route: route.fulfill(json=snapshot))
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(json={"job": {"status": "failed", "error": "浏览器检查不启动全量采集"}}))
    page.goto(URL + "/desktop/market_health?section=financing", wait_until="networkidle", timeout=60000)
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()
    expect(page.locator("#mfCount")).to_contain_text(str(len(finance_snapshot["rows"])), timeout=30000)
    assert page.locator("#mfStocks tbody tr").count() == min(50,len(finance_snapshot["rows"]))
    page.screenshot(path=str(OUT / "financing.png"), full_page=True)
    page.screenshot(path=str(OUT / "financing_top.png"))
    page.locator("#mfRisk").select_option("closeout")
    assert "低于平仓线" in page.locator("#mfStocks").inner_text()
    page.locator("#mfStocks [data-mf-code]").first.click()
    expect(page.locator("#mfDetail")).to_be_visible()
    assert "模型担保比例" in page.locator("#mfDetail").inner_text()
    page.locator("#mfRisk").select_option("all")
    page.locator("#mfSearch").fill("不存在的股票")
    expect(page.locator("#mfCount")).to_contain_text("筛选 0")
    page.locator("#mfSearch").fill("")
    with page.expect_download() as download:
        page.locator("#mfCsv").click()
    assert download.value.suggested_filename.endswith(".csv")
    previous = page.locator("#mfDistribution").inner_text()
    page.locator("#mfDeposit").fill("100")
    with page.expect_response(lambda response: "/financing?" in response.url and "deposit=100" in response.url):
        page.locator("#mfModel button[type=submit]").click()
    expect(page.locator("#mfDistribution")).not_to_have_text(previous, timeout=30000)
    page.locator("#mhTab-participants").click()
    expect(page.locator("#mpCount")).to_contain_text(str(len(capital_snapshot["rows"])), timeout=30000)
    expect(page.locator("#mpMatrix")).to_contain_text("订单分歧")
    assert page.locator("#mpMatrix tbody tr").count() > 1
    page.screenshot(path=str(OUT / "participants.png"), full_page=True)
    page.locator('[data-mp-days="5"]').click()
    expect(page.locator("#mpMeta")).to_contain_text("2026-10-26", timeout=30000)
    for kind in ("institution", "etf", "national", "quant", "sm"):
        with page.expect_response(lambda response: "/participants?" in response.url and "participant=" + kind in response.url):
            page.locator(f'[data-mp-type="{kind}"]').click()
        expect(page.locator(f'[data-mp-type="{kind}"]')).to_have_attribute("aria-pressed", "true")
        if kind == "etf":
            expect(page.locator("#mpStocks")).to_contain_text("申赎估算")
            assert page.locator("#mpStocks tbody tr").count() == 50
        if kind in ("quant", "national"):
            expect(page.locator("#mpDirection")).to_be_disabled()
        if kind == "quant":
            expect(page.locator("#mpCount")).to_contain_text("3161",timeout=30000)
            assert "不可识别" in page.locator("#mpStocks").inner_text()
    page.locator('[data-mp-type="main"]').click()
    expect(page.locator("#mpDirection")).to_be_enabled()
    page.locator("#mpSearch").fill("银行")
    with page.expect_response(lambda response: "/participants?" in response.url and "keyword=" in response.url):
        page.locator("#mpQuery button[type=submit]").click()
    expect(page.locator("#mpStocks")).to_contain_text("银行")
    page.set_viewport_size({"width": 1100, "height": 900})
    page.screenshot(path=str(OUT / "participants_1100.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    print(json.dumps({"browser_errors": errors, "checks": "tabs, filters, model, CSV, 6 participants, dates, industry search, 1100px layout", "screenshots": str(OUT)}, ensure_ascii=False))
    browser.close()
