import pytest

from webui.services.kline_service import StockKlineService

pytestmark = pytest.mark.usefixtures('isolated_kline')


class StubKlineService(StockKlineService):
    def __init__(self, records=None):
        super().__init__("unused")
        self.records = records or []

    def fetch_sina_kline(self, stock_code, period, limit):
        return self.records, ""


def _records_with_pattern():
    records = []
    close = 10.0
    for i in range(80):
        close *= 1.002
        records.append({
            "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000 + i,
        })
    prev = records[-1]["close"]
    zt = prev * 1.10
    records.append({"date": "2026-04-01", "open": prev * 1.02, "high": zt, "low": prev * 1.01, "close": zt, "volume": 6000, "pct_chg": 10.0})
    records.append({"date": "2026-04-02", "open": zt * 0.99, "high": zt, "low": zt * 0.96, "close": zt * 0.98, "volume": 2500})
    records.append({"date": "2026-04-03", "open": zt * 0.99, "high": zt * 1.02, "low": zt * 0.98, "close": zt * 1.01, "volume": 5200})
    return records


def test_get_payload_attaches_patterns():
    service = StubKlineService(_records_with_pattern())

    payload, error = service.get_payload("600000")

    assert error is None
    assert payload["available"] is True
    assert payload["patterns"]
    item = payload["patterns"][0]
    assert {"pattern", "name", "date", "anchor_date", "mark_dates", "strength", "tone", "rationale", "days_ago"} <= set(item)


def test_get_payload_patterns_degrade_to_empty(monkeypatch):
    service = StubKlineService(_records_with_pattern())

    def boom(*args, **kwargs):
        raise RuntimeError("detector offline")

    monkeypatch.setattr("webui.services.kline_service.detect_kline_patterns", boom)
    payload, error = service.get_payload("600000")

    assert error is None
    assert payload["available"] is True
    assert payload["patterns"] == []


def test_get_payload_attaches_bearish_patterns():
    records = []
    close = 10.0
    for i in range(80):
        close *= 1.002
        records.append({
            "date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}",
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000 + i,
        })
    last = records[-1]["close"]
    records.append({
        "date": "2026-04-01",
        "open": last * 1.01,
        "high": last * 1.015,
        "low": last * 0.94,
        "close": last * 0.95,
        "volume": 5000,
    })
    service = StubKlineService(records)

    payload, error = service.get_payload("600000")

    assert error is None
    assert any(item.get("direction") == "bearish" for item in payload["patterns"])
