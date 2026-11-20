"""机会挖掘形态回测(item H):_opportunity_pattern_backtest 自回测打分逻辑。

用合成日K + 注入 fetch_klines 隔离网络;用 monkeypatch 的 opportunity_repo 提供 run/items。
"""
from __future__ import annotations

import datetime as dt
import math

import pytest


@pytest.fixture
def core(monkeypatch):
    import webui.core as core

    run = {"id": 5, "run_at": "2026-06-10T15:00:00", "run_date": "2026-06-10"}
    items = [
        {"code": "600000", "name": "浦发银行", "rating": "A", "total_score": 80},
        {"code": "000001", "name": "平安银行", "rating": "B", "total_score": 72},
    ]
    from data_store import opportunity_repo
    monkeypatch.setattr(opportunity_repo, "get_run", lambda rid: run if int(rid) == 5 else None)
    monkeypatch.setattr(opportunity_repo, "latest_run", lambda: run)
    monkeypatch.setattr(opportunity_repo, "list_runs", lambda **k: [run])
    monkeypatch.setattr(opportunity_repo, "items_for_run", lambda rid: items)
    return core


def _wave_klines(n=300, end='2026-06-10'):
    # 造一段有重复形态的正弦+上行趋势收盘序列,保证 scan_series 能命中并有前向收益
    last = dt.date.fromisoformat(end)
    return [{"day": (last - dt.timedelta(days=n-1-i)).isoformat(),
             "close": round(100 + 0.05 * i + 5 * math.sin(i / 6.0), 3)} for i in range(n)]


def test_opportunity_pattern_backtest_scores(core):
    fake = lambda code, cutoff, limit: _wave_klines(300)
    out = core._opportunity_pattern_backtest({"run_id": 5, "top_n": 2, "window_days": 20},
                                             fetch_klines=fake)
    assert out["ok"] is True
    assert out["total"] == 2
    assert out["scanned"] == 2
    assert len(out["rows"]) == 2
    r = out["rows"][0]
    assert r["code"] in {"600000", "000001"}
    assert "pattern_score" in r and "win_rate" in r and "avg_return" in r
    assert "horizons" in r and "10" in r["horizons"]
    # rows 按 pattern_score 降序(None 沉底)
    scores = [(x.get("pattern_score") if x.get("pattern_score") is not None else -1) for x in out["rows"]]
    assert scores == sorted(scores, reverse=True)


def test_opportunity_pattern_backtest_insufficient_history(core):
    fake = lambda code, cutoff, limit: _wave_klines(20)  # 太短
    out = core._opportunity_pattern_backtest({"run_id": 5, "top_n": 2, "window_days": 30},
                                             fetch_klines=fake)
    assert out["ok"] is True
    assert all(r["pattern_score"] is None for r in out["rows"])
    assert all('历史数据不足' in r['note'] and '20/52' in r['note'] for r in out['rows'])
    assert out["scanned"] == 0


def test_opportunity_pattern_backtest_no_run(core, monkeypatch):
    from data_store import opportunity_repo
    monkeypatch.setattr(opportunity_repo, "get_run", lambda rid: None)
    monkeypatch.setattr(opportunity_repo, "latest_run", lambda: None)
    out = core._opportunity_pattern_backtest({"top_n": 2}, fetch_klines=lambda *args: [])
    assert out["ok"] is False


def test_run_day_ignores_all_future_bars_and_invalid_revisions(core, monkeypatch):
    from webui.services import market_health_service
    monkeypatch.setattr(market_health_service, 'now', lambda: dt.datetime(2026, 7, 20))
    bars = _wave_klines(250, '2026-07-10')  # 220 bars through June 10, 30 after
    params = {'run_id': 5, 'window_days': 20}
    def evaluate(**options):
        return core._opportunity_pattern_backtest({**params, **options}, fetch_klines=lambda *args: reversed(bars))
    before = evaluate()
    assert before['mode'] == 'run_date' and before['data_as_of'] == before['cutoff'] == '2026-06-10'
    assert all(r['bar_count'] == 220 and r['sample_count'] > 0 for r in before['rows'])
    for i, row in enumerate(bars[220:]):
        row['close'] = 5000 / (i + 1)
    bars += [dict(day='2026-06-10', close=v) for v in [float('nan'), float('inf'), 0, -1]]
    bars += [bars[100], dict(day='not-a-date', close=10), dict(close=20)]
    assert evaluate() == before
    latest = evaluate(mode='latest')
    assert latest['cutoff'] == '2026-07-20' and latest['data_as_of'] == '2026-07-10'
    assert all(r['mode'] == 'latest' and r['bar_count'] == 250 for r in latest['rows'])


def test_old_batch_backfills_original_range_not_current_tail(core, monkeypatch):
    import pandas as pd
    from data_store import ohlcv_repo, ohlcv_fetch
    calls = []
    monkeypatch.setattr(ohlcv_repo, 'load_dataframe', lambda *a, **kw: pd.DataFrame())
    def fetch(code, start, end):
        calls.append((code, start, end))
        return pd.DataFrame(_wave_klines(300, '2020-05-08'))
    monkeypatch.setattr(ohlcv_fetch, 'fetch_daily', fetch)
    bars = core._opportunity_bars_until('600000', '2020-05-08')
    assert len(bars) == 250 and bars[-1][0] == '2020-05-08'
    assert calls[0][0] == '600000' and calls[0][1] < '20190508' and calls[0][2] == '20200508'
