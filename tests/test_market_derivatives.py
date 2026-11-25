"""衍生品多空概览：披露日对齐、缺值、对冲与同合约比较边界。"""
from copy import deepcopy

import pytest

from analysis.market_derivatives import build_derivatives


AS_OF = "2026-11-25"
CALENDAR = ["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", AS_OF]


def position(variety="IF", date=AS_OF, **values):
    return {"variety": variety, "date": date, "source": "cffex", "aggregate": {"summary": {
        "long_total": 100, "short_total": 200, "long_chg": 10, "short_chg": -20, **values}}}


def quote(variety="IF", date=AS_OF, **values):
    return {"variety": variety, "main": {"symbol": variety + "2612", "tradedate": date,
            "price": 4000, "position": 1500, "verified_basis_pct": -.5, **values}}


def report(positions=(), products=(), previous=(), **values):
    return build_derivatives({"positions": list(positions), "quotes": {"products": list(products)},
                              "previous_quotes": {"products": list(previous)}, **values}, AS_OF, CALENDAR)


def test_missing_data_keeps_four_products_and_does_not_invent_neutral_positions():
    result = report()
    assert [p["variety"] for p in result["products"]] == ["IF", "IH", "IC", "IM"]
    assert result["summary"]["eligible"] == 0 and result["summary"]["missing"] == 4
    assert result["summary"]["tone"] == "unavailable"
    assert all(p["direction"] == "unavailable" and p["long"] is None for p in result["products"])
    assert "期权" in "".join(result["notes"]) and "未接入" in "".join(result["notes"])


def test_net_short_balance_and_discount_do_not_override_bullish_net_change():
    result = report([position(), position("IH")], [quote()])
    assert result["summary"]["tone"] == "bullish"
    product = result["products"][0]
    assert product["net"] == -100 and product["net_change"] == 30
    assert product["direction"] == "bullish" and product["basis_pct"] == -.5
    assert product["action_label"] == "多单增仓、空单减仓"
    assert "套期保值" in "".join(result["notes"])


@pytest.mark.parametrize("field", ["long_total", "short_total", "long_chg", "short_chg"])
def test_missing_position_input_is_not_zero_or_direction(field):
    result = report([position(**{field: None})])
    product = result["products"][0]
    assert product["direction"] == "unavailable"
    assert result["summary"]["eligible"] == 0


def test_report_does_not_trust_precomputed_signal_and_net_fields():
    result = report([position(long_chg=-20, short_chg=10, net=999, net_chg=999, signal="偏多")])
    product = result["products"][0]
    assert product["direction"] == "bearish" and product["net_change"] == -30
    assert product["net"] == -100


def test_zero_changes_are_neutral_and_double_increases_preserve_both_actions():
    neutral = report([position(long_chg=0, short_chg=0), position("IH", long_chg=0, short_chg=0)])
    assert neutral["summary"]["tone"] == "neutral" and neutral["summary"]["neutral"] == 2
    assert neutral["products"][0]["net_change"] == 0
    both = report([position(long_chg=50, short_chg=30)])
    assert both["products"][0]["action_label"] == "多单增仓、空单增仓"
    assert both["products"][0]["direction"] == "bullish"


def test_total_only_combines_latest_common_position_date():
    result = report([position(), position("IH", date="2026-11-24"), position("IC", date="2026-11-24")])
    assert result["summary"]["date"] == AS_OF and result["summary"]["eligible"] == 1
    assert result["summary"]["tone"] == "unavailable" and result["summary"]["bullish"] == 1
    assert result["products"][1]["direction"] == "bullish"
    assert "不合并计数" in "".join(result["notes"])


def test_lag_uses_previous_trading_day_including_weekend():
    result = build_derivatives({"positions": [position(date="2026-11-20"), position("IH", date="2026-11-20")]},
                               "2026-11-23", CALENDAR)
    assert result["summary"]["tone"] == "bullish"
    assert result["products"][0]["position_status"] == "lagged"
    assert "上一交易日" in "".join(result["notes"])


def test_future_rows_do_not_leak_and_stale_positions_keep_values_without_direction():
    result = report([position(date="2026-11-26"), position(date="2026-11-23")],
                    [quote(date="2026-11-26")])
    product = result["products"][0]
    assert product["position_date"] == "2026-11-23" and product["position_status"] == "stale"
    assert product["long"] == 100 and product["direction"] == "unavailable"
    assert product["quote_date"] is None and product["basis_pct"] is None


def test_mixed_current_directions_show_disagreement_without_summing_lots():
    result = report([position(), position("IH", long_chg=-10, short_chg=20)])
    assert result["summary"]["tone"] == "neutral" and "分歧" in result["summary"]["label"]
    assert result["summary"]["bullish"] == result["summary"]["bearish"] == 1
    assert "net" not in result["summary"] and "long" not in result["summary"]


