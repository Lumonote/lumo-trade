"""大盘体检：数值边界、缺失口径、同日评分和反转确认。"""
import datetime as dt
import json
from pathlib import Path

import pytest

from analysis.market_health import (INDEXES, breadth_history, breadth_snapshot, build_health, confirmation,
                                    index_metrics, market_snapshot, rsi, signal_cards)
from webui.services.market_health_service import merge_indices, pick_sample


def bars(count=300, start=100, change=0):
    return [{"date": (dt.date(2025, 1, 1)+dt.timedelta(days=i)).isoformat(),
             "close": start+i*change, "open": start+i*change, "high": start+i*change+1,
             "low": start+i*change-1, "volume": 100} for i in range(count)]


def test_index_metrics_use_correct_windows_and_wilder_rsi():
    rising = bars(change=1)
    m = index_metrics("sh000001", rising)
    assert m["rsi14"] == 100
    assert m["ma250"] == pytest.approx(sum(100+i for i in range(50,300))/250)
    assert m["ret20"] == pytest.approx((399/379-1)*100)
    assert m["drawdown"] == 0 and m["down_days"] == 0
    assert m["volume250"] == 1 and m["volume5"] == 1
    assert m["trend"] == "多头排列"
    assert rsi([100]*30) == 50
    assert rsi([100,99]) is None
    assert index_metrics("sh000001", bars(5))["ma250"] is None


def test_threshold_uses_raw_value_not_rounded_report_label():
    card = signal_cards({"sz399006":{"dev250":-24.78}}, {}, {})["bottom"]
    assert next(r for r in card["rules"] if r["key"] == "cy_dev")["hit"] is False
    assert card["known"] == 1 and card["score"] is None
    exact = signal_cards({"sz399006":{"dev250":-25}}, {}, {})["bottom"]
    assert exact["rules"][0]["hit"] is True


def test_missing_data_is_unknown_and_does_not_become_cold_market():
    value = build_health({})
    assert value["temperature"]["score"] is None
    assert value["stage"] == "数据不足"
    assert value["signals"]["top"]["known"] == 0
    assert all(r["hit"] is None for r in value["signals"]["bottom"]["rules"])


def test_old_breadth_and_valuations_are_shown_but_excluded():
    rows = bars()
    state = {"bars":{s:rows for s in INDEXES},
             "market":{"as_of":"2020-01-01","values":{"up_ratio":0,"pb_median":1,"below_pb1_ratio":60}},
             "breadth":{"as_of":"2020-01-01","above_ma20":0,"above_ma250":0}}
    value = build_health(state)
    assert value["market"]["values"]["pb_median"] == 1
    assert value["temperature"]["score"] is None
    assert next(r for r in value["signals"]["bottom"]["rules"] if r["key"] == "pb")["hit"] is None


def test_breadth_uses_same_day_and_per_indicator_denominators():
    long = bars()
    short = bars(30)
    short = [{**r,"date":long[-30+i]["date"]} for i,r in enumerate(short)]
    result = breadth_snapshot({"long":long,"short":short,"old":bars(290)},long[-1]["date"])
    assert result["sample"] == 2
    assert result["denominators"]["above_ma250"] == 1
    assert result["denominators"]["above_ma20"] == 2
    assert result["above_ma250"] == 0  # 严格大于均线，横盘不算站上。


def test_market_valuation_only_uses_positive_valid_samples():
    rows = [{"trade":10,"changepercent":2,"pb":2,"per":20},
            {"trade":20,"changepercent":-2,"pb":-1,"per":0},
            {"trade":30,"changepercent":0,"pb":.5,"per":None},
            {"trade":0,"changepercent":5,"pb":1,"per":10}]
    m = market_snapshot(rows)
    assert m["total"] == 3 and m["pb_sample"] == 2 and m["pe_sample"] == 1
    assert m["pb_median"] == 1.25 and m["below_pb1_ratio"] == 50
    assert m["up_ratio"] == pytest.approx(100/3)


