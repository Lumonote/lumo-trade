"""Exercise the real workbench drawer with out-of-order synthetic job replies."""
from urllib.parse import urlsplit

from playwright.sync_api import expect
from market_browser_harness import URL, market_page


with market_page() as (page, _, _out):
    page.set_default_timeout(15000)
    errors, starts, held = [], [], {}
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/stock-dashboard*', lambda r: r.fulfill(json={'opportunity': {'items': []}, 'jobs': []}))
    page.route('**/api/opportunity-runs?*', lambda r: r.fulfill(json={'runs': [
        {'id': 101, 'run_at': '2026-09-01'}, {'id': 102, 'run_at': '2026-10-15'}]}))
    page.route('**/api/opportunity-canvas*', lambda r: r.fulfill(json={'canvas': {'nodes': [], 'edges': []}}))
    page.route('**/api/jobs', lambda r: r.fulfill(json={'jobs': []}))

    def start(route):
        assert route.request.method == 'POST'
        starts.append(route.request.post_data_json)
        route.fulfill(status=202, json={'job_id': f'fixture-{len(starts)}'})

    def finish(route, seq):
        params = starts[seq - 1]
        run = params.get('run_id', 103)
        day = {101: '2026-09-01', 102: '2026-10-15', 103: '2026-10-16'}[run]
        cutoff = day if params['mode'] == 'run_date' else '2026-11-19'
        result = {'ok': True, 'run': {'id': run, 'date': day}, 'mode': params['mode'], 'cutoff': cutoff,
                  'data_as_of': cutoff, 'scanned': 1, 'total': 3, 'rows': [
                      {'code': '000001', 'name': f'结果-{seq}', 'data_as_of': cutoff, 'bar_count': 219},
                      {'code': '000002', 'name': '短历史', 'data_as_of': '2026-08-28', 'bar_count': 8},
                      {'code': '000003', 'name': '无报价', 'data_as_of': None, 'bar_count': 0}]}
        route.fulfill(json={'job': {'id': f'fixture-{seq}', 'status': 'finished',
                                    'logs': [f'任务日志-{seq}'], 'result': result}})

    def poll(route):
        seq = int(urlsplit(route.request.url).path.rsplit('-', 1)[1])
        if seq in (1, 3, 5):
            held[seq] = route
        else:
            finish(route, seq)

    page.route('**/api/opportunity/pattern-backtest', start)
    page.route('**/api/jobs/fixture-*', poll)
    page.goto(URL + '/desktop/workbench', wait_until='domcontentloaded')
    if page.locator('#riskDisclaimerModal').is_visible():
        page.locator('#riskDisclaimerAckBtn').click()
    picker = page.locator('#opportunityCanvasDateSelect')
    drawer = page.locator('#opportunityDataDrawer')
    menu = page.locator('.opportunity-data-menu-btn').first
    close = drawer.locator('[data-drawer-close]')
    pattern = drawer.locator('[data-opportunity-data-tab=pattern]')
    all_tab = drawer.locator('[data-opportunity-data-tab=all]')
    mode = drawer.locator('.opp-pattern-mode')
    button = drawer.locator('.opp-pattern-run')
    summary = drawer.locator('#opportunityDataSummary')
    rows = drawer.locator('.opportunity-pattern-table tbody tr')
    # Observe the production poll, including its last await, before asserting a late reply was ignored.
    page.evaluate('''() => {
      const poll = pollJob; window.finishedPolls = []; window.summaryWrites = [];
      pollJob = async (...args) => {try {return await poll(...args)} finally {finishedPolls.push(args[0])}};
      new MutationObserver(records => records.forEach(r => r.addedNodes.forEach(n => summaryWrites.push(n.textContent))))
        .observe(document.querySelector('#opportunityDataSummary'), {childList:true});
    }''')

    def launch(seq, run, basis):
        with page.expect_request(lambda req: req.url.endswith(f'/api/jobs/fixture-{seq}')):
            button.click()
        assert starts[-1] == {'top_n': 20, 'mode': basis, **({'run_id': run} if run else {})}

    def release(seq):
        finish(held.pop(seq), seq)
        page.wait_for_function('id => finishedPolls.includes(id)', arg=f'fixture-{seq}')
        assert f'任务日志-{seq}' not in page.evaluate('summaryWrites')

    def check_rows(seq, end):
        expect(rows).to_have_count(3)
        for name, last, count in [(f'结果-{seq}', end, 219), ('短历史', '2026-08-28', 8), ('无报价', '无有效日线', 0)]:
            cells = rows.filter(has_text=name).locator('td')
            expect(cells.nth(3)).to_have_text(last)
            expect(cells.nth(4)).to_have_text(str(count))

    picker.select_option('101')
    menu.click()
    pattern.click()
    expect(mode).to_have_value('run_date')
    launch(1, 101, 'run_date')
    mode.select_option('latest')
    launch(2, 101, 'latest')
    check_rows(2, '2026-11-19')
    release(1)
    check_rows(2, '2026-11-19')
    expect(summary).to_contain_text('最新行情重评 · 批次 2026-09-01')

    launch(3, 101, 'latest')
    close.click()
    picker.select_option('102')
    menu.click()
    expect(rows).to_have_count(0)
    launch(4, 102, 'latest')
    check_rows(4, '2026-11-19')
    release(3)
    check_rows(4, '2026-11-19')
    expect(summary).to_contain_text('批次 2026-10-15')

    launch(5, 102, 'latest')
    all_tab.click()
    release(5)
    expect(summary).to_contain_text('全部数据')
    expect(rows).to_have_count(0)
    close.click()
    picker.select_option('')
    menu.click()
    pattern.click()
    mode.select_option('run_date')
    launch(6, None, 'run_date')
    check_rows(6, '2026-10-16')
    all_tab.click()
    pattern.click()
    expect(summary).to_contain_text('最近一次挖掘批次')
    expect(rows).to_have_count(0)
    assert len(starts) == 6 and not held and not errors, (starts, held, errors)
    print('Backtest drawer passed: 6 fixture jobs, stale logs/results, batch, mode, tab, history dates and counts.')