def test_price_basis_and_oi_compare_same_contract_adjacent_date():
    result = report(products=[quote()], previous=[quote(date="2026-11-24", price=3900, position=1400, verified_basis_pct=-1)])
    product = result["products"][0]
    assert product["price_change_pct"] == pytest.approx((4000 / 3900 - 1) * 100)
    assert product["oi_change"] == 100 and product["basis_change"] == .5
    assert product["price_position_label"] == "价涨 / 仓增"
    assert product["previous_date"] == "2026-11-24"


@pytest.mark.parametrize("previous", [quote(date="2026-11-24", symbol="IF2611"), quote(date="2026-11-23")])
def test_rollover_or_missing_adjacent_date_cannot_fabricate_changes(previous):
    product = report(products=[quote()], previous=[previous])["products"][0]
    assert product["price_change_pct"] is None and product["oi_change"] is None
    assert product["basis_change"] is None and product["previous_date"] is None


def test_unverified_basis_and_settlement_return_are_not_reused():
    product = report(products=[quote(verified_basis_pct=None, basis_pct=-10, change_pct=25)],
                     previous=[quote(date="2026-11-24", price=None)])["products"][0]
    assert product["basis_pct"] is None and product["basis_change"] is None
    assert product["price_change_pct"] is None


def test_intraday_price_and_oi_are_labelled_observations_not_closed_confirmations():
    product = report(products=[quote(ticktime="10:22:00", position=1600)],
                     previous=[quote(date="2026-11-24", price=3900)])["products"][0]
    assert product["quote_at"] == AS_OF + "T10:22:00"
    assert product["quote_status"] == "current" and product["oi_change"] == 100
    assert "收盘" not in product["price_position_label"]


def test_contract_fallback_is_dated_and_can_compare_matching_old_contract():
    current = {"variety": "IF", "main": {}, "contracts": [
        {**quote()["main"], "volume": 20}, {**quote()["main"], "symbol": "IF2703", "volume": 10}]}
    previous = {"variety": "IF", "main": quote(date="2026-11-24", symbol="IF2611")["main"],
                "contracts": [quote(date="2026-11-24", price=3900)["main"]]}
    product = report(products=[current], previous=[previous])["products"][0]
    assert product["contract"] == "IF2612" and product["price_change_pct"] is not None


def test_old_main_does_not_hide_newer_contract_quotes_but_same_date_main_is_respected():
    current = {"variety": "IF", "main": quote(date="2026-11-24")["main"],
               "contracts": [quote(symbol="IF2703", volume=50)["main"], quote(volume=10)["main"]]}
    product = report(products=[current])["products"][0]
    assert product["contract"] == "IF2703" and product["quote_date"] == AS_OF
    current["main"] = quote(volume=10)["main"]
    product = report(products=[current])["products"][0]
    assert product["contract"] == "IF2612" and product["quote_date"] == AS_OF


def test_negative_or_nonfinite_positions_are_not_valid_and_zero_short_ratio_is_missing():
    invalid = report([position(long_total=-1), position("IH", short_total=float("nan"))])
    assert invalid["summary"]["eligible"] == 0
    zero = report([position(short_total=0)])["products"][0]
    assert zero["ls_ratio"] is None and zero["long_share_pct"] == 100


def test_error_notes_preserve_unique_failures_without_modifying_input():
    data = {"positions": [position()], "errors": ["读取超时", "读取超时"], "error": "保留旧快照"}
    original = deepcopy(data)
    result = build_derivatives(data, AS_OF, CALENDAR)
    assert result["errors"] == ["读取超时", "保留旧快照"] and data == original


def test_per_product_failures_remain_visible_with_other_products_available():
    result = report([position(), {"variety": "IH", "error": "席位尚未披露"}],
                    [{"variety": "IC", "error": "行情超时"}])
    assert result["errors"] == ["IH：席位尚未披露", "IC：行情超时"]
    assert result["products"][0]["direction"] == "bullish"


def test_integration_adds_report_without_changing_existing_fusion_scores():
    from analysis.market_health import build_health
    from analysis.market_health_fusion import build_fusion
    import json
    from pathlib import Path

    state = json.loads((Path(__file__).resolve().parents[1] / "config/market_health_baseline.json").read_text())
    health = build_health(state)
    result = build_fusion(health, state["cross"], state["bars"]["sh000001"])
    assert len(result["dimensions"]) == 7
    assert result["derivatives"]["as_of"] == health["as_of"]
    assert len(result["derivatives"]["products"]) == 4
