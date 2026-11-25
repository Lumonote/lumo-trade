"""广度与估值的分母、相邻交易日、缺失及盘中边界。"""
from copy import deepcopy
import json

import pytest

from analysis.market_breadth import build_breadth_report
from analysis.market_health import market_snapshot


DAY, PREVIOUS = "2026-11-25", "2026-11-24"
KEYS = ("above_ma20", "above_ma60", "above_ma250", "newhigh60", "newlow60")


def breadth(day=DAY, value=60, **extra):
    return {"as_of": day, "sample": 400, "cohort": "basket-a", "method": "same-method",
            "denominators": {key: 400 for key in KEYS}, **dict.fromkeys(KEYS, value), **extra}


def health():
    return {"as_of": DAY, "breadth": breadth(), "observations": [breadth(PREVIOUS, 55)],
            "market": {"as_of": DAY, "values": market_snapshot([
                {"trade": 10, "changepercent": 2, "mktcap": 100, "pb": 1, "per": 10},
                {"trade": 20, "changepercent": -1, "mktcap": 200, "pb": 2, "per": 20},
                {"trade": 30, "changepercent": 0, "mktcap": None, "pb": 3, "per": 30}])}}


def test_distribution_includes_only_finite_positive_values_with_exact_bucket_edges():
    pb = [.5, 1, 2, 3, 5, 10, 0, -1, None, "--", float("nan"), float("inf")]
    pe = [1, 15, 30, 50, 100, 200, 0, -2, None, "--", float("nan"), float("inf")]
    rows = [{"trade": 10, "pb": a, "per": b} for a, b in zip(pb, pe)]
    rows += [{"trade": 0, "pb": 500, "per": 500}, {"trade": -1}, {"trade": None}, None, "bad", 4]
    snapshot = market_snapshot(rows)
    assert snapshot["total"] == 12
    for kind in ("pb", "pe"):
        distribution = snapshot["valuation_distribution"][kind]
        assert distribution["sample"] == 6 and distribution["total"] == 12
        assert distribution["coverage_pct"] == 50
        assert distribution["missing"] == 4 and distribution["nonpositive"] == 2
        assert [b["count"] for b in distribution["buckets"]] == [1, 1, 1, 1, 2]
        assert sum(b["pct"] for b in distribution["buckets"]) == pytest.approx(100)
        assert distribution["quantiles"]["p50"] == snapshot[f"{kind}_median"]
    assert snapshot["valuation_distribution"]["pb"]["quantiles"] == {"p10": .75, "p25": 1.25, "p50": 2.5, "p75": 4.5, "p90": 7.5}
    json.dumps(snapshot, allow_nan=False)


def test_zero_and_single_valuation_samples_have_honest_quantiles_and_coverage():
    empty = market_snapshot([])
    assert empty["valuation_distribution"]["pb"]["coverage_pct"] is None
    assert all(v is None for v in empty["valuation_distribution"]["pb"]["quantiles"].values())
    report = market_snapshot([{"trade": 1, "pb": 2, "per": -1}, {"trade": 2, "pb": None}])
    assert set(report["valuation_distribution"]["pb"]["quantiles"].values()) == {2}
    assert report["valuation_distribution"]["pe"]["sample"] == 0
    assert report["valuation_distribution"]["pe"]["coverage_pct"] == 0
    assert all(b["pct"] is None for b in report["valuation_distribution"]["pe"]["buckets"])


def test_boolean_valuation_is_missing_and_invalid_return_is_not_weighted():
    report = market_snapshot([{"trade": 1, "pb": True, "per": False, "changepercent": -101, "mktcap": 100},
                              {"trade": 2, "pb": 2, "per": 10, "changepercent": 3, "mktcap": 200}])
    assert report["pb_sample"] == report["pe_sample"] == 1
    assert report["pb_median"] == 2 and report["pe_median"] == 10
    assert report["valuation_distribution"]["pb"]["missing"] == 1
    assert report["change_sample"] == report["weighted_sample"] == 1
    assert report["chg_median"] == report["mv_weighted_chg"] == 3


