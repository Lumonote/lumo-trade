from copy import deepcopy

import pytest

from analysis.market_health import market_internals
from webui.services import market_internals_service as service


def test_market_breadth_and_trin_use_matching_directional_groups():
    result = market_internals([(11, 10, 30), (12, 10, 30), (9, 10, 15),
                               (10, 10, 1000), (9, 10, 0), (10, 0, 1000)])
    assert (result["up"], result["down"], result["flat"]) == (2, 1, 1)
    assert (result["directional"], result["eligible"], result["universe"]) == (3, 4, 6)
    assert (result["inactive"], result["missing_price"]) == (1, 1)
    assert result["breadth_pct"] == pytest.approx(200 / 3)
    assert (result["up_volume_shares"], result["down_volume_shares"], result["trin"]) == (60, 15, .5)


def test_zero_side_and_missing_volume_are_distinct_from_market_neutral():
    assert market_internals([(11, 10, 100)])["breadth_pct"] == 100
    assert market_internals([(11, 10, 100)])["trin"] is None
    assert market_internals([(10, 10, 100)])["breadth_pct"] is None
    assert market_internals([(11, 10, 100), (9, 10, 100)])["trin"] == 1
    result = market_internals([(10, 9, 100), (9, 10, 200), (9, 10, float("nan"))])
    assert result["missing_volume"] == 1 and result["breadth_pct"] == 50
    assert result["trin"] is None


def test_providers_use_reference_price_and_convert_tushare_lots_to_shares():
    day = "2026-10-22"
    quotes = [("sh600001", "6.000", "5.990", 100, 2000), ("sz000001", 9, 10, 200, 1000),
              ("bj920001", 10, 10, 800, 9000), ("sz159001", 9, 10, 400, 40000)]
    sina = service.from_sina([{"symbol": s, "trade": p, "settlement": ref, "volume": vol, "amount": amount, "changepercent": 0}
                               for s, p, ref, vol, amount in quotes], day, started="start", captured="end", provisional=True)
    daily = [{"ts_code": f"{s[2:]}.{s[:2].upper()}", "trade_date": "20261022", "close": p,
              "pre_close": ref, "vol": vol / 100, "amount": amount / 1000} for s, p, ref, vol, amount in quotes]
    close = service._from_daily(daily, day)
    assert close["trin"] == sina["trin"] == 2
    assert close["breadth_pct"] == sina["breadth_pct"] == 50
    assert close["eligible"] == sina["eligible"] == 3
    assert close["up_volume_shares"] == sina["up_volume_shares"] == 100
    assert close["turnover_yuan"] == sina["turnover_yuan"] == 12000
    assert close["sh_sz_turnover_yuan"] == sina["sh_sz_turnover_yuan"] == 3000
    assert close["exchange_turnover_yuan"] == {"SH": 2000, "SZ": 1000, "BJ": 9000}
    assert close["up_amount_pct"] == pytest.approx(200 / 3)
    assert close["up_volume_pct"] == pytest.approx(100 / 3)
    assert close["amount_trin"] == .5
    assert close["amount_breadth_gap_pp"] == pytest.approx(50 / 3)
    assert close["volume_breadth_gap_pp"] == pytest.approx(-50 / 3)
    with pytest.raises(ValueError, match="重复"):
        service._from_daily(daily + daily, day)
    with pytest.raises(ValueError, match="异日"):
        service._from_daily(daily, "2026-10-23")


@pytest.mark.usefixtures("isolated_price_db")
def test_closed_history_keeps_gaps_and_separates_live_observations():
    first, gap, today = "2026-10-21", "2026-10-22", "2026-10-23"
    values = market_internals([(11, 10, 200), (9, 10, 100)])
    close = {**values, "as_of": first, "provisional": False, "source": service.TUSHARE}
    live = {**values, "as_of": today, "provisional": True, "source": service.SINA}
    service.save_close(close)
    service.save_close({**close, "source": service.SINA, "trin": 2})
    service.save_close(live)
    history = service.read_history([first, gap])
    assert history == [close, {"as_of": gap}]
    assert service.read_history([today]) == [{"as_of": today}]
    before = deepcopy(history)
    assert service.view(history, {"internals": live}, gap)["current"]["as_of"] == first
    assert service.view(history, {"internals": live}, today, provisional=True)["current"]["as_of"] == today
    assert service.view(history, {}, today, provisional=True)["current"]["as_of"] == first
    assert history == before


def test_turnover_comparison_uses_adjacent_close_and_checks_each_exchange(monkeypatch):
    rows = market_internals([(11, 10, 100, 1200, "SH"), (9, 10, 200, 1900, "SZ"), (10, 10, 100, None, "BJ")])
    assert rows["turnover_yuan"] is None and rows["sh_sz_turnover_yuan"] == 3100
    assert rows["up_amount_yuan"] == 1200 and rows["directional_amount_sample"] == 2

    monkeypatch.setattr(service, "limit_counts", lambda date: None)
    monkeypatch.setattr(service, "intraday_history", lambda date: [])
    first, second, third, today = "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"
    base = {"eligible": 4, "up": 1, "down": 3, "flat": 0, "provisional": False, "source": service.TUSHARE}
    history = [{**base, "as_of": first, "turnover_yuan": 10000, "sh_sz_turnover_yuan": 9500},
               {**base, "as_of": second, "turnover_yuan": 9900, "sh_sz_turnover_yuan": 9520, "up": 2, "down": 1, "flat": 1},
               {"as_of": third}]
    closed = service.view(history, {}, third)["current"]["vs_previous"]
    assert closed["as_of"] == first and closed["up_pct"] == 100 and closed["flat_pct"] is None
    assert closed["turnover"]["state"] == "contraction" and closed["sh_sz_turnover"]["state"] == "flat"
    live = {**base, "as_of": today, "turnover_yuan": 4000, "sh_sz_turnover_yuan": 2000, "provisional": True}
    gap = service.view(history, {"internals": live}, today, provisional=True)["current"]["vs_previous"]
    assert gap["as_of"] == third and gap["turnover"]["progress_pct"] is None
    recent = service.view(history[:2], {"internals": live}, today, provisional=True)["current"]["vs_previous"]
    assert recent["sh_sz_turnover"]["progress_pct"] == pytest.approx(200000 / 9520)
    assert "state" not in recent["sh_sz_turnover"]


def test_history_read_failure_is_reported_without_failing_market_refresh(monkeypatch):
    def unavailable(_calendar):
        raise OSError("cache unavailable")

    monkeypatch.setattr(service, "read_history", unavailable)
    assert "历史回补中断" in service.backfill(["2026-10-22"], 5500)
