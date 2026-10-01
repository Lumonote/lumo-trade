from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import math

from analysis.operation_models import analyze_operations, confirmed_fractals, normalize_bars

NOW = datetime(2026, 10, 1, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def model_bars(n=240, end=date(2026, 9, 30)):
    dates = [end-timedelta(days=i) for i in range(n*2) if (end-timedelta(days=i)).weekday()<5][:n][::-1]
    rows = []
    for i, stamp in enumerate(dates):
        if i < n-31:
            close = 10+i*.05
        else:
            j = i-(n-31)
            close = 10+(n-31)*.05 + math.sin(j*1.4)*max(.04, .8*(1-j/30))
        rows.append({"date": stamp.isoformat(), "open": close-.03, "high": close+.15,
                     "low": close-.15, "close": close, "volume": 1000})
    ceiling=max(r["high"] for r in rows[-21:-1])
    rows[-1].update(open=ceiling-.04, close=ceiling+.05, high=ceiling+.2, low=ceiling-.2, volume=2000)
    return rows


def test_breakout_returns_ordered_structure_levels_and_distinct_model_checks():
    result=analyze_operations(model_bars(),now=NOW)
    assert result["available"]
    assert result["selected_model"] == "breakout"
    assert result["models"][0]["state"] == "triggered"
    assert result["stage"]["key"] == "advance"
    levels=result["levels"]
    assert 0 < levels["stop_loss"] < levels["entry_low"] < levels["entry_high"] < levels["target1"]
    assert result["fundamentals"]["available"] is False
    assert "CAN SLIM" in result["fundamentals"]["label"]


def test_weekly_stage_needs_completed_weeks_and_short_history_is_labeled_proxy():
    full=analyze_operations(model_bars(),now=NOW)
    short=analyze_operations(model_bars(90),now=NOW)
    assert "30周" in full["stage"]["basis"]
    assert "日线60日代理" in short["stage"]["basis"]


def test_intraday_breakout_does_not_backfill_a_confirmed_event():
    rows=model_bars(end=date(2026,10,1))
    now=NOW.replace(hour=14)
    result=analyze_operations(rows,now=now)
    assert result["provisional"]
    assert result["confirmed_as_of"] < "2026-10-01"
    assert not any(e["confirmed_at"]=="2026-10-01" for e in result["events"])
    assert all(m["state"] != "triggered" for m in result["models"])


def test_price_jump_or_missing_volume_cannot_be_a_confirmed_setup():
    rows=model_bars()
    for r in rows: r.pop("volume")
    result=analyze_operations(rows,now=NOW)
    assert result["available"]
    assert all(m["state"] != "triggered" for m in result["models"])
    rows=model_bars()
    for key in ("open","high","low","close"): rows[-1][key]*=2
    result=analyze_operations(rows,now=NOW)
    assert not result["available"] and "复权" in result["reason"]


def test_confirmed_fractals_keep_their_confirmation_time_when_more_bars_arrive():
    rows=model_bars()
    early=confirmed_fractals(rows[:100])
    later=confirmed_fractals(rows)
    assert all(p in later for p in early)
    assert all(p["confirmed_at"] > p["date"] for p in later)


def test_normalization_is_immutable_and_rejects_invalid_ohlc():
    rows=model_bars(80)
    original=deepcopy(rows)
    result=analyze_operations(rows,now=NOW)
    assert rows==original
    assert result["available"]
    rows[-1]["high"]=float("nan")
    assert len(normalize_bars(rows))==79
    rows[-2]["low"]=rows[-2]["high"]+1
    assert len(normalize_bars(rows))==78


def test_event_prefix_is_stable_and_uses_actual_confirmation_dates():
    rows=model_bars()
    early=analyze_operations(rows[:-5],now=NOW)
    later=analyze_operations(rows,now=NOW)
    assert all(e in later["events"] for e in early["events"])
    assert all(e["confirmed_at"]==e["date"] for e in later["events"])
    assert later["events"][-1]["date"]==rows[-1]["date"]


def test_future_bars_are_excluded_from_analysis():
    rows=model_bars()
    future={**rows[-1], "date":"2026-10-02"}
    assert analyze_operations(rows+[future],now=NOW)==analyze_operations(rows,now=NOW)


def test_support_break_is_visible_even_if_volume_is_missing():
    rows=model_bars()
    close=min(r["low"] for r in rows[-11:-1])-.5
    rows[-1].update(open=close+.03,high=close+.15,low=close-.15,close=close,volume=None)
    result=analyze_operations(rows,now=NOW)
    assert result["events"][-1]["kind"]=="risk"
    assert result["events"][-1]["confirmed_at"]==rows[-1]["date"]


def test_shortest_valid_history_also_rejects_price_jumps():
    rows=model_bars(60)
    for key in ("open","high","low","close"): rows[-1][key]*=2
    result=analyze_operations(rows,now=NOW)
    assert not result["available"] and "复权" in result["reason"]
