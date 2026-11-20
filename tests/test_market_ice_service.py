import datetime as dt
from unittest.mock import Mock

import pytest

from data_store import kv_repo, price_bars_repo
from webui.services import market_health_cross_service, market_health_service, market_ice_service


def trading_days(count):
    first = dt.date(2025, 1, 6)
    return [(first+dt.timedelta(days=i)).isoformat() for i in range(count//5*7+7)
            if (first+dt.timedelta(days=i)).weekday()<5][:count]


def test_single_missing_close_backfills_and_becomes_visible(isolated_price_db, monkeypatch):
    days = trading_days(21)
    target = days[-1]
    symbols = [f'sz000{i:03}' for i in range(1, 201)]
    rows = [{'date': d, 'close': 12 if d == target else 10} for d in days]
    for symbol in symbols:
        price_bars_repo.store('tencent', symbol, '1d', rows if symbol != symbols[-1] else rows[:-1])
    baseline = {'bars': {'sh000001': rows}, 'breadth': {'sample_symbols': symbols}}
    kv_repo.set_('market_health', 'snapshot', baseline)
    monkeypatch.setattr(market_health_cross_service, 'read_cached', lambda *args: {})
    source = Mock(return_value=rows[-1:])
    monkeypatch.setattr(market_ice_service, 'fetch_tencent', source)
    monkeypatch.setattr(market_ice_service.tushare_client, 'available', lambda: False)

    assert not market_health_service.query()['ice_history']
    repaired, points, _ = market_ice_service.fill_block(days, [target], symbols, target)

    assert repaired == [target]
    source.assert_called_once_with(symbols[-1], target, target)
    assert points[target]['denominators'] == {'above_ma20': 200}
    assert price_bars_repo.read('tencent', symbols[-1], start=target, end=target)[0]['close'] == 12
    value = market_health_service.query()
    assert value['ice_history'] == [points[target]]
    assert value['ice_coverage']['covered'] == 1
    assert not value['observations']  # The repair does not contribute to five-day confirmation.


def test_first_visible_day_reads_its_prior_19_benchmark_sessions(isolated_price_db, monkeypatch):
    dates = trading_days(25)
    price_bars_repo.store('tencent', 'sh000001', '1d', [{'date': d, 'close': 3000} for d in dates[:19]])
    source = Mock(side_effect=AssertionError('benchmark warmup is already in SQLite'))
    monkeypatch.setattr(market_ice_service, 'fetch_tencent', source)
    wanted, days = market_ice_service._window(dates[19:], [dates[19]])
    assert wanted == [dates[19]]
    assert days == dates[:20]
    source.assert_not_called()


def test_full_refresh_reuses_saved_daily_closes(isolated_price_db, monkeypatch):
    dates = trading_days(255)
    symbol = 'sh600001'
    price_bars_repo.store('sina', symbol, '1d', [{'date': d, 'close': 10} for d in dates])
    source = Mock(side_effect=AssertionError('no network needed'))
    monkeypatch.setattr(market_health_service, '_fetch_json', source)
    progress = Mock()
    with pytest.raises(ValueError, match='样本不足200只'):
        market_health_service.fetch_breadth([{'symbol': symbol}], dates[-1], progress, [symbol], dates)
    progress.assert_called_once_with(1, 1)
    source.assert_not_called()
