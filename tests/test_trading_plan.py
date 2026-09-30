from copy import deepcopy
from datetime import date, timedelta
import math

import pytest

from analysis.trading_plan import evaluate_plan, market_plan, number, sector_plan, stock_plan

TODAY = date(2026, 9, 30)


def bars():
    rows = []
    for i in range(80):
        close = 10 + i * .005 + math.sin(i) * .03
        rows.append({"date": (TODAY - timedelta(days=79-i)).isoformat(),
                     "close": close, "high": close + .12, "low": close - .12,
                     "volume": 1600 if i == 79 else 1000})
    rows[-15]["high"] = 13
    return {"source": "test", "records": rows}


def pulse(level="neutral"):
    return {"level": level, "as_of": {"index": TODAY.isoformat()},
            "indices": [{"pulse": "range", "as_of": TODAY.isoformat()} for _ in range(4)],
            "sectors": {"fired": [{"sector": "银行", "trade_date": TODAY.isoformat(), "provisional": False}]}}


def candidate():
    return stock_plan("000001", {"name": "示例银行", "sector": "银行"}, bars(), today=TODAY)


def evaluate(plan=None, **kwargs):
    original = candidate()
    market = market_plan(pulse(), {"total_equity": 100000, "cash": 100000}, [], today=TODAY)
    sector = sector_plan({"sector": "银行"}, pulse(), today=TODAY)
    defaults = dict(plan=plan or original, candidate=original, market=market, sector=sector,
                    quote={"price": original["reference_price"], "quote_date": TODAY.isoformat()}, today=TODAY)
    return evaluate_plan(**{**defaults, **kwargs})


def test_finite_number_guard():
    for value in (None, "—", float("inf"), float("nan")):
        assert number(value) is None
    assert number("0") == 0


@pytest.mark.parametrize("level,cap,single", [("risk_on", 80, 15), ("neutral", 60, 10),
                                               ("caution", 40, 5), ("risk_off", 20, 0)])
def test_market_regime_limits(level, cap, single):
    m = market_plan(pulse(level), {"total_equity": 100000, "cash": 90000},
                    [{"market_value": 10000}], today=TODAY)
    assert m["total_cap_pct"] == cap
    assert m["single_cap_pct"] == single
    assert m["new_budget_pct"] == max(0, cap - 10)


def test_unknown_and_stale_market_block_entries():
    p = pulse()
    p["as_of"]["index"] = "2026-08-01"
    out = market_plan(p, {"total_equity": 100000, "cash": 100000}, [], today=TODAY)
    assert out["level"] == "unknown"
    assert not out["entry_allowed"]
    assert out["reduce_pct"] == 0


def test_partial_market_only_allows_caution_limit():
    p = pulse("risk_on")
    p["indices"].pop()
    assert market_plan(p, today=TODAY)["level"] == "caution"


def test_provisional_and_stale_sector_do_not_confirm():
    p = pulse()
    p["sectors"]["fired"][0]["provisional"] = True
    assert not sector_plan({"sector": "银行"}, p, today=TODAY)["entry_allowed"]
    p["sectors"]["fired"][0].update(provisional=False, trade_date="2026-08-01")
    assert not sector_plan({"sector": "银行"}, p, today=TODAY)["entry_allowed"]


def test_stock_prices_are_ordered_and_data_not_mutated():
    data = bars()
    original = deepcopy(data)
    p = stock_plan("000001", {"sector": "银行"}, data, today=TODAY)
    l = p["levels"]
    assert 0 < l["stop_loss"] < l["entry_low"] <= l["entry_high"] < l["target1"] < l["target2"]
    assert p["trend"] == "up"
    assert data == original
    assert p["basis_key"]


@pytest.mark.parametrize("kind", ["short", "stale", "nan", "flat"])
def test_bad_stock_data_is_unavailable(kind):
    data = bars()
    if kind == "short":
        data["records"] = data["records"][-30:]
    elif kind == "stale":
        for r in data["records"]:
            r["date"] = "2026-08-01"
    elif kind == "nan":
        for r in data["records"]:
            r["high"] = float("nan")
    else:
        for r in data["records"]:
            r.update(close=10, high=10, low=10)
    assert not stock_plan("000001", {}, data, today=TODAY)["available"]


def test_entry_requires_all_gates_and_valid_quote():
    assert evaluate()["status"] == "entry"
    out = evaluate(quote={"price": 10.4})
    assert out["status"] == "blocked"
    assert out["price_source"] == "daily_close"


def test_no_chasing_and_no_buying_below_stop():
    p = candidate()
    assert evaluate(quote={"price": p["levels"]["entry_high"] + .3, "quote_date": TODAY.isoformat()})["status"] == "wait_pullback"
    assert evaluate(quote={"price": p["levels"]["stop_loss"] - .1, "quote_date": TODAY.isoformat()})["status"] == "invalidated"


def test_locked_stop_wins_even_when_market_and_sector_missing():
    p = candidate()
    p["locked"] = True
    out = evaluate(p, market=market_plan({}, today=TODAY),
                   quote={"price": p["levels"]["stop_loss"] - .1, "quote_date": TODAY.isoformat()},
                   position={"qty": 1000}, sector={"entry_allowed": False, "label": "未知"})
    assert out["status"] == "exit"
    assert out["levels"]["stop_loss"] == p["levels"]["stop_loss"]


def test_profit_target_has_priority_and_add_never_averages_down():
    p = candidate()
    quote = {"price": p["levels"]["target1"], "quote_date": TODAY.isoformat()}
    assert evaluate(p, quote=quote, position={"qty": 100})["status"] == "take_profit"
    assert evaluate(position={"qty": 100, "avg_cost": 11})["status"] == "hold"
    assert evaluate(position={"qty": 100, "avg_cost": 10})["status"] == "add"


def test_expired_plan_blocks_new_entries_but_keeps_exit():
    p = candidate()
    p["valid_until"] = "2026-09-29"
    assert evaluate(p)["status"] == "blocked"
    assert evaluate(p, quote={"price": p["levels"]["stop_loss"] - .1, "quote_date": TODAY.isoformat()},
                    position={"qty": 100})["status"] == "exit"


def test_old_quote_cannot_override_newer_daily_bar():
    out = evaluate(quote={"price": 10.4, "quote_date": "2026-09-29"})
    assert out["status"] == "blocked"
    assert out["price_source"] == "daily_close"


def test_low_score_and_degraded_opportunity_block_entries():
    c = candidate()
    c["opportunity_score"] = 30
    assert evaluate(candidate=c)["status"] == "blocked"
    c.update(opportunity_score=80, opportunity_degraded=True)
    assert evaluate(candidate=c)["status"] == "blocked"


def test_null_risk_fields_are_replaced_by_price_evidence():
    c = stock_plan("000001", {}, bars(), {"signals": {"rsi": None, "change_3d": float("nan")}}, today=TODAY)
    assert c["risk"]["unknown"] is False
    assert math.isfinite(c["signals"]["change_3d"])


def test_below_ma20_never_becomes_a_new_entry():
    c = candidate()
    price = (c["ma20"] + c["levels"]["entry_low"]) / 2
    assert evaluate(quote={"price": price, "quote_date": TODAY.isoformat()})["status"] == "blocked"
