"""收盘边界以真实日线推进快照日期，不把盘中或未更新的行情改标为今日。"""
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis.market_health import build_health
from webui.services import market_health_service as service
from webui.services import market_health_cross_service as cross


pytestmark = pytest.mark.usefixtures("isolated_price_db")


class Jobs:
    def __init__(self):
        self.data = {"id": "job", "status": "queued"}

    def start(self, kind, params, worker):
        worker("job", params)
        return self.data

    def update(self, job_id, **values):
        self.data.update(values)

    def append_log(self, job_id, message):
        self.data.setdefault("logs", []).append(message)


def closing_state(day="2026-11-24"):
    value = json.loads((Path(__file__).resolve().parents[1] / "config/market_health_baseline.json").read_text())
    for rows in value["bars"].values():
        rows.append({**rows[-1], "date": day})
    return value


@pytest.mark.parametrize("clock, expected", [
    ("14:59:59", "2026-11-24"),
    ("15:00:00", "2026-11-25"),
    ("15:10:00", "2026-11-25"),
])
def test_index_refresh_accepts_today_only_after_market_close(monkeypatch, clock, expected):
    original = closing_state()
    incoming = deepcopy(original["bars"])
    for rows in incoming.values():
        rows.append({**rows[-1], "date": "2026-11-25"})
    current = dt.datetime.fromisoformat(f"2026-11-25T{clock}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    written = {}
    monkeypatch.setattr(service, "now", lambda: current)
    monkeypatch.setattr(service, "read_state", lambda: deepcopy(original))
    monkeypatch.setattr(service, "latest_job", lambda jobs: None)
    monkeypatch.setattr(service, "fetch_live_quotes", lambda **kwargs: {})
    monkeypatch.setattr(service.kv_repo, "set_", lambda ns, key, value: written.update({key: deepcopy(value)}))

    result = service.start_refresh("indices", Jobs(), lambda *args, **kwargs: incoming)

    assert result["status"] == "finished", result
    assert build_health(written["snapshot"])["as_of"] == expected
    assert all(rows[-1]["date"] == expected for rows in written["snapshot"]["bars"].values())


def test_after_close_without_new_daily_bars_keeps_actual_snapshot_date(monkeypatch):
    original = closing_state()
    written = {}
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 25, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(service, "read_state", lambda: deepcopy(original))
    monkeypatch.setattr(service, "latest_job", lambda jobs: None)
    monkeypatch.setattr(service, "fetch_live_quotes", lambda **kwargs: {})
    monkeypatch.setattr(service.kv_repo, "set_", lambda ns, key, value: written.update({key: deepcopy(value)}))

    result = service.start_refresh("indices", Jobs(), lambda *args, **kwargs: original["bars"])

    assert result["status"] == "finished", result
    assert build_health(written["snapshot"])["as_of"] == "2026-11-24"
    assert written["snapshot"]["bars"] == original["bars"]


def test_after_close_excludes_provisional_and_future_daily_bars(monkeypatch):
    original = closing_state()
    incoming = deepcopy(original["bars"])
    for rows in incoming.values():
        rows.extend([
            {**rows[-1], "date": "2026-11-25", "provisional": True},
            {**rows[-1], "date": "2026-11-26"},
        ])
    written = {}
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 25, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(service, "read_state", lambda: deepcopy(original))
    monkeypatch.setattr(service, "latest_job", lambda jobs: None)
    monkeypatch.setattr(service, "fetch_live_quotes", lambda **kwargs: {})
    monkeypatch.setattr(service.kv_repo, "set_", lambda ns, key, value: written.update({key: deepcopy(value)}))

    result = service.start_refresh("indices", Jobs(), lambda *args, **kwargs: incoming)

    assert result["status"] == "finished", result
    assert written["snapshot"]["bars"] == original["bars"]
    assert build_health(written["snapshot"])["as_of"] == "2026-11-24"


def test_request_started_before_close_does_not_promote_inflight_daily_bar(monkeypatch):
    original = closing_state()
    incoming = deepcopy(original["bars"])
    for rows in incoming.values():
        rows.append({**rows[-1], "date": "2026-11-25"})
    current = dt.datetime(2026, 11, 25, 14, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    written = {}
    monkeypatch.setattr(service, "now", lambda: current)
    monkeypatch.setattr(service, "read_state", lambda: deepcopy(original))
    monkeypatch.setattr(service, "latest_job", lambda jobs: None)
    monkeypatch.setattr(service, "fetch_live_quotes", lambda **kwargs: {})
    monkeypatch.setattr(service.kv_repo, "set_", lambda ns, key, value: written.update({key: deepcopy(value)}))

    def index_loader(*args, **kwargs):
        nonlocal current
        current = current.replace(hour=15, minute=0, second=1)
        return incoming

    result = service.start_refresh("indices", Jobs(), index_loader)

    assert result["status"] == "finished", result
    assert build_health(written["snapshot"])["as_of"] == "2026-11-24"


@pytest.mark.parametrize("clock, quote_day, quote_time, closed_day, pending", [
    ("14:59:59", "2026-11-25", "14:59:00", "2026-11-24", False),
    ("15:00:00", "2026-11-25", "15:00:00", "2026-11-24", True),
    ("15:10:00", "2026-11-25", "14:00:00", "2026-11-24", True),
    ("15:10:00", "2026-11-25", "15:00:00", "2026-11-25", False),
    ("15:10:00", "2026-11-24", "15:00:00", "2026-11-24", False),
])
def test_close_pending_tracks_unstored_session_even_when_live_quote_is_stale(
        monkeypatch, clock, quote_day, quote_time, closed_day, pending):
    current = dt.datetime.fromisoformat(f"2026-11-25T{clock}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(service, "now", lambda: current)
    value = {"quotes": {"sh000001": {"as_of": quote_day, "quote_at": f"{quote_day}T{quote_time}"}}}

    result = service.live_status(value, closed_day)

    assert result["close_pending"] is pending
    if quote_time == "14:00:00":
        assert result["available"] is False


def test_post_close_query_includes_today_in_market_internals_calendar(monkeypatch):
    original = closing_state("2026-11-25")
    calendars = []
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 25, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(service, "read_state", lambda: original)
    monkeypatch.setattr(service.kv_repo, "get", lambda *args: None)
    monkeypatch.setattr(cross, "read_cached", lambda day: {})
    monkeypatch.setattr(service.market_ice_service, "history", lambda: [])
    monkeypatch.setattr(service.market_internals_service, "read_history", lambda calendar: calendars.append(calendar) or [])

    result = service.query()

    assert result["as_of"] == "2026-11-25"
    assert calendars[0][-1] == "2026-11-25"


def test_post_close_market_snapshot_is_saved_to_closing_history(monkeypatch):
    original = closing_state("2026-11-25")
    market = {"as_of": "2026-11-25", "provisional": False,
              "sample_started_at": "2026-11-25T15:10:00+08:00"}
    internals = {"as_of": "2026-11-25", "provisional": False}
    saved = []
    monkeypatch.setattr(service.market_internals_service, "save_close", lambda value: saved.append(deepcopy(value)))

    assert service._save_market_snapshot(original, market, internals) is None
    assert saved == [internals]
    assert original["market"] == market


@pytest.mark.parametrize("quote_time, available", [("15:23:51", True), ("15:30:01", False)])
def test_after_close_quote_timestamp_can_follow_trading_close(monkeypatch, quote_time, available):
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 11, 25, 15, 29, tzinfo=ZoneInfo("Asia/Shanghai")))
    value = {"quotes": {"sh000001": {"as_of": "2026-11-25", "quote_at": f"2026-11-25T{quote_time}"}}}

    result = service.live_status(value, "2026-11-24")

    assert result["available"] is available
    assert result["close_pending"] is True


@pytest.mark.parametrize("allow_current_fallback, online, expected", [
    (False, False, ["2026-11-24"]),
    (True, False, ["2026-11-24", "2026-11-25", "2026-11-26"]),
    (False, True, ["2026-11-24", "2026-11-25"]),
])
def test_index_loader_keeps_online_today_but_can_exclude_unconfirmed_cache(
        monkeypatch, allow_current_fallback, online, expected):
    from analysis import market_regime

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == ZoneInfo("Asia/Shanghai")
            return cls(2026, 11, 25, 15, 29, tzinfo=tz)

    cached = [{"day": day, "close": 100} for day in ("2026-11-24", "2026-11-25", "2026-11-26")]
    persisted = []
    monkeypatch.setattr(market_regime, "datetime", Clock)
    monkeypatch.setattr(market_regime, "fetch_index_daily", lambda *args, **kwargs: cached[:2] if online else [])
    monkeypatch.setattr(market_regime, "load_index_bars_from_db", lambda *args, **kwargs: cached)
    monkeypatch.setattr(market_regime, "persist_index_bars", lambda symbol, bars: persisted.append((symbol, bars)))

    result = market_regime.get_index_bars(["sh000001"], allow_current_fallback=allow_current_fallback)

    assert [bar["day"] for bar in result["sh000001"]] == expected
    assert bool(persisted) is online
    assert len(cached) == 3