def test_same_day_internals_preferred_but_its_denominator_is_explicit():
    data = health()
    data["market_internals"] = {"current": {"as_of": DAY, "eligible": 8, "directional": 6,
        "up": 4, "down": 2, "flat": 2, "median_return_pct": 1, "source": "daily"}}
    report = build_breadth_report(data, [PREVIOUS, DAY])
    participation = report["participation"]
    assert participation["sample"] == 8 and participation["up_ratio"] == pytest.approx(100*4/6)
    assert participation["net_advances"] == 2 and participation["label"] == "上涨家数占优"
    assert "平盘不进入该分母" in participation["note"]
    assert participation["weighted_sample"] == 2 and participation["weighted_return"] == 0
    assert participation["weighted_gap_pp"] == -1 and "样本及权重不同" in participation["note"]


def test_older_or_invalid_internals_falls_back_to_same_day_market():
    data = health()
    data["market_internals"] = {"current": {"as_of": PREVIOUS, "eligible": 8, "up": 7, "down": 1, "flat": 0}}
    participation = build_breadth_report(data)["participation"]
    assert participation["sample"] == 3 and participation["up_ratio"] == pytest.approx(100/3)
    assert "分母包含平盘" in participation["note"]
    data["market_internals"]["current"]["as_of"] = DAY
    data["market_internals"]["current"]["eligible"] = 1
    assert build_breadth_report(data)["participation"]["sample"] == 3


def test_participation_does_not_mix_weighted_return_from_a_different_day():
    data = health()
    data["market"]["as_of"] = PREVIOUS
    data["market_internals"] = {"current": {"as_of": DAY, "eligible": 1, "up": 0, "down": 0, "flat": 1,
                                           "median_return_pct": 0}}
    participation = build_breadth_report(data)["participation"]
    assert participation["median_return"] == 0 and participation["up_ratio"] is None
    assert participation["weighted_return"] is None and participation["weighted_gap_pp"] is None


def test_same_cohort_method_denominator_and_previous_actual_session_are_required():
    data = health()
    report = build_breadth_report(data, ["2026-11-23", PREVIOUS, DAY])
    trend = report["trend"]
    assert trend["previous_date"] == PREVIOUS
    assert all(r["comparable"] and r["change_pp"] == 5 for r in trend["rows"])
    assert trend["label"] == "短长趋势同步占优"
    data["observations"][0]["denominators"]["above_ma60"] = 399
    trend = build_breadth_report(data, [PREVIOUS, DAY])["trend"]
    row = next(r for r in trend["rows"] if r["key"] == "above_ma60")
    assert not row["comparable"] and row["change_pp"] is None and row["previous_value"] is None
    assert "分母不同" in trend["comparison_note"]
    assert trend["rows"][0]["change_pp"] == 5


@pytest.mark.parametrize("field,value,phrase", [("cohort", "basket-b", "成分组"), ("method", "different", "口径"),
                                                ("provisional", True, "只有盘中")])
def test_cross_cohort_method_and_provisional_history_are_excluded(field, value, phrase):
    data = health()
    data["observations"][0][field] = value
    trend = build_breadth_report(data, [PREVIOUS, DAY])["trend"]
    assert all(not row["comparable"] and row["change_pp"] is None for row in trend["rows"])
    assert phrase in trend["comparison_note"]


def test_comparison_never_skips_a_missing_session_or_invents_calendar():
    data = health()
    data["observations"][0]["as_of"] = "2026-11-23"
    trend = build_breadth_report(data, ["2026-11-23", PREVIOUS, DAY])["trend"]
    assert "不跨缺口" in trend["comparison_note"]
    assert all(row["change_pp"] is None for row in trend["rows"])
    for calendar in (None, [PREVIOUS]):
        trend = build_breadth_report(data, calendar)["trend"]
        assert trend["previous_date"] is None
        assert "交易日历" in trend["comparison_note"]


def test_weekend_gap_uses_actual_calendar_and_zero_changes_remain_zero():
    data = health()
    data["as_of"] = data["breadth"]["as_of"] = "2026-11-23"
    data["observations"][0].update(as_of="2026-11-20", **dict.fromkeys(KEYS, 60))
    trend = build_breadth_report(data, ["2026-11-20", "2026-11-23"])["trend"]
    assert all(row["change_pp"] == 0 for row in trend["rows"])


