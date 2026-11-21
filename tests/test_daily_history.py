import datetime as dt
from unittest.mock import Mock

from data_store import calendar_repo, price_bars_repo
from webui.services import daily_history as service


def candle(day, close=10):
    return {'date': str(day), 'open': close, 'high': close+1, 'low': close-1, 'close': close, 'volume': 2}


def test_older_range_is_persisted_and_overlapping_queries_only_get_unchecked_days(isolated_price_db, monkeypatch):
    fetch = Mock(side_effect=lambda symbol, start, end: [candle(start), candle(end)])
    monkeypatch.setattr(service, 'fetch_tencent', fetch)
    rows, src, error = service.load_range('sh000001', '2008-01-02', '2008-01-11')
    assert src == 'sqlite:tencent' and not error
    assert [r['date'] for r in rows] == ['2008-01-02', '2008-01-11']
    assert service.load_range('sh000001', '2008-01-02', '2008-01-11')[0] == rows
    more, _, _ = service.load_range('sh000001', '2008-01-02', '2008-01-18')
    assert more[-1]['date'] == '2008-01-18'
    assert fetch.call_args_list[-1].args == ('sh000001', '2008-01-12', '2008-01-18')
    assert len(fetch.call_args_list) == 2
    assert price_bars_repo.read('tencent', 'sh000001') == more


def test_partial_range_survives_download_error_and_resumes_same_window(isolated_price_db, monkeypatch):
    fetch = Mock(side_effect=[[candle('2008-01-02')], OSError('offline'), [candle('2009-12-31')]])
    monkeypatch.setattr(service, 'fetch_tencent', fetch)
    args = ('sh000001', '2008-01-01', '2009-12-31')
    rows, source, message = service.load_range(*args)
    assert source == 'sqlite:tencent' and rows[0]['date'] == '2008-01-02' and '待补' in message
    rows, _, message = service.load_range(*args)
    assert message is None
    assert [r['date'] for r in rows] == ['2008-01-02', '2009-12-31']
    assert fetch.call_args_list[1] == fetch.call_args_list[2]


def test_empty_window_never_returns_warmup_as_valid_data_and_can_be_retried(isolated_price_db, monkeypatch):
    before = candle('2007-12-31')
    fetch = Mock(return_value=[before])
    monkeypatch.setattr(service, 'fetch_tencent', fetch)
    args = ('sh600000', '2008-01-01', '2008-01-06')
    rows, source, note = service.load_range(*args, warmup=1)
    assert not rows and source == 'sqlite' and '未取得' in note
    assert price_bars_repo.read('tencent', 'sh600000')[0]['date'] == '2007-12-31'
    service.load_range(*args, warmup=1)
    fetch.assert_called_once()


def test_complete_sqlite_range_stays_offline_and_excludes_provisional(isolated_price_db, monkeypatch):
    date = dt.datetime(2008, 1, 3, 11)
    calendar_repo.upsert(['20080101'], is_open=0)
    calendar_repo.upsert(['20080102', '20080103'])
    price_bars_repo.store('sina', 'sh600000', '1d', [candle('2008-01-02'), candle('2008-01-03')], provisional_day='2008-01-03')
    fetch = Mock(side_effect=AssertionError('complete SQLite range must stay offline'))
    monkeypatch.setattr(service, 'fetch_tencent', fetch)
    rows, source, _ = service.load_range('sh600000', '2008-01-01', '2008-01-03', now=date)
    assert source == 'sqlite:sina' and [r['date'] for r in rows] == ['2008-01-02']
    fetch.assert_not_called()
