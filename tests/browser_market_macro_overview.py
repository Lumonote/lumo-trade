"""宏观概览离线验收：真实基线、数据状态、查询上下文和局部刷新反馈。"""
from copy import deepcopy
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from market_browser_harness import HEALTH, URL, market_page


MACRO = HEALTH + "/macro"
QUERY = "**/api/market-health/macro?*"


def choose_text(page, selector, text):
    options = page.locator(selector + " option").evaluate_all(
        "nodes => nodes.map(node => ({value: node.value, text: node.textContent}))"
    )
    [option] = [row for row in options if text in row["text"]]
    page.locator(selector).select_option(option["value"])


def controlled_briefing(report):
    """Four deliberately distinct states; no live source or user data is changed."""
    briefing = deepcopy(report["briefing"])
    by_key = {row["key"]: row for row in briefing["indicators"]}
    selected = [by_key[key] for key in ("us10y", "pmi", "m2", "social")]
    for row in selected:
        row.update(status="fresh", status_label="观察时效内", date=report["today"],
                   previous_date="2026-11-24", age_days=0)
    selected[0].update(value=4.0, previous_value=4.125, change=-12.5, change_unit="bp",
                       change_label="较上一观测 -12.50 bp")
    selected[1].update(value=50.0, previous_value=50.0, change=0, change_unit="点",
                       change_label="较上期持平", period="2026-10")
    selected[2].update(value=7.5, previous_value=7.3, change=.2, change_unit="百分点",
                       change_label="较上期 +0.20 百分点", date="2020-01-31", period="2020-01",
                       status="stale", status_label="历史观测", age_days=2400)
    selected[3].update(value=None, previous_value=None, change=None, date=None, period=None,
                       published_on=None, status="missing", status_label="缺失", age_days=None,
                       change_label="无可比前值")
    briefing.update(indicators=selected, coverage={"total": 4, "available": 3, "fresh": 2,
                                                   "stale": 1, "missing": 1})
    return briefing


