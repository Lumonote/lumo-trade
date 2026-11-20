"""Display real Robyn health payloads with an isolated SQLite observation gap."""
from playwright.sync_api import expect

from market_browser_harness import HEALTH, URL, market_page


with market_page() as (page, read, _):
    from data_store import kv_repo
    from webui.services import market_health_service, market_ice_service

    state = market_health_service.read_baseline()
    rows = state['bars']['sh000001']
    days = [r.get('date') or r['day'] for r in rows[-4:]]
    kv_repo.set_('market_health', 'snapshot', {**state, 'observations': []})
    for i, day in enumerate(days):
        if i == 2:
            continue
        kv_repo.set_(market_ice_service.NS, 'day:'+day, {'as_of': day, 'above_ma20': (10, 25, 30, 35)[i],
            'above_count': (20, 50, 60, 70)[i], 'denominators': {'above_ma20': 200},
            'cohort': 'fixture', 'method': 'fixture', 'source': 'SQLite test', 'provisional': False})
    assert read(HEALTH)['ice_coverage']['covered'] == 3

    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL+'/desktop/market_health?section=breadth', wait_until='domcontentloaded')
    if page.locator('#riskDisclaimerModal').is_visible():
        page.locator('#riskDisclaimerAckBtn').click()
    chart = page.locator('#mhIceChart')
    expect(chart.locator('[data-ice-point]')).to_have_count(3)
    expect(chart.locator('[data-ice-segment]')).to_have_count(1)

    chart.get_by_label('K线起点').fill(days[0])
    chart.get_by_label('K线终点').fill(days[-1])
    chart.get_by_role('button', name='查询').click()
    plot = chart.locator('svg[tabindex="0"]')
    plot.press('End')
    expect(chart.locator('.lp-readout')).to_contain_text(days[-1])
    expect(chart.locator('.lp-readout')).to_contain_text('有效 200 只')
    plot.press('ArrowLeft')
    expect(chart.locator('.lp-readout')).to_contain_text(days[-2])
    expect(chart.locator('.lp-readout')).to_contain_text('当日无有效广度观测')
    plot.hover(position={'x': 0, 'y': 25})
    expect(chart.locator('.lp-hover-date')).to_have_text(days[0])
    assert not errors, errors
    print('ice_chart_sqlite_points_range_keyboard_and_mouse_passed')
