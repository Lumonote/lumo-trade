"""离线验收衍生品入口、数据时点、刷新与窄屏布局；不启动真实采集。"""
from copy import deepcopy
import re

from playwright.sync_api import expect

from market_browser_harness import HEALTH, URL, market_page


with market_page() as (page, read, out):
    from analysis.market_derivatives import build_derivatives

    errors, refreshes = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.emulate_media(reduced_motion="reduce")
    page.clock.install()
    page.set_default_timeout(20000)
    snapshot = deepcopy(read(HEALTH))
    snapshot["source"] = "browser_read_only"
    snapshot["fusion"]["updated_at"] = snapshot["as_of"]
    report = snapshot["fusion"]["derivatives"]
    assert len(report["products"]) == 4

    def refreshed(route):
        refreshes.append(route.request.post_data_json)
        snapshot["job"] = {"id": "derivatives-offline", "status": "finished", "progress": 100,
                           "result": {"notes": ["离线验收任务；未启动真实采集"]}}
        route.fulfill(status=202, json={"job": snapshot["job"]})

    page.route(re.compile(r"^http://kronos\.test/api/market-health(?:\?.*)?$"),
               lambda route: route.fulfill(json=snapshot))
    page.route(URL + HEALTH + "/refresh", refreshed)
    page.goto(URL + "/desktop/market_health?section=derivatives", wait_until="domcontentloaded")
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()

    panel = page.locator("#mhView-derivatives")
    expect(panel).to_be_visible()
    expect(page.locator("#mhTab-derivatives")).to_have_attribute("aria-selected", "true")
    expect(panel.locator("[data-md-product]")).to_have_count(4)
    expect(panel.locator(".md-summary")).to_contain_text(report["summary"]["label"])
    if report["summary"]["date"]:
        expect(panel.locator(".md-summary")).to_contain_text(report["summary"]["date"])
    for product in report["products"]:
        card = panel.locator(f'[data-md-product="{product["variety"]}"]')
        expect(card).to_be_visible()
        expect(card).to_contain_text(product["name"])
        expect(card.locator(".md-direction")).to_contain_text(product["direction_label"])
        if product["position_date"]:
            expect(card.locator(".md-observation")).to_contain_text(product["position_date"])
    assert "section=derivatives" in page.url

    # The new section participates in the existing relation network and can return
    # to its original tab without losing its content or selected URL state.
    page.locator("#mhTab-correlation").click()
    node = page.locator('#corrOverview [data-section="derivatives"]')
    expect(node).to_be_visible()
    node.click()
    expect(node).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#corrSectionContent #mhView-derivatives")).to_be_visible()
    assert "networkSection=derivatives" in page.url
    page.locator("#corrOpenSection").click()
    expect(panel).to_be_visible()
    expect(page.locator("#mhTab-derivatives")).to_have_attribute("aria-selected", "true")

    refreshes.clear()
    panel.locator("[data-mh-derivatives-refresh]").click()
    expect(page.locator("#mhJob")).to_contain_text("离线验收任务")
    assert refreshes == [{"mode": "cross"}]
    expect(panel.locator("[data-mh-derivatives-refresh]")).to_be_enabled()

    # Screenshots stay outside the harness temporary directory for inspection.
    panel.screenshot(path="/tmp/kronos-derivatives-desktop.png", animations="disabled")
    for width in (850, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        expect(panel).to_be_visible()
        assert panel.evaluate("node => node.scrollWidth <= node.clientWidth + 1"), f"衍生品模块在 {width}px 溢出"
    panel.screenshot(path="/tmp/kronos-derivatives-mobile.png", animations="disabled")

    # A separately refreshed futures cache must render on the regular poll even
    # if the enclosing cross-market snapshot has the same updated_at value.
    # Missing and stale evidence pass through the real API load and renderer.
    observation = snapshot["as_of"]
    empty = build_derivatives({}, observation, [observation])
    snapshot["fusion"]["derivatives"] = empty
    page.clock.fast_forward(31000)
    expect(panel.locator(".md-summary")).to_contain_text("同日证据不足")
    expect(panel.locator(".md-position-empty")).to_have_count(4)
    assert all(value == "—" for value in panel.locator(".md-change-heading strong").all_text_contents())

    stale = build_derivatives({"positions": [{"variety": "IF", "date": "2016-01-04",
        "aggregate": {"summary": {"long_total": 100, "short_total": 200,
                                    "long_chg": 5, "short_chg": -3}}}]}, observation, [observation])
    snapshot["fusion"]["derivatives"] = stale
    page.clock.fast_forward(31000)
    card = panel.locator('[data-md-product="IF"]')
    expect(card.locator(".md-direction")).to_have_text("历史观测")
    expect(card).to_contain_text("2016-01-04")
    expect(card).to_contain_text("不计方向")
    expect(panel.locator(".md-summary")).to_contain_text("同日证据不足")

    assert not errors, errors
    print("derivatives browser checks passed: direct tab, four products, relation network, refresh, missing/stale dates, responsive layout")