with market_page() as (page, read, out):
    page.set_default_timeout(20000)
    page.emulate_media(reduced_motion="reduce")
    page.clock.install()
    report = read(MACRO)
    errors, queries, refreshes = [], [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("request", lambda req: queries.append(req.url)
            if urlsplit(req.url).path == MACRO else None)
    page.route("**/api/market-health/refresh", lambda route: route.fulfill(
        json={"job": {"status": "failed", "error": "离线验收不执行采集"}}))
    page.goto(URL + "/desktop/market_health?section=macro", wait_until="domcontentloaded")
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()
    expect(page.locator("#mmOverview")).to_contain_text(report["briefing"]["summary"]["title"])
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(len(report["briefing"]["indicators"]))
    expect(page.locator("#mmDimensions")).to_contain_text(report["briefing"]["dimensions"][0]["name"])
    expect(page.locator("#mmOverview")).to_contain_text(report["briefing"]["assessed_on"])
    expect(page.locator("#mmAppliedQuery")).to_contain_text(report["equity_name"])

    # Grouping is entirely local, and a change of displayed cards does not rerun research.
    before = len(queries)
    external = next(row for row in report["briefing"]["groups"] if row["key"] == "external")
    page.locator("#mmMetricGroup").select_option(external["key"])
    expected = sum(row["group"] == external["key"] for row in report["briefing"]["indicators"])
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(expected)
    assert len(queries) == before
    choose_text(page, "#mmMetricGroup", "全部")
    page.locator("#mmMetricsToggle").click()
    expect(page.locator("#mmMetrics")).to_be_hidden()
    expect(page.locator("#mmMetricsToggle")).to_have_attribute("aria-expanded", "false")
    page.locator("#mmMetricsToggle").click()
    expect(page.locator("#mmMetrics")).to_be_visible()

    # Navigation uses the existing analysis sections, including when deeply scrolled.
    jump = page.locator("#mmNav [data-mm-jump]").nth(1)
    target = jump.get_attribute("data-mm-jump")
    jump.click()
    box = page.locator("#" + target).bounding_box()
    assert box and 0 <= box["y"] < 1000, box

    # Editing controls keeps an explicit applied context until a successful new query.
    original_title = page.locator("#mmTrend h3").inner_text()
    page.locator("#mmEquity").select_option("csi500")
    expect(page.locator("#mmAppliedQuery")).to_contain_text(re.compile("待分析|未应用|尚未"))
    expect(page.locator("#mmAppliedQuery")).to_contain_text(report["equity_name"])
    page.route(QUERY, lambda route: route.fulfill(status=503, json={"error": "模拟研究读取失败"}))
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmError")).to_contain_text("模拟研究读取失败")
    expect(page.locator("#mmTrend h3")).to_have_text(original_title)
    expect(page.locator("#mmAppliedQuery")).to_contain_text(report["equity_name"])
    page.unroute(QUERY)
    page.locator("#mmEquity").select_option(report["equity"])

    # A delayed old factor request must not replace a newer metric selection.
    held_queries = []
    def hold_old_query(route):
        if "factor=us10y" in route.request.url:
            held_queries.append(route)
        else:
            route.fulfill(json=read(route.request.url))
    page.route(QUERY, hold_old_query)
    page.locator('#mmMetrics [data-mm-factor="us10y"]').click()
    expect(page.locator("#mmQueryButton")).to_be_disabled()
    page.locator('#mmMetrics [data-mm-factor="m2"]').click()
    expect(page.locator("#mmTrend h3")).to_contain_text("M2")
    expect(page.locator("#mmFrequency")).to_have_value("monthly")
    assert len(held_queries) == 1
    with page.expect_response(lambda response: "factor=us10y" in response.url):
        held_queries[0].fulfill(json=read(held_queries[0].request.url))
    expect(page.locator("#mmTrend h3")).to_contain_text("M2")
    expect(page.locator("#mmAppliedQuery")).to_contain_text("M2")
    page.unroute(QUERY)
    page.locator("#mmFrequency").select_option(report["frequency"])
    page.locator("#mmFactor").select_option(report["factor"])

    # Real response shape with controlled stale, missing and zero-change observations.
    fixture = deepcopy(report)
    fixture["briefing"] = controlled_briefing(report)
    page.route(QUERY, lambda route: route.fulfill(json=fixture))
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(4)
    expect(page.locator('#mmMetrics [data-mm-factor="us10y"]')).to_contain_text("bp")
    expect(page.locator('#mmMetrics [data-mm-factor="m2"]')).to_contain_text("2020-01")
    expect(page.locator('#mmMetrics [data-mm-factor="social"]')).to_contain_text("—")
    assert "NaN" not in page.locator("#mmMetrics").inner_text()
    before = len(queries)
    choose_text(page, "#mmMetricStatus", "待更新")
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(2)
    page.locator("#mmMetricGroup").select_option("external")
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(0)
    expect(page.locator("#mmMetrics")).to_contain_text(re.compile("没有|暂无|无匹配"))
    assert len(queries) == before
    choose_text(page, "#mmMetricGroup", "全部")
    choose_text(page, "#mmMetricStatus", "全部")
    choose_text(page, "#mmMetricStatus", "有变化")
    keys = page.locator("#mmMetrics .mm-metric").evaluate_all(
        "nodes => nodes.map(node => node.dataset.mmFactor)"
    )
    assert "us10y" in keys and "pmi" not in keys and "social" not in keys, keys
    choose_text(page, "#mmMetricStatus", "全部")

    # A data refresh keeps the applied research, leaving unsubmitted controls as drafts.
    page.locator("#mmEquity").select_option("csi500")
    expect(page.locator("#mmAppliedQuery")).to_have_attribute("data-pending", "true")
    before_refresh = len(queries)

    # Hold the POST so repeated clicks cannot start another update.
    pending = []
    def refresh(route):
        refreshes.append(route.request.post_data_json)
        pending.append(route)
    page.route("**/api/market-health/macro/refresh", refresh)
    page.locator("#mmRefresh").click()
    expect(page.locator("#mmRefresh")).to_be_disabled()
    page.locator("#mmRefresh").evaluate("node => { node.click(); node.click(); }")
    assert len(refreshes) == 1
    job = {"id": "macro-offline", "status": "finished", "progress": 100,
           "result": {"as_of": report["as_of"], "notes": ["海外通道读取失败，保留原日期"]}}
    fixture["job"] = job
    pending[0].fulfill(status=202, json={"job": job})
    expect(page.locator("#mmJob")).to_contain_text("海外通道读取失败")
    expect(page.locator("#mmJob")).to_contain_text(re.compile("部分|提示|未完成"))
    expect(page.locator("#mmRefresh")).to_be_enabled()
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(4)
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    assert len(queries[before_refresh:]) == 1
    assert parse_qs(urlsplit(queries[-1]).query)["equity"] == [report["equity"]]
    expect(page.locator("#mmTrend h3")).to_have_text(original_title)
    expect(page.locator("#mmEquity")).to_have_value("csi500")
    expect(page.locator("#mmAppliedQuery")).to_have_attribute("data-pending", "true")

    # A refresh finishing during a manual query must not take its response slot.
    page.unroute(QUERY)
    manual_queries = []
    def hold_manual_query(route):
        equity = parse_qs(urlsplit(route.request.url).query).get("equity")
        if equity == ["csi500"] and not manual_queries:
            manual_queries.append(route)
        else:
            route.fulfill(json=read(route.request.url))
    page.route(QUERY, hold_manual_query)
    page.locator("#mmRefresh").click()
    expect(page.locator("#mmRefresh")).to_be_disabled()
    concurrent_start = len(queries)
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmQueryButton")).to_be_disabled()
    assert len(manual_queries) == 1
    pending[1].fulfill(status=202, json={"job": job})
    expect(page.locator("#mmRefresh")).to_be_enabled()
    assert len(queries[concurrent_start:]) == 1, queries[concurrent_start:]
    manual_queries[0].fulfill(json=read(manual_queries[0].request.url))
    expect(page.locator("#mmTrend h3")).to_contain_text("中证500")
    expect(page.locator("#mmQueryButton")).to_be_enabled()
    expect(page.locator("#mmAppliedQuery")).to_contain_text("中证500")
    expect(page.locator("#mmAppliedQuery")).to_have_attribute("data-pending", "false")
    assert all(parse_qs(urlsplit(url).query).get("equity") == ["csi500"]
               for url in queries[concurrent_start:]), queries[concurrent_start:]

    # Restore the real research before visual checks and save persistent screenshots.
    page.unroute(QUERY)
    page.locator("#mmQueryButton").click()
    expect(page.locator("#mmMetrics .mm-metric")).to_have_count(len(report["briefing"]["indicators"]))
    page.locator("#mmOverview").scroll_into_view_if_needed()
    page.screenshot(path="/tmp/kronos-macro-overview-desktop.png")
    for width in (850, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        page.locator("#mmOverview").scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
        assert page.locator("#mhView-macro").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), width
    page.screenshot(path="/tmp/kronos-macro-overview-mobile.png")
    assert not errors, errors
    print("Macro overview checks passed: baseline, local filters, navigation, applied context, stale/missing data, partial refresh, responsive layout.")
