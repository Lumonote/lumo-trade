"""广度估值离线验收：可比样本、分布缺口、独立轮询与视图状态。"""
from copy import deepcopy
import re
from urllib.parse import urlsplit

from playwright.sync_api import expect

from market_browser_harness import HEALTH, URL, market_page


with market_page() as (page, read, out):
    from analysis.market_breadth import build_breadth_report
    from analysis.market_health import market_snapshot

    page.set_default_timeout(20000)
    page.emulate_media(reduced_motion="reduce")
    page.clock.install()
    baseline = deepcopy(read(HEALTH))
    snapshot = deepcopy(baseline)
    snapshot["source"] = "browser_read_only"
    snapshot["fusion"]["updated_at"] = snapshot["as_of"]
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append(request.url)
            if urlsplit(request.url).path.startswith(HEALTH) else None)
    page.route(re.compile(r"^http://kronos\.test/api/market-health(?:\?.*)?$"),
               lambda route: route.fulfill(json=snapshot))
    page.route(URL + HEALTH + "/refresh", lambda route: route.fulfill(
        json={"job": {"status": "failed", "error": "离线验收不执行采集"}}))
    page.goto(URL + "/desktop/market_health?section=breadth", wait_until="domcontentloaded")
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()
    panel = page.locator("#mhView-breadth")
    expect(panel).to_be_visible()
    expect(page.locator("#mhBreadthOverview")).to_contain_text(snapshot["breadth_valuation"]["summary"]["title"])
    expect(page.locator("#mhValuationDistribution")).to_contain_text("此快照未保存估值分布")
    pb = snapshot["market"]["values"]["pb_median"]
    expect(page.locator("#mhValuationDistribution .mh-valuation-kpis")).to_contain_text(f"{pb:.3f}")
    expect(page.locator("#mhDailyForm")).to_be_visible()
    expect(page.locator("#mhIceChart svg")).to_have_count(1)

    # PB / PE and history scope change only the local presentation.
    before = len(requests)
    page.locator('[data-mh-valuation="pe"]').click()
    expect(page.locator('[data-mh-valuation="pe"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mhValuationDistribution")).to_contain_text("正 PE 中位数")
    page.locator('[data-mh-breadth-scope="all"]').click()
    expect(page.locator('[data-mh-breadth-scope="all"]')).to_have_attribute("aria-pressed", "true")
    assert len(requests) == before

    # A deterministic real report; distributions are calculated by production code.
    days = ["2026-11-23", "2026-11-24", "2026-11-25"]
    b = {"as_of": days[-1], "sample": 4, "cohort": "same-cohort", "method": "fixture-history",
         "above_ma20": 0, "above_ma60": None, "above_ma250": 25,
         "newlow60": 50, "newhigh60": 0, "dd52_median": -15,
         "denominators": {"above_ma20": 4, "above_ma60": 0, "above_ma250": 4,
                          "newlow60": 4, "newhigh60": 4}}
    observations = [dict(deepcopy(b), as_of=days[0], cohort="other-cohort"),
                    dict(deepcopy(b), as_of=days[1], above_ma20=25), deepcopy(b)]
    quotes = [{"symbol": f"sh60000{i}", "trade": 10, "changepercent": change,
               "pb": pb, "per": pe, "mktcap": 100+i, "turnoverratio": 1}
              for i, (pb, pe, change) in enumerate([(0.5, 10, -1), (1.5, 20, 0),
                                                    (4, None, 1), (-1, -5, 2)])]
    close = deepcopy(snapshot)
    close.update(as_of=days[-1], provisional=False, breadth=b, observations=observations,
                 market={"as_of": days[-1], "source": "isolated fixture", "values": market_snapshot(quotes)},
                 market_internals={"current": {}, "history": [], "intraday_history": []}, intraday=None)
    close["breadth_valuation"] = build_breadth_report(close, days)
    snapshot = close
    page.clock.fast_forward(31000)
    expect(page.locator("#mhBreadthOverview")).to_contain_text(days[-1])
    expect(page.locator('[data-mh-valuation="pe"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-mh-breadth-scope="all"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mhBreadthRecords tbody tr")).to_have_count(3)
    before = len(requests)
    page.locator('[data-mh-breadth-scope="cohort"]').click()
    expect(page.locator("#mhBreadthRecords tbody tr")).to_have_count(2)
    page.locator('[data-mh-valuation="pb"]').click()
    expect(page.locator(".mh-valuation-bucket")).to_have_count(5)
    assert len(requests) == before
    zero = page.locator('[data-mh-breadth-key="above_ma20"]')
    expect(zero).to_contain_text("0.0%")
    expect(zero).to_contain_text("实际分母 4 只")
    assert zero.locator(".mh-breadth-missing").count() == 0
    missing = page.locator('[data-mh-breadth-key="above_ma60"]')
    expect(missing.locator(".mh-breadth-bar-label strong")).to_have_text("—")
    expect(missing.locator(".mh-breadth-missing")).to_have_attribute("aria-label", "数据缺失，不按零显示")
    assert missing.locator(".mh-bar-track").get_attribute("style") is None
    zero_bucket = page.locator(".mh-valuation-bucket").filter(has_text="2–3")
    expect(zero_bucket).to_contain_text("0.0%")
    assert zero_bucket.locator(".mh-breadth-missing").count() == 0

    # A report-only revision must rerender even when all enclosing dates are unchanged.
    revised = deepcopy(close)
    revised["breadth"]["above_ma20"] = 75
    snapshot["breadth_valuation"] = build_breadth_report(revised, days)
    page.locator('[data-mh-valuation="pe"]').click()
    page.locator('[data-mh-breadth-scope="all"]').click()
    page.clock.fast_forward(31000)
    expect(page.locator('[data-mh-breadth-key="above_ma20"]')).to_contain_text("75.0%")
    expect(page.locator('[data-mh-valuation="pe"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-mh-breadth-scope="all"]')).to_have_attribute("aria-pressed", "true")

    # A distribution with zero valid samples has unknown percentages, not zero shares.
    no_valuations = deepcopy(close)
    no_valuations["market"]["values"] = market_snapshot([dict(row, pb=None, per=None) for row in quotes])
    snapshot["breadth_valuation"] = build_breadth_report(no_valuations, days)
    page.clock.fast_forward(31000)
    expect(page.locator(".mh-valuation-bucket .mh-breadth-missing")).to_have_count(5)
    assert all(value is None for value in page.locator(".mh-valuation-bucket .mh-bar-track").evaluate_all(
        "nodes => nodes.map(node => node.getAttribute('style'))"))

    old = deepcopy(close)
    old["breadth"]["as_of"] = days[0]
    old["market"]["as_of"] = days[0]
    snapshot["breadth_valuation"] = build_breadth_report(old, days)
    page.clock.fast_forward(31000)
    expect(page.locator("#mhBreadthOverview")).to_contain_text("历史")
    expect(page.locator("#mhBreadthTrend")).to_contain_text("不计算本日变化")

    # Actual view switching uses a closed report and a distinct intraday report.
    live = deepcopy(close)
    live.update(provisional=True, quote_at=days[-1]+"T14:00:00+08:00", confirmation_as_of=days[-2])
    live["breadth"]["provisional"] = True
    live["market"]["provisional"] = True
    live["breadth_valuation"] = build_breadth_report(live, days)
    snapshot["breadth_valuation"] = build_breadth_report(close, days)
    snapshot["intraday"] = live
    page.clock.fast_forward(31000)
    expect(page.locator("#mhBreadthOverview")).to_contain_text("盘中")
    page.locator("#mhCloseMode").click()
    expect(page.locator("#mhBreadthOverview")).to_contain_text("收盘观察")
    assert "盘中观测" not in page.locator("#mhBreadthTrend").inner_text()
    page.locator("#mhLatestMode").click()
    expect(page.locator("#mhBreadthOverview")).to_contain_text("盘中")
    expect(page.locator('[data-mh-valuation="pe"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-mh-breadth-scope="all"]')).to_have_attribute("aria-pressed", "true")

    # The existing daily query and ice chart remain usable through the new navigation.
    page.locator('[data-mh-breadth-jump="mhBreadthDaily"]').click()
    page.locator("#mhDailyStart").fill(baseline["as_of"])
    with page.expect_response(lambda response: "/daily-structure?" in response.url):
        page.locator('#mhDailyForm button[type="submit"]').click()
    expect(page.locator("#mhDailyStatus")).to_contain_text(baseline["as_of"])
    page.locator('[data-mh-breadth-jump="mhBreadthIce"]').click()
    ice = page.locator("#mhIceChart")
    expect(ice.locator("svg[tabindex='0']")).to_be_visible()
    ice.locator("svg[tabindex='0']").press("End")
    expect(ice.locator(".lp-readout")).not_to_be_empty()

    # Save the new view at several widths without altering global shell styles.
    page.locator('[data-mh-breadth-jump="mhBreadthOverview"]').click()
    page.screenshot(path="/tmp/kronos-breadth-desktop.png")
    for width in (1440, 850, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        page.locator("#mhBreadthOverview").scroll_into_view_if_needed()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
        assert panel.evaluate("node => node.scrollWidth <= node.clientWidth + 1"), width
    page.screenshot(path="/tmp/kronos-breadth-mobile.png")
    assert not errors, errors
    print("Breadth browser checks passed: baseline, comparable cohorts, valuation filters, missing/zero distribution, independent polling, view dates, daily/ice tools, responsive layout.")
