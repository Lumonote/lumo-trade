"""Breadth reports use the selected view's dates and saved adjacent exchange sessions."""
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path

import pytest

from analysis.market_health import market_snapshot
from webui.services import market_health_service as service, market_health_cross_service as cross


pytestmark = pytest.mark.usefixtures("isolated_price_db")


def test_breadth_calendar_keeps_missing_sessions_visible(monkeypatch):
    calls = []
    def sessions(start, end):
        calls.append((start, end))
        return {"2026-11-23", "2026-11-24", "2026-11-25"}
    monkeypatch.setattr(service.daily_history, "_known_sessions", sessions)
    assert service._breadth_calendar("2026-11-25", ["2026-11-23", "2026-11-25"]) == [
        "2026-11-23", "2026-11-24", "2026-11-25"]
    assert calls == [(dt.date(2026, 10, 21), dt.date(2026, 11, 25))]


def test_breadth_calendar_does_not_invent_current_session(monkeypatch):
    monkeypatch.setattr(service.daily_history, "_known_sessions", lambda *args: set())
    assert service._breadth_calendar("2026-11-25", ["2026-11-24", "2026-11-26"]) == ["2026-11-24"]
    assert service._breadth_calendar(None, []) == []


@pytest.mark.parametrize("calendar_available", [True, False])
def test_query_report_isolates_live_valuations_and_merges_closed_comparison(monkeypatch, calendar_available):
    original = json.loads((Path(__file__).resolve().parents[1] / "config/market_health_baseline.json").read_text())
    closed_breadth = {**original["breadth"], "above_ma20": 50}
    original.update(breadth=closed_breadth, observations=[closed_breadth])
    before = deepcopy(original)
    live_day = "2026-10-23"
    live_breadth = {**closed_breadth, "as_of": live_day, "above_ma20": 60, "provisional": True}
    live_market = {"as_of": live_day, "provisional": True, "source": "offline test", "values": market_snapshot([
        {"trade": 10, "changepercent": 1, "pb": 2, "per": 10, "mktcap": 100},
        {"trade": 10, "changepercent": -1, "pb": 6, "per": 30, "mktcap": 200},
    ])}
    quotes = {symbol: {"as_of": live_day, "quote_at": live_day + "T14:00:00", "price": bars[-1]["close"],
                       "previous_close": bars[-1]["close"], "open": bars[-1]["close"],
                       "high": bars[-1]["close"] * 1.01, "low": bars[-1]["close"] * .99}
              for symbol, bars in original["bars"].items()}
    saved = {"live_quotes": {"quotes": quotes, "fetched_at": live_day + "T14:00:05+08:00"},
             "intraday": {"as_of": live_day, "market": live_market, "breadth": live_breadth,
                          "observations": [live_breadth]}}
    monkeypatch.setattr(service, "read_state", lambda: original)
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 10, 23, 14, 1))
    monkeypatch.setattr(service.kv_repo, "get", lambda ns, key: (saved[key], None) if key in saved else None)
    monkeypatch.setattr(cross, "read_cached", lambda day: original["cross"])
    monkeypatch.setattr(service.daily_history, "_known_sessions",
                        lambda *args: {"2026-10-22", live_day} if calendar_available else set())
    result = service.query()
    closed, live = result["breadth_valuation"], result["intraday"]["breadth_valuation"]
    assert (closed["as_of"], live["as_of"]) == ("2026-10-22", live_day)
    assert closed["provisional"] is False and live["provisional"] is True
    assert closed["valuation"]["pb"]["distribution"] is None
    assert live["valuation"]["pb"]["distribution"]["quantiles"]["p50"] == 4
    row = next(r for r in live["trend"]["rows"] if r["key"] == "above_ma20")
    assert row["change_pp"] == (10 if calendar_available else None)
    assert row["comparable"] is calendar_available
    assert original == before
