"""Financial invariants for the offline opportunity audit, with no live data writes."""
import math

import pytest

from scripts.analyze_installed_opportunities import exit_return, next_day, ts_code


def bar(open_price=100, high=101, low=99, close=100, pre_close=100):
    return {"open": open_price, "high": high, "low": low, "close": close, "pre_close": pre_close}


def test_entry_day_profit_cannot_be_sold_under_t_plus_one():
    path = [bar(high=110, low=90, close=100), bar(high=100.8, low=99.5, close=100.5)]
    assert exit_return(path, take_profit=1, stop_loss=5) == pytest.approx(0.5)


def test_both_barriers_same_day_assumes_loss_first():
    path = [bar(), bar(high=110, low=94)]
    assert exit_return(path, take_profit=1, stop_loss=5) == -5


def test_gap_loss_is_not_capped_at_the_stop_price():
    path = [bar(), bar(open_price=80, high=90, low=79, close=85)]
    assert exit_return(path, take_profit=1, stop_loss=5) == pytest.approx(-20)


def test_locked_down_bar_defers_sale_to_next_fill():
    path = [bar(), bar(open_price=90, high=90, low=90, close=90),
            bar(open_price=85, high=87, low=83, close=86, pre_close=90)]
    assert exit_return(path, take_profit=1, stop_loss=5) == pytest.approx(-15)


def test_locked_down_final_bar_is_unresolved():
    assert math.isnan(exit_return([bar(), bar(open_price=90, high=90, low=90, close=90)], 1, 5))


def test_gap_profit_uses_open_fill():
    assert exit_return([bar(), bar(open_price=104, high=106, low=103, close=105)], 1, 5) == pytest.approx(4)


def test_weekend_reports_share_one_next_entry_day():
    calendar = ["20260612", "20260615", "20260616"]
    assert {next_day(calendar, day) for day in ["2026-06-12", "2026-06-13", "2026-06-14"]} == {"20260615"}


def test_beijing_920_codes_are_not_mapped_to_shanghai():
    assert ts_code("920001") == "920001.BJ"
    assert ts_code("000001") == "000001.SZ"
    assert ts_code("600000") == "600000.SH"
