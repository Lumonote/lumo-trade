"""Offline price chart smoke through real pages and SQLite read handlers."""
import json
import logging
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch

import pandas as pd
from playwright.sync_api import expect, sync_playwright
from robyn.testing import TestClient

from market_test_sandbox import offline_market_runtime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
URL = 'http://kronos.test'
BROWSER_CHANNEL = os.getenv('KRONOS_BROWSER_CHANNEL') or None
HISTORY_START = '2008-01-02'
HISTORY_END = '2008-01-04'


def bars(dates):
    close = [12 + index * .01 + math.sin(index * .2) for index in range(len(dates))]
    return pd.DataFrame({'timestamps': dates, 'open': [p - .1 for p in close], 'high': [p + .1 for p in close],
                         'low': [p - .2 for p in close], 'close': close, 'volume': [100] * len(dates)})


def exercise(client, stamps, prediction_dates):
    from analysis.short_term_screen import build_report
    from test_short_term_memberships import DAY, source

    codes = {'600001': '示例甲', '600002': '示例乙'}
    short_term = build_report(source(), DAY, DAY + 'T10:02', current_only=True)
    short_term.update(available=True, historical=True)
    snapshot = {'stocks': [{'code': '000001', 'name': '背景粒子'}, {'code': '600001', 'name': '示例甲', 'real_api_code': 'sh600001'}]}
    stubbed = {
        '/api/snapshot': snapshot,
        '/api/short-term/overview': short_term,
        '/api/short-term/orders': {'big_buys': [], 'big_sells': [], 'refreshing': False},
        '/api/stock-dashboard': {'opportunity': {'items': [
            {'code': code, 'name': name, 'has_local_kline': True, 'rank': rank}
            for rank, (code, name) in enumerate(codes.items(), 1)
        ]}},
    }
    kline_paths = []
    predict_requests = []

    def dispatch(route):
        request = route.request
        if not request.url.startswith(URL + '/'):
            return route.abort()
        parts = urlsplit(request.url)
        query = dict(parse_qsl(parts.query))
        if request.method == 'POST':
            if parts.path not in ('/api/predict', '/api/load-data'):
                return route.fulfill(status=405, json={})
            if parts.path == '/api/predict':
                predict_requests.append(request.post_data_json)
            response = client.post(parts.path, json_data=request.post_data_json)
        elif parts.path in stubbed:
            return route.fulfill(json=stubbed[parts.path])
        elif parts.path.startswith('/api/stock-context/'):
            return route.fulfill(json={'stock': {'code': parts.path[-6:], 'name': codes[parts.path[-6:]]}})
        elif parts.path.startswith('/api/stock-kline/') or parts.path.startswith('/static/') or parts.path in {
            '/', '/desktop/workbench', '/desktop/short_term', '/particles', '/prediction',
            '/api/available-models', '/api/model-status', '/api/data-files',
        }:
            if parts.path.startswith('/api/stock-kline/'):
                kline_paths.append((parts.path, query))
                if query.get('start') == '2008-02-01':
                    return route.fulfill(status=503, json={'error': '历史行情源暂时不可用'})
            response = client.get(parts.path, query_params=query)
        else:
            return route.fulfill(status=404, json={'error': 'outside test scope'})
        route.fulfill(status=response.status_code, body=response.content, content_type=response.headers.get('Content-Type'))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel=BROWSER_CHANNEL, args=['--enable-unsafe-swiftshader'])
        context = browser.new_context(viewport={'width': 1120, 'height': 900})
        context.route('**/*', dispatch)

        def visit(path):
            page = context.new_page()
            page.goto(URL + path, wait_until='networkidle')
            if page.locator('#riskDisclaimerModal').is_visible():
                page.locator('#riskDisclaimerAckBtn').click()
            return page

        def full_chart(page, selector, count=len(stamps)):
            chart = page.locator(selector)
            expect(chart.locator('.lp-meta')).to_contain_text(f'{count} / {count} 根')
            expect(chart.get_by_label('价格均线')).to_be_checked()
            expect(chart.get_by_label('成交量 / 均量')).to_be_checked()
            expect(chart.get_by_label('候选五浪')).to_be_checked()
            chart.get_by_label('前20根支撑 / 阻力').check()
            expect(chart.locator('.lp-legend')).to_contain_text('前20根高 / 低')
            return chart

        def search_history(chart, start=HISTORY_START, end=HISTORY_END):
            chart.get_by_label('K线起点').fill(start)
            chart.get_by_label('K线终点').fill(end)
            chart.get_by_role('button', name='查询').click()

        def historical_prices(chart, end=HISTORY_END):
            search_history(chart, end=end)
            expect(chart.locator('.lp-meta')).to_contain_text(f'{HISTORY_START} 至 {HISTORY_END} · 3 / ')
            chart.locator('svg').press('Home')
            expect(chart.locator('.lp-hover-date')).to_have_text(HISTORY_START)
            assert kline_paths[-1][1]['start'] == HISTORY_START
            if end:
                assert kline_paths[-1][1]['end'] == end

        page = visit('/desktop/workbench')
        page.evaluate("loadKline('600001','示例甲')")
        full_chart(page, '#klineChart')
        page.locator('#openKlineModalBtn').click()
        chart = full_chart(page, '#klineModalChart')
        page.evaluate("loadKline('600002','示例乙')")
        expect(page.locator('#klineTitle')).to_contain_text('600002')
        expect(page.locator('#klineModalTitle')).to_contain_text('600001')
        chart.get_by_role('button', name='60根').click()
        expect(chart.locator('.lp-meta')).to_contain_text(f'60 / {len(stamps)} 根')
        expect(chart.locator('.lp-readout')).to_contain_text('较前5根均量 1倍')
        chart.locator('svg').press('Home')
        expect(chart.locator('.lp-hover-date')).to_have_text(stamps[-60])
        chart.locator('svg').press('ArrowRight')
        expect(chart.locator('.lp-hover-date')).to_have_text(stamps[-59])
        chart.locator('svg').hover(position={'x': 75, 'y': 55})
        expect(chart.locator('.lp-hover-date')).to_be_visible()
        chart.get_by_label('K线起点').fill(stamps[9])
        chart.get_by_label('K线终点').fill(stamps[10])
        chart.get_by_role('button', name='查询').click()
        expect(chart.locator('.lp-meta')).to_contain_text(f'{stamps[9]} 至 {stamps[10]} · 2 / ')
        expect(page.locator('#klineModalRange')).to_contain_text('区间')
        historical_prices(chart)
        assert kline_paths[-1][0] == '/api/stock-kline/600001'
        search_history(chart, '2008-02-01', '2008-02-08')
        expect(chart.locator('.lp-range-status')).to_have_text('历史行情源暂时不可用')
        expect(chart.locator('.lp-meta')).to_contain_text(f'{HISTORY_START} 至 {HISTORY_END}')
        chart.get_by_role('button', name='全部').click()
        expect(chart.locator('.lp-meta')).to_contain_text(f'{len(stamps)} / {len(stamps)} 根')
        page.close()

        page = visit('/')
        historical_prices(full_chart(page, '#klineChart'))
        page.close()

        page = visit('/particles')
        expect(page.locator('#particlePicker select option')).to_have_count(1)
        page.locator('#particlePicker button').click()
        historical_prices(full_chart(page, '#particleChart'))
        page.close()

        page = visit('/desktop/short_term')
        page.locator('#stStockList [data-st-kind="stock"]').first.click()
        page.locator('[data-st-kline]').click()
        count = sum(stamp <= DAY for stamp in stamps)
        chart = full_chart(page, '#stKline', count)
        assert kline_paths[-1][1].get('end') == DAY
        historical_prices(chart)
        assert chart.get_by_label('K线终点').get_attribute('max') == DAY
        search_history(chart, HISTORY_START, '')
        expect(chart.locator('.lp-range-status')).to_be_hidden()
        assert kline_paths[-1][1].get('end') == DAY
        page.close()

        page = visit('/prediction')
        page.locator('#data-file-select').select_option('sqlite:ohlcv:600001:5m')
        page.locator('#load-data-btn').click()
        expect(page.locator('#data-timeframe')).to_have_text('5 分钟')
        page.locator('#lookback').fill('2')
        page.locator('#pred-len').fill('2')
        page.locator('#predict-btn').click()
        chart = full_chart(page, '#chart', 4)
        expect(chart.locator('.lp-readout')).to_contain_text('模型预测量')
        expect(chart.locator('.lp-readout')).to_contain_text('同期实测收')
        page.locator('#replayViews [data-view="actual"]').click()
        expect(chart.locator('.lp-readout')).to_contain_text('同期实测量')
        chart.locator('svg').press('End')
        expect(chart.locator('.lp-hover-date')).to_have_text(prediction_dates[-1].isoformat(sep=' '))
        assert chart.locator('[data-replay-region]').count() == 1
        assert predict_requests[0]['file_path'] == 'sqlite:ohlcv:600001:5m'

        page.locator('#data-stock-code').fill('600001')
        page.get_by_label('行情起点').fill('2007-12-31')
        page.get_by_label('行情终点').fill(HISTORY_END)
        page.locator('#load-data-btn').click()
        expect(page.locator('#data-time-range')).to_have_text(f'2007-12-31T00:00:00 to {HISTORY_END}T00:00:00')
        page.locator('#predict-btn').click()
        expect(chart.locator('.lp-meta')).to_contain_text('2007-12-31')
        assert predict_requests[-1]['start'] == '2007-12-31' and predict_requests[-1]['end'] == HISTORY_END
        page.close()
        context.close()
        browser.close()


