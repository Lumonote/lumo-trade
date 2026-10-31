"""在独立运行的WebUI上验证真实两融/资金API与浏览器交互，不启动数据采集。"""
import json
import csv
from pathlib import Path
import sys
import urllib.request
from urllib.parse import parse_qs, urlsplit

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
quant_snapshot = json.load(opener.open(URL + "/api/market-health/participants?participant=quant", timeout=30))

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(headless=True)
    except Error:
        browser = p.chromium.launch(headless=True, channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(60000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("requestfailed", lambda request: print("request_failed",request.url,request.failure,flush=True) if "/api/market-health/participants" in request.url else None)
    # 现有大盘自动行情和自动全量采集使用只读快照，新增API保持真实请求。
    page.route("**/api/market-health?*", lambda route: route.fulfill(json=snapshot))
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(json={"job": {"status": "failed", "error": "浏览器检查不启动全量采集"}}))
    page.goto(URL + "/desktop/market_health?section=financing", wait_until="domcontentloaded", timeout=60000)
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()
    expect(page.locator("#mfCount")).to_contain_text(str(len(finance_snapshot["rows"])), timeout=30000)
    assert page.locator("#mfStocks tbody tr").count() == min(50,len(finance_snapshot["rows"]))
    # 全量原始数值排序：升/降序、跨页、CSV顺序及缺失值置后。
    def check_stock_sort(container, column, records, key, code_attribute):
        header=page.locator(f"#{container} th").nth(column)
        valid=[r for r in records if r.get(key) is not None]
        for direction in ("ascending","descending"):
            header.click()
            expect(header).to_have_attribute("aria-sort",direction)
            expected=sorted(valid,key=lambda r:r[key],reverse=direction=="descending")+[r for r in records if r.get(key) is None]
            expect(page.locator(f"#{container} [{code_attribute}]").first).to_have_attribute(code_attribute,expected[0]["code"] if container=="mfStocks" else "0")
            if container=="mfStocks":
                assert page.locator(f"#{container} [data-mf-code]").first.get_attribute("data-mf-code")==expected[0]["code"]
            else:
                assert page.locator(f"#{container} [data-mp-row]").first.inner_text()==expected[0]["name"]
        if container=="mfStocks" and len(expected)>50:
            page.locator('[data-mc-page="mf:2"]').click()
            assert page.locator("#mfStocks [data-mf-code]").first.get_attribute("data-mf-code")==expected[50]["code"]
            header.click()  # 新排序从第一页开始。
            expect(page.locator("#mfPage")).to_contain_text("1/")
            header.click()
        return expected
    finance_order=check_stock_sort("mfStocks",1,finance_snapshot["rows"],"rzye","data-mf-code")
    with page.expect_download() as sorted_download:
        page.locator("#mfCsv").click()
    exported=list(csv.DictReader(Path(sorted_download.value.path()).read_text(encoding="utf-8-sig").splitlines()))
    assert [r["code"] for r in exported]==[r["code"] for r in finance_order]
    check_stock_sort("mfStocks",9,finance_snapshot["rows"],"model_ratio","data-mf-code")
    with page.expect_download() as missing_download:
        page.locator("#mfCsv").click()
    exported=list(csv.DictReader(Path(missing_download.value.path()).read_text(encoding="utf-8-sig").splitlines()))
    numeric=[float(r["model_ratio"]) for r in exported if r["model_ratio"]]
    assert numeric==sorted(numeric,reverse=True)
    if any(not r["model_ratio"] for r in exported):
        assert not exported[-1]["model_ratio"]
    stress_header=page.locator("#mfStress th").first
    stress_header.click();stress_header.click()
    assert "20%" in page.locator("#mfStress tbody tr").first.inner_text()
    sector_header=page.locator("#mfSectors th").nth(1)
    sector_header.click();sector_header.click()
    assert int(page.locator("#mfSectors tbody tr").first.locator("td").nth(1).inner_text())==max(r["count"] for r in finance_snapshot["sectors"])
    page.locator("#mfSort").select_option("model_ratio")
    expect(page.locator("#mfStocks th").nth(9)).to_have_attribute("aria-sort","none")
    page.screenshot(path=str(OUT / "financing.png"), full_page=True)
    page.screenshot(path=str(OUT / "financing_top.png"))
    print("financing_sorting_passed",flush=True)
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
    check_stock_sort("mpStocks",1,capital_snapshot["rows"],"net","data-mp-row")
    matrix_header=page.locator("#mpMatrix th").nth(1)
    matrix_header.click();matrix_header.click()
    matrix_max=max((r for r in capital_snapshot["matrix"] if r.get("main") is not None),key=lambda r:r["main"])
    assert page.locator("#mpMatrix tbody tr").first.locator("td").first.inner_text()==matrix_max["name"]
    # 明细卡片不能继承大盘云图的深色底，标签与数值都要达到可读对比度。
    page.locator('#mpStocks [data-mp-row]').first.click()
    expect(page.locator('#mpDetail')).to_be_visible()
    expect(page.locator('#mpDetail')).to_contain_text('股票 / 基金代码')
    expect(page.locator('#mpDetail')).to_contain_text('窗口完整性')
    contrast = page.locator('#mpDetail .mc-detail-grid').evaluate('''grid => {
      const luminance = color => {
        const values = color.match(/[\\d.]+/g).slice(0,3).map(Number).map(v => {
          const s=v/255;return s<=0.04045?s/12.92:((s+0.055)/1.055)**2.4;
        });return values[0]*0.2126+values[1]*0.7152+values[2]*0.0722;
      };
      return Array.from(grid.querySelectorAll('dt,dd')).map(el=>{
        const text=luminance(getComputedStyle(el).color),bg=luminance(getComputedStyle(el.parentElement).backgroundColor);
        return (Math.max(text,bg)+0.05)/(Math.min(text,bg)+0.05);
      });
    }''')
    assert contrast and min(contrast)>=4.5, contrast
    page.locator('#mpDetail').screenshot(path=str(OUT / 'participants_detail.png'))
    def check_sector_click(selector, expected_total):
        target=page.locator(selector).first
        sector=target.get_attribute('data-mp-sector')
        if sector is None:
            sector=target.locator('..').get_attribute('data-mp-sector')
        with page.expect_response(lambda response:'/participants?' in response.url and parse_qs(urlsplit(response.url).query).get('sector')==[sector]) as matched:
            target.click()
        result=matched.value.json()
        assert result['rows'] and all(r['sector']==sector for r in result['rows'])
        expect(page.locator('#mpSector')).to_have_value(sector)
        expect(page.locator('#mpCount')).to_contain_text('行业 / 跟踪指数：'+sector,timeout=30000)
        expect(page.locator('#mpDetail')).to_be_hidden()
        expect(page.locator('#mpStocks')).to_be_in_viewport(ratio=.1,timeout=10000)
        with page.expect_response(lambda response:'/participants?' in response.url and not parse_qs(urlsplit(response.url).query).get('sector')):
            page.locator('#mpSector').select_option('')
        expect(page.locator('#mpCount')).to_contain_text(f'当前 {expected_total} 项',timeout=30000)
    check_sector_click('#mpSectors button[data-mp-sector]',len(capital_snapshot['rows']))
    check_sector_click('#mpSectors .mc-bar[data-mp-sector]>div',len(capital_snapshot['rows']))
    check_sector_click('#mpMatrix button[data-mp-sector]',len(capital_snapshot['rows']))
    page.screenshot(path=str(OUT / "participants.png"), full_page=True)
    page.locator('[data-mp-days="5"]').click()
    expect(page.locator("#mpMeta")).to_contain_text("2026-10-26", timeout=30000)
    with page.expect_response(lambda response:'/participants?' in response.url and not parse_qs(urlsplit(response.url).query).get('start')):
        page.locator('#mpLatest').click()
    expect(page.locator('#mpStart')).to_have_value('')
    expect(page.locator('#mpEnd')).to_have_value('')
    for kind in ("institution", "etf", "national", "quant", "sm"):
        with page.expect_response(lambda response: "/participants?" in response.url and "participant=" + kind in response.url) as changed:
            page.locator(f'[data-mp-type="{kind}"]').click()
        current=changed.value.json()
        expect(page.locator('#mpCount')).to_contain_text(f"当前 {len(current['rows'])} 项",timeout=30000)
        expect(page.locator('#mpSector')).to_have_value('')
        expect(page.locator(f'[data-mp-type="{kind}"]')).to_have_attribute("aria-pressed", "true")
        print("participant",kind,flush=True)
        if kind == "etf":
            expect(page.locator("#mpStocks")).to_contain_text("申赎估算")
            assert page.locator("#mpStocks tbody tr").count() == 50
            etfs=current
            check_stock_sort("mpStocks",5,etfs["rows"],"nav","data-mp-row")
        if kind in ("quant", "national"):
            expect(page.locator("#mpDirection")).to_be_disabled()
        if kind == "quant":
            expect(page.locator("#mpCount")).to_contain_text(str(len(quant_snapshot["rows"])),timeout=30000)
            assert "不可识别" in page.locator("#mpStocks").inner_text()
            assert not any(r["name"] in (r["code"],r["code"].split(".")[0]) for r in quant_snapshot["rows"])
        check_sector_click('#mpSectors button[data-mp-sector]',len(current['rows']))
    page.locator('[data-mp-type="main"]').click()
    expect(page.locator("#mpDirection")).to_be_enabled()
    page.locator("#mpSearch").fill("银行")
    with page.expect_response(lambda response: "/participants?" in response.url and "keyword=" in response.url):
        page.locator("#mpQuery button[type=submit]").click()
    expect(page.locator("#mpStocks")).to_contain_text("银行")
    # 使用会覆盖NaN分类的宽泛关键字，逐个验证大资金、ETF与散户真实请求。
    for kind,word in (("main","华"),("etf","ETF"),("sm","华")):
        print("search",kind,word,flush=True)
        with page.expect_response(lambda response:"/participants?" in response.url and "participant="+kind in response.url):
            page.locator(f'[data-mp-type="{kind}"]').click()
        page.locator("#mpSearch").fill(word)
        with page.expect_response(lambda response:"/participants?" in response.url and "keyword=" in response.url) as search:
            page.locator("#mpQuery button[type=submit]").click()
        result=search.value.json()
        assert search.value.status==200 and result["rows"]
        expect(page.locator("#mpError")).to_be_hidden()
        assert all(r["name"] and r["sector"] for r in result["rows"])
    # 文本表头可用键盘排序，刷新/筛选后保留该排序。
    name_header=page.locator("#mpStocks th").first
    name_header.locator("button").focus()
    name_header.locator("button").press("Enter")
    expect(name_header).to_have_attribute("aria-sort","ascending")
    page.locator("#mpDirection").select_option("sell")
    expect(name_header).to_have_attribute("aria-sort","ascending")
    page.locator("#mpSort").select_option("desc")
    expect(name_header).to_have_attribute("aria-sort","none")
    # 切换顶部六类资金，日期、搜索、行业、方向与排序共用同一组条件。
    page.locator('#mpStart').fill('2026-07-01')
    page.locator('#mpEnd').fill(capital_snapshot['as_of'])
    page.locator('#mpSearch').fill('银行')
    sector=page.locator('#mpSectors button[data-mp-sector]').first.get_attribute('data-mp-sector')
    page.locator('#mpSector').select_option(sector)
    expect(page.locator('#mpCount')).to_contain_text('行业 / 跟踪指数：'+sector,timeout=30000)
    page.locator('#mpDirection').select_option('sell')
    page.locator('#mpSort').select_option('asc')
    saved={key:page.locator('#'+key).input_value() for key in ('mpStart','mpEnd','mpSearch','mpSector','mpDirection','mpSort')}
    for kind in ('main','institution','etf','national','quant','sm','main'):
        with page.expect_response(lambda response:'/participants?' in response.url and parse_qs(urlsplit(response.url).query).get('participant')==[kind]) as switched:
            page.locator(f'[data-mp-type="{kind}"]').click()
        response=switched.value
        params=parse_qs(urlsplit(response.url).query)
        for key,field in (('start','mpStart'),('end','mpEnd'),('keyword','mpSearch'),('sector','mpSector')):
            assert params[key]==[saved[field]],(kind,key,params)
        result=response.json()
        expect(page.locator('#mpMeta')).to_contain_text('2026-07-01',timeout=30000)
        expect(page.locator('#mpRankTitle')).to_contain_text({'main':'大资金','institution':'大机构','etf':'ETF','national':'国家队','quant':'量化资金','sm':'散户'}[kind])
        expected=len(result['rows']) if kind in ('national','quant') else sum(r['net'] is not None and r['net']<0 for r in result['rows'])
        expect(page.locator('#mpCount')).to_contain_text(f'当前 {expected} 项',timeout=30000)
        for key,value in saved.items():
            expect(page.locator('#'+key)).to_have_value(value)
        expect(page.locator('#mpError')).to_be_hidden()
    page.set_viewport_size({"width": 1100, "height": 900})
    page.screenshot(path=str(OUT / "participants_1100.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not errors, errors
    print(json.dumps({"browser_errors": errors, "checks": "tabs, filters, global numeric/text header sorting, pagination, sorted CSV, nulls last, model, 6 participants, real NaN-cache search, names/industries, 1100px layout", "screenshots": str(OUT)}, ensure_ascii=False))
    browser.close()
