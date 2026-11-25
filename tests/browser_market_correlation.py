"""Check the correlation network against the offline index snapshots and real GET handler."""
import re

from playwright.sync_api import expect

from market_browser_harness import HEALTH, URL, market_page


def check_network(page, network):
    chart = page.locator("#corrChart")
    for side in ("bull", "bear"):
        branch = network[side]
        nodes = chart.locator(f'.mcorr-node[data-side="{side}"]')
        expect(nodes.locator('xpath=self::*[@data-kind="root"]')).to_contain_text(branch["label"])
        for dimension in branch["children"]:
            expect(nodes.filter(has_text=dimension["label"]).first).to_be_visible()
            for signal in dimension["children"]:
                button = nodes.locator(f'xpath=self::*[@data-kind="signal" and @data-id="{signal["id"]}"]')
                expect(button).to_contain_text(signal["label"])
                button.click()
                expect(chart.locator(".mcorr-detail")).to_contain_text(signal["description"] or signal["label"])


def check_drawing(page, selector):
    canvas = page.locator(f"{selector} canvas")
    expect(canvas).to_be_visible()
    # Compare the composited canvas with and without its pixels. This works with
    # WebGL's discarded drawing buffer as well as a 2D context.
    labels_hidden = f"{selector} .mcorr-node {{ visibility:hidden !important; }}"
    rendered = canvas.screenshot(style=labels_hidden, animations="disabled")
    blank = canvas.screenshot(style=labels_hidden + f"{selector} canvas {{ opacity:0 !important; }}",
                              animations="disabled")
    assert rendered != blank, "The correlation canvas has no visible drawing"


with market_page() as (page, read, out):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.emulate_media(reduced_motion="reduce")
    shanghai = read(HEALTH + "/correlation?symbol=sh000001&format=all")
    assert shanghai["success"]
    target = URL + "/desktop/market_health?section=correlation"
    page.goto(target, wait_until="domcontentloaded")
    if page.locator("#riskDisclaimerModal").is_visible():
        page.locator("#riskDisclaimerAckBtn").click()

    summary = page.locator("#corrSummaryContent")
    meta = shanghai["metadata"]
    diff = meta["bull_signal_count"] - meta["bear_signal_count"]
    bias, color = ("看空", "negative") if diff < -2 else ("看多", "positive") if diff > 2 else ("中性", "neutral")
    expect(summary.locator("strong.mh-" + color)).to_contain_text(bias)
    expect(summary).to_contain_text(f"信号差 {diff:+d}" if diff else "信号差 0")
    assert "<span" not in summary.inner_text()
    check_network(page, shanghai["data"]["network"])
    expect(page.locator("#corrChart canvas")).to_have_attribute("data-engine", re.compile("^(three|canvas2d)$"))
    check_drawing(page, "#corrChart")
    page.locator("#corrVisualization").screenshot(path=str(out / "correlation-desktop.png"))

    page.locator("#corrFormat").select_option("text")
    expect(page.locator("#corrVisualization")).to_be_hidden()
    expect(page.locator("#corrTextContent")).to_have_text(shanghai["data"]["text"])
    shenzhen = read(HEALTH + "/correlation?symbol=sz399001&format=all")
    page.locator("#corrIndex").select_option("sz399001")
    expect(page.locator("#corrTextContent")).to_have_text(shenzhen["data"]["text"])
    page.locator("#corrFormat").select_option("visual")
    expect(page.locator("#corrTextTree")).to_be_hidden()
    check_network(page, shenzhen["data"]["network"])

    overview = page.locator("#corrOverview")
    check_drawing(page, "#corrOverview")
    evidence = page.locator("#corrSectionContent")
    panels = {
        "overview": "七维证据矩阵", "cross": "股指期货", "derivatives": "多空正在怎样变化", "sectors": "十日资金轮动路径",
        "limits": "完整日数据", "financing": "两融披露", "participants": "查询",
        "macro": "研究快照", "signals": "触发阈值", "indices": "指数指标矩阵",
        "breadth": "估值与交易结构", "confirmation": "底部反转", "history": "原报告历史条件对照",
        "method": "温度与特征分如何计算",
    }
    page.route("**/api/market-health/sectors/detail?*", lambda route: route.fulfill(json={
        "error": "离线测试不补采板块成分",
    }, status=404))
    for key in ("sectors", "limits", "participants", "financing"):
        page.route(URL + HEALTH + f"/{key}/refresh", lambda route: route.fulfill(status=202, json={
            "job": {"status": "queued"},
        }))
    expect(overview.locator("[data-section]")).to_have_count(len(panels) + 1)
    for key, copy in panels.items():
        node = overview.locator(f'[data-section="{key}"]')
        node.click()
        expect(node).to_have_attribute("aria-pressed", "true")
        expect(evidence.locator(f"#mhView-{key}")).to_be_visible()
        expect(evidence).to_contain_text(copy)

    overview.locator('[data-section="participants"]').click()
    expect(evidence.locator("[data-mp-type]")).to_have_count(6)
    evidence.locator('[data-mp-type="etf"]').click()
    expect(evidence.locator("#mpMeta")).to_contain_text("ETF")
    expect(evidence.locator("#mpStocks")).to_contain_text("申赎估算")

    overview.locator('[data-section="indices"]').click()
    expect(evidence.locator("#mhIndexSelect option")).to_have_count(7)
    evidence.locator("#mhIndexSelect").select_option("sh000688")
    expect(evidence).to_contain_text("科创50 · 量价与波段")
    page.locator("#corrOpenSection").click()
    expect(page.locator("#mhTab-indices")).to_have_attribute("aria-selected", "true")
    expect(evidence).to_be_empty()
    expect(page.locator("#mhView-indices")).to_contain_text("科创50 · 量价与波段")
    page.locator("#mhTab-correlation").click()
    expect(evidence.locator("#mhView-indices")).to_be_visible()

    page.add_init_script("""const original = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function(type, ...args) {
            if (type.startsWith('webgl')) return null;
            return original.call(this, type, ...args);
        };""")
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    expect(overview.locator("canvas")).to_have_attribute("data-engine", "canvas2d")
    check_drawing(page, "#corrOverview")
    expect(evidence.locator("#mhView-indices")).to_be_visible()
    overview.locator('[data-section="correlation"]').click()
    page.locator("#corrIndex").select_option("sh000001")
    expect(page.locator("#corrChart canvas")).to_have_attribute("data-engine", "canvas2d")
    check_network(page, shanghai["data"]["network"])
    check_drawing(page, "#corrChart")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.locator("#corrVisualization").screenshot(path=str(out / "correlation-mobile.png"))
    assert not errors, errors
    print("Correlation checks passed (15 sections, shared controls, signal bias, text tree, mobile Canvas).")