if __name__ == '__main__':
    logging.getLogger('robyn.logger').setLevel(logging.WARNING)
    with offline_market_runtime():
        from data_store import connection, ohlcv_repo, price_bars_repo
        from webui.robyn_app import app, webui_core
        from webui.services import daily_history
        from test_short_term_memberships import DAY

        days = pd.bdate_range(end=pd.Timestamp(DAY) + pd.Timedelta(days=7), periods=650)
        dates = pd.to_datetime(['2026-01-09 11:25:00', '2026-01-09 11:30:00', '2026-01-09 13:05:00', '2026-01-12 09:35:00'])
        for code in ('600001', '600002'):
            ohlcv_repo.upsert_df(code, '1d', bars(days))
            old = bars(pd.bdate_range('2007-12-24', HISTORY_END)).rename(columns={'timestamps': 'date'})
            price_bars_repo.store('tencent', f'sh{code}', '1d', [
                {**record, 'date': record['date'].date().isoformat()} for record in old.to_dict('records')
            ])
        ohlcv_repo.upsert_df('600001', '5m', bars(dates))
        webui_core.MODEL_AVAILABLE = True
        webui_core.predictor = SimpleNamespace(predict=lambda **_: bars(dates[:2]), model=SimpleNamespace(), device='cpu')
        try:
            with patch.object(daily_history, 'fetch_tencent', return_value=[]), TestClient(app) as client:
                exercise(client, [day.date().isoformat() for day in days], dates)
        finally:
            connection.close_conn()
        print(json.dumps({'passed': True, 'pages': ['desktop', 'stock', 'particles', 'short-term', 'prediction']}))
