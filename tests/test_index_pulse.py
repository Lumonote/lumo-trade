"""四大指数指标与状态分级(纯函数)。

⚠️ 新浪日K 对指数不返回 amount(实测 None),量能一律用 volume。
"""
import pytest

from analysis import index_pulse as ip


def _bars(closes, volumes=None):
    vols = volumes or [1000.0] * len(closes)
    return [{"day": f"2026-06-{i + 1:02d}", "open": c, "high": c, "low": c,
             "close": c, "volume": v, "amount": None}
            for i, (c, v) in enumerate(zip(closes, vols))]


def test_index_metrics_computes_trailing_changes():
    m = ip.index_metrics(_bars([100.0] * 20 + [110.0]))
    assert m["chg_5d"] == pytest.approx(10.0)
    assert m["chg_20d"] == pytest.approx(10.0)


def test_index_metrics_computes_moving_averages():
    m = ip.index_metrics(_bars(list(range(1, 62))))
    assert m["ma20"] == pytest.approx(sum(range(42, 62)) / 20)
    assert m["ma60"] == pytest.approx(sum(range(2, 62)) / 60)


def test_index_metrics_above_ma20_flag():
    assert ip.index_metrics(_bars([100.0] * 20 + [200.0]))["above_ma20"] is True
    assert ip.index_metrics(_bars([100.0] * 20 + [50.0]))["above_ma20"] is False


def test_index_metrics_volume_ratio_uses_volume_not_amount():
    # 25 根:前 20 根量 100、后 5 根量 300。近5日均量 300;近20日均量
    # (15*100 + 5*300)/20 = 150 → 比值 2.0。amount 全为 None,不参与计算。
    bars = _bars([100.0] * 25, volumes=[100.0] * 20 + [300.0] * 5)
    assert ip.index_metrics(bars)["vol_ratio"] == pytest.approx(2.0)


def test_index_metrics_percentile_60d():
    m = ip.index_metrics(_bars(list(range(1, 62))))
    assert m["percentile_60d"] == pytest.approx(1.0)


def test_index_metrics_short_series_returns_nones_without_crash():
    m = ip.index_metrics(_bars([100.0, 101.0]))
    assert m["ma20"] is None and m["chg_20d"] is None
    assert m["close"] == pytest.approx(101.0)


def test_index_metrics_empty_bars():
    m = ip.index_metrics([])
    assert m["close"] is None and m["chg_5d"] is None


def test_classify_pulse_strong():
    assert ip.classify_pulse({"chg_5d": 4.0, "chg_20d": 8.0, "above_ma20": True,
                              "ma20_slope": 0.5, "percentile_60d": 0.95}) == "strong"


def test_classify_pulse_risk():
    assert ip.classify_pulse({"chg_5d": -5.0, "chg_20d": -9.0, "above_ma20": False,
                              "ma20_slope": -0.6, "percentile_60d": 0.05}) == "risk"


def test_classify_pulse_pullback():
    assert ip.classify_pulse({"chg_5d": 2.0, "chg_20d": -3.0, "above_ma20": False,
                              "ma20_slope": 0.1, "percentile_60d": 0.5}) == "pullback"


def test_classify_pulse_range_when_flat():
    assert ip.classify_pulse({"chg_5d": 0.2, "chg_20d": 0.3, "above_ma20": True,
                              "ma20_slope": 0.0, "percentile_60d": 0.5}) == "range"


def test_classify_pulse_unknown_without_data():
    assert ip.classify_pulse({}) == "unknown"
    assert ip.classify_pulse(None) == "unknown"
    assert ip.classify_pulse({"chg_5d": 5.0}) == "unknown"


def test_classify_pulse_uses_twenty_days_even_after_five_day_rebound():
    assert ip.classify_pulse({"chg_5d": 4, "chg_20d": -8, "above_ma20": False}) == "risk"
    assert ip.classify_pulse({"chg_5d": -4, "chg_20d": 8, "above_ma20": True}) == "strong"


def test_pulse_labels_cover_all_states():
    for state in ("strong", "mild_up", "range", "pullback", "risk", "unknown"):
        assert ip.PULSE_LABELS[state]


def test_style_axis_small_cap_leading():
    axis = ip.style_axis({"chg_20d": 1.0}, {"chg_20d": 6.0})
    assert axis["axis"] == "small"
    assert axis["spread"] == pytest.approx(5.0)


def test_style_axis_large_cap_leading():
    assert ip.style_axis({"chg_20d": 6.0}, {"chg_20d": 1.0})["axis"] == "large"


def test_style_axis_balanced_within_band():
    assert ip.style_axis({"chg_20d": 1.0}, {"chg_20d": 1.5})["axis"] == "balanced"


def test_style_axis_unknown_without_data():
    assert ip.style_axis({}, {})["axis"] == "unknown"


def test_overlay_realtime_updates_same_day_bar():
    bars = _bars([100.0, 101.0])
    bars[-1]["day"] = "2026-08-21"
    merged, live = ip.overlay_realtime(bars, {"close": 105.0, "pct_chg": 4.0,
                                              "date": "2026-08-21"})
    assert live is True
    assert merged[-1]["close"] == pytest.approx(105.0)
    assert len(merged) == len(bars)


def test_overlay_realtime_appends_new_day_bar():
    bars = _bars([100.0, 101.0])
    bars[-1]["day"] = "2026-08-20"
    merged, live = ip.overlay_realtime(bars, {"close": 105.0, "pct_chg": 4.0,
                                              "date": "2026-08-21"})
    assert live is True
    assert len(merged) == len(bars) + 1
    assert merged[-1]["day"] == "2026-08-21"


def test_overlay_realtime_without_quote_is_noop():
    bars = _bars([100.0, 101.0])
    merged, live = ip.overlay_realtime(bars, None)
    assert live is False
    assert merged == bars


def test_overlay_realtime_does_not_mutate_input():
    bars = _bars([100.0, 101.0])
    bars[-1]["day"] = "2026-08-21"
    ip.overlay_realtime(bars, {"close": 999.0, "pct_chg": 1.0, "date": "2026-08-21"})
    assert bars[-1]["close"] == pytest.approx(101.0)