def test_backfilled_breadth_uses_real_history_and_one_complete_cohort():
    rising, falling = bars(change=1), bars(start=400,change=-1)
    days = [r["date"] for r in rising[-5:]]
    suspended = [r for r in rising if r["date"] != days[2]]
    observations = breadth_history({"r":rising,"f":falling,"suspended":suspended,"short":rising[-251:]},days)
    assert len(observations) == 5
    assert len({r["cohort"] for r in observations}) == 1
    assert all(r["sample"] == 2 and r["above_ma250"] == 50 for r in observations)
    assert all(r["newhigh60"] == 50 and r["newlow60"] == 50 for r in observations)
    assert all(set(r["denominators"].values()) == {2} for r in observations)
    assert confirmation({}, {"sh000001":rising}, {}, observations, days[-1])["bottom"]["items"][3]["hit"] is True


def test_confirmation_requires_real_five_days_same_cohort_and_next_day_hold():
    rows = bars()
    obs = [{"as_of":r["date"],"above_ma250":30,"method":"sample","cohort":"same"} for r in rows[-5:]]
    value = confirmation({}, {"sh000001":rows}, {}, obs, rows[-1]["date"])
    assert value["bottom"]["items"][3]["hit"] is True
    obs[-1]["cohort"] = "different"
    assert confirmation({}, {"sh000001":rows}, {}, obs, rows[-1]["date"])["bottom"]["items"][3]["hit"] is None
    rows[-2].update(close=103,volume=140)
    rows[-1]["close"] = 104
    assert confirmation({}, {"sh000001":rows}, {}, [], rows[-1]["date"])["bottom"]["items"][1]["hit"] is True
    rows[-1]["close"] = 102
    assert confirmation({}, {"sh000001":rows}, {}, [], rows[-1]["date"])["bottom"]["items"][1]["hit"] is False


def test_failed_or_regressed_indices_preserve_previous_values():
    old = {"sh000001":bars()}
    result, accepted = merge_indices(old,{"sh000001":bars(280),"sh000300":[]})
    assert accepted == 0 and result == old
    result["sh000001"][0]["close"] = 1
    assert old["sh000001"][0]["close"] == 100


def test_hot_market_can_be_hot_and_top_features_trigger():
    rows = bars(change=1)
    day = rows[-1]["date"]
    state = {"bars":{s:rows for s in INDEXES},
             "market":{"as_of":day,"values":{"up_ratio":98,"pb_median":5,"below_pb1_ratio":1}},
             "breadth":{"as_of":day,"above_ma20":98,"above_ma250":95,"dd52_median":-1,"newlow60":0}}
    result = build_health(state)
    assert result["temperature"]["score"] >= 80
    assert result["signals"]["top"]["hit"] >= 7
    assert "顶部" in result["stage"]


def test_packaged_baseline_is_self_contained():
    root = Path(__file__).resolve().parents[1]
    state = json.loads((root/"config/market_health_baseline.json").read_text())
    value = build_health(state)
    assert value["as_of"] == "2026-10-22"
    assert len([r for r in value["indices"] if r["available"]]) == 7
    assert value["breadth"]["sample"] >= 200
    assert value["breadth"]["newhigh60"] is not None
    assert len(value["observations"]) >= 5
    assert all(v["known"] == 5 for v in value["confirmation"].values())
    assert value["market"]["values"]["total"] > 5500
    assert value["temperature"]["score"] <= 40
    assert next(r for r in value["signals"]["bottom"]["rules"] if r["key"] == "cy_dev")["hit"] is False
    assert (root/"webui/static/research/ashare_bottom_20261022.html").exists()


def test_sampling_is_deterministic_and_stratified():
    rows = [{"symbol":f"sh{i:06d}","trade":10,"mktcap":i+1} for i in range(800)]
    a, b = pick_sample(rows), pick_sample(list(reversed(rows)))
    assert [r["symbol"] for r in a] == [r["symbol"] for r in b]
    assert len(a) == 400
    assert len({r["symbol"] for r in a}) == 400
