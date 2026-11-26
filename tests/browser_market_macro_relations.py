"""宏观关系视图离线验收：真实数据、独立筛选、研究跳转与移动端布局。"""
from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from market_browser_harness import HEALTH, URL, market_page


MACRO = HEALTH + "/macro"
QUERY = "**/api/market-health/macro?*"


with market_page() as (page, read, out):
    page.set_default_timeout(30000)
    page.emulate_media(reduced_motion="reduce")
    page.clock.install()
    report = read(MACRO)
    atlas = report["macro_relations"]
    relations = {row["key"]: row for row in atlas["rows"]}
    errors, queries = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: queries.append(request.url)
            if urlsplit(request.url).path == MACRO else None)
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(
        json={"job": {"status": "failed", "error": "离线验收不执行采集"}}))
    page.goto(URL + "/desktop/market_health?section=macro", wait_until="domcontentloaded")
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()

    expect(page.locator("#mmRelationList [data-mm-relation]")).to_have_count(len(relations))
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    expect(page.locator("#mmRelationContext")).to_contain_text(report["equity_name"])
    page.locator('#mmNav [data-mm-jump="mmRelations"]').click()
    expect(page.locator("#mmRelations")).to_be_in_viewport()

    # Browsing explanations and filtering the catalogue do not request new research.
    before = len(queries)
    page.locator('[data-mm-relation="pmi"]').click()
    detail = page.locator("#mmRelationDetail")
    expect(detail).to_contain_text(relations["pmi"]["mechanism"])
    expect(detail).to_contain_text(relations["pmi"]["up"])
    expect(detail).to_contain_text(relations["pmi"]["down"])
    expect(detail).to_contain_text(relations["pmi"]["sectors"][0])
    expect(detail).to_contain_text(relations["pmi"]["caveat"])
    expect(detail.locator('[data-mm-research="pmi"]')).to_have_count(1)
    page.locator("#mmRelationGroup").select_option("growth")
    expect(page.locator("#mmRelationList [data-mm-relation]")).to_have_count(
        sum(row["group"] == "growth" for row in relations.values()))
    page.locator("#mmRelationSearch").fill("CPI")
    matching = [row for row in relations.values() if row["group"] == "growth" and "cpi" in " ".join(
        [row["name"], row["mechanism"], row["up"], row["down"], *row["sectors"], *row["watch"]]).lower()]
    expect(page.locator("#mmRelationList [data-mm-relation]")).to_have_count(len(matching))
    expect(page.locator('[data-mm-relation="cpi"]')).to_be_visible()
    page.locator("#mmRelationSearch").fill("不存在的宏观指标 xyz987")
    expect(page.locator("#mmRelationList [data-mm-relation]")).to_have_count(0)
    expect(page.locator("#mmRelationList")).to_contain_text(re.compile("没有|暂无|无匹配"))
    page.locator("#mmRelationSearch").fill("")
    page.locator('[data-mm-relation="gdp"]').click()
    expect(detail).to_contain_text(relations["gdp"]["mechanism"])
    expect(detail.locator("[data-mm-research]")).to_have_count(0)
    expect(detail).to_contain_text("季频")
    assert len(queries) == before, queries[before:]

    # A relationship's action applies its own frequency, window and lag in both directions.
    page.locator("#mmRelationGroup").select_option("all")
    page.locator('[data-mm-relation="m2"]').click()
    page.locator('[data-mm-research="m2"]').click()
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    expect(page.locator("#mmAppliedQuery")).to_contain_text("M2")
    expect(page.locator("#mmFrequency")).to_have_value("monthly")
    expect(page.locator("#mmWindow")).to_have_value("60")
    expect(page.locator("#mmLag")).to_have_value("0")
    params = parse_qs(urlsplit(queries[-1]).query)
    assert params["frequency"] == ["monthly"] and params["window"] == ["60"]
    page.locator('[data-mm-relation="real10y"]').click()
    page.locator('[data-mm-research="real10y"]').click()
    expect(page.locator("#mmAppliedQuery")).to_contain_text("美国10年实际利率")
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    expect(page.locator("#mmFrequency")).to_have_value("daily")
    expect(page.locator("#mmWindow")).to_have_value("252")
    expect(page.locator("#mmLag")).to_have_value("0")

    # Context belongs to the last successful response, not an unsubmitted or failed draft.
    page.locator("#mmEquity").select_option("csi500")
    expect(page.locator("#mmRelationContext")).to_contain_text(report["equity_name"])
    page.route(QUERY, lambda route: route.fulfill(status=503, json={"error": "关系研究测试失败"}))
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmError")).to_contain_text("关系研究测试失败")
    expect(page.locator("#mmRelationContext")).to_contain_text(report["equity_name"])
    page.unroute(QUERY)
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmRelationContext")).to_contain_text("中证500")
    expect(page.locator("#mmQueryButton")).to_be_enabled()

    # Unknown observations and statistics stay unknown; external strings cannot inject markup.
    fixture = deepcopy(read(MACRO + "?equity=csi500"))
    hostile = '<img src=x onerror="alert(1)">'
    for row in fixture["briefing"]["indicators"]:
        if row["key"] == "pmi":
            row.update(value=None, change=None, date=None, period=None,
                       published_on=None, status="missing", status_label="未取得")
    for row in fixture["macro_relations"]["rows"]:
        if row["key"] == "pmi":
            row.update(mechanism=hostile, sources=[{"name": hostile, "url": "javascript:alert(1)"}])
            row["evidence"].update(rho=None, spearman=None, n=0, start=None, end=None,
                                   status="样本不足测试")
    page.route(QUERY, lambda route: route.fulfill(json=fixture))
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    page.locator('[data-mm-relation="pmi"]').click()
    expect(detail).to_contain_text("未取得")
    expect(detail).to_contain_text("样本不足测试")
    expect(detail).to_contain_text("—")
    expect(detail).to_contain_text(hostile)
    expect(detail.locator("img")).to_have_count(0)
    expect(detail.locator('a[href^="javascript:"]')).to_have_count(0)
    assert "NaN" not in detail.inner_text()

    # JSON export preserves the complete relation model, independently of local filters.
    expect(page.locator("#mmExport")).to_be_enabled()
    page.locator("#mmRelationGroup").select_option("growth")
    with page.expect_download() as event:
        page.locator("#mmExport").click()
    export = json.loads(event.value.path().read_text(encoding="utf-8"))
    assert len(export["macro_relations"]["rows"]) == len(relations)
    assert export["equity"] == "csi500"

    # Restore real content for layout and visual inspection.
    page.unroute(QUERY)
    page.locator("#mmRelationGroup").select_option("all")
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    page.locator('[data-mm-relation="pmi"]').click()
    page.locator("#mmRelations").scroll_into_view_if_needed()
    page.screenshot(path="/tmp/kronos-macro-relations-desktop.png")
    for width in (850, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        page.locator("#mmRelations").scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
        assert page.locator("#mmRelations").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), width
    page.screenshot(path="/tmp/kronos-macro-relations-mobile.png")
    assert not errors, errors
    print("Macro relation checks passed: complete catalogue, local filters, transmission details, "
          "research actions, applied index, missing data, escaping, export and responsive layout.")