def test_current_intraday_can_compare_previous_close_but_is_labeled():
    data = health()
    data["provisional"] = data["breadth"]["provisional"] = True
    data["observations"].append(breadth(PREVIOUS, 99, provisional=True))
    report = build_breadth_report(data, [PREVIOUS, DAY])
    assert report["provisional"] and report["trend"]["provisional"]
    assert all(row["change_pp"] == 5 for row in report["trend"]["rows"])
    assert "盘中抽样" in report["trend"]["comparison_note"]


def test_intraday_without_comparable_close_does_not_claim_a_comparison():
    data = health()
    data["breadth"]["provisional"] = True
    for calendar in (None, [DAY]):
        trend = build_breadth_report(data, calendar)["trend"]
        assert "尚非收盘确认" in trend["comparison_note"]
        assert "变化对比" not in trend["comparison_note"]
        assert all(row["change_pp"] is None for row in trend["rows"])
    data["as_of"] = "2026-11-26"
    trend = build_breadth_report(data, [PREVIOUS, DAY, "2026-11-26"])["trend"]
    assert trend["status"] == "stale"
    assert "变化对比" not in trend["comparison_note"]


def test_stale_rows_remain_historical_and_future_rows_cannot_supply_current_values():
    data = health()
    data["as_of"] = "2026-11-26"
    report = build_breadth_report(data, [PREVIOUS, DAY, "2026-11-26"])
    assert all(report[key]["status"] == "stale" for key in ("trend", "participation", "valuation"))
    assert report["trend"]["tone"] == report["participation"]["tone"] == "unavailable"
    assert report["valuation"]["pb"]["median"] == 2
    assert all(row["change_pp"] is None for row in report["trend"]["rows"])
    data["as_of"] = PREVIOUS
    report = build_breadth_report(data, [PREVIOUS, DAY])
    assert all(report[key]["status"] == "missing" for key in ("trend", "participation", "valuation"))
    assert report["valuation"]["pb"]["median"] is None
    assert report["trend"]["rows"][0]["value"] is None


def test_legacy_distribution_missing_stays_null_and_input_is_not_mutated():
    data = health()
    del data["market"]["values"]["valuation_distribution"]
    del data["market"]["values"]["weighted_sample"]
    original = deepcopy(data)
    report = build_breadth_report(data, [PREVIOUS, DAY])
    assert report["valuation"]["pb"]["median"] == 2
    assert report["valuation"]["pb"]["distribution"] is None
    assert report["participation"]["weighted_sample"] is None
    assert "旧快照" in report["valuation"]["note"]
    assert data == original
    json.dumps(report, allow_nan=False)


def test_source_and_capture_time_preserve_each_module_observation():
    data = health()
    data["market"].update(source="market source", captured_at=DAY + "T15:10:00")
    data["breadth"].update(source="sample source", captured_at=DAY + "T15:20:00")
    data["market_internals"] = {"current": {"as_of": DAY, "eligible": 1, "up": 1, "down": 0, "flat": 0,
                                           "source": "daily source", "captured_at": DAY + "T15:30:00"}}
    report = build_breadth_report(data, [PREVIOUS, DAY])
    assert report["participation"]["source"] == "daily source" and report["participation"]["captured_at"].endswith("15:30:00")
    assert report["trend"]["source"] == "sample source" and report["trend"]["captured_at"].endswith("15:20:00")
    assert report["valuation"]["source"] == "market source" and report["valuation"]["captured_at"].endswith("15:10:00")


def test_missing_values_and_zero_denominators_are_not_fabricated_as_zero():
    data = {"as_of": DAY, "breadth": breadth(above_ma20=None, denominators=dict.fromkeys(KEYS, 0))}
    report = build_breadth_report(data, [PREVIOUS, DAY])
    assert report["trend"]["status"] == "missing"
    assert all(row["value"] is None and row["change_pp"] is None for row in report["trend"]["rows"])
    assert report["participation"]["sample"] is None and report["valuation"]["pb"]["median"] is None
    assert report["summary"]["title"] == "广度与估值证据待更新"
