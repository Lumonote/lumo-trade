"""指数风向 + 板块拐点编排层。每个数据源独立降级,单源失败不影响其余面板。"""
import pytest

from webui.services.market_pulse_service import MarketPulseService


def _bars(n=30, base=100.0):
    return [{"day": f"2026-06-{(i % 28) + 1:02d}", "open": base, "high": base,
             "low": base, "close": base + i, "volume": 1000.0} for i in range(n)]


def _series(n=30):
    return [{
        "trade_date": f"2026-06-{(i % 28) + 1:02d}", "sector": "银行",
        "sector_type": "行业", "member_count": 20, "net_amount": 10.0,
        "net_rate_median": 0.1, "pct_chg_mean": 0.5, "breadth": 0.6,
        "amount_median": 1000.0, "excess_vs_market": 0.1, "seat_count": 0,
        "provisional": 0,
    } for i in range(n)]


def _service(**overrides):
    kwargs = dict(
        index_bars=lambda symbols, datalen=160: {s: _bars() for s in symbols},
        index_quotes=lambda symbols: {},
        sector_series=lambda end_date=None, limit=60: {("银行", "行业"): _series()},
        turning_rules=lambda series_by_sector, weights=None, enabled=None: {
            "fired": [{"sector": "银行", "sector_type": "行业", "score": 80.0,
                       "rules": [], "trade_date": "2026-06-28", "provisional": False}],
            "watch": []},
        rule_stats=lambda: {"enabled": ["T1"], "weights": {"T1": 1.0}},
    )
    kwargs.update(overrides)
    return MarketPulseService(**kwargs)


def test_payload_lists_four_display_indices():
    out = _service().payload()
    assert len(out["indices"]) == 4
    assert [i["name"] for i in out["indices"]] == ["上证指数", "深证成指", "创业板指", "科创50"]


def test_payload_includes_style_axis_and_advice():
    out = _service().payload()
    assert out["style"]["axis"] in {"large", "small", "balanced", "unknown"}
    assert isinstance(out["position_advice"], str) and out["position_advice"]


def test_payload_index_carries_pulse_label():
    out = _service().payload()
    assert out["indices"][0]["pulse_label"]


def test_payload_sectors_from_turning_rules():
    out = _service().payload()
    assert [s["sector"] for s in out["sectors"]["fired"]] == ["银行"]


def test_payload_passes_enabled_rules_from_stats():
    seen = {}

    def turning(series_by_sector, weights=None, enabled=None):
        seen["enabled"] = enabled
        seen["weights"] = weights
        return {"fired": [], "watch": []}

    _service(turning_rules=turning).payload()
    assert seen["enabled"] == ["T1"]
    assert seen["weights"] == {"T1": 1.0}


def test_payload_degrades_when_index_source_fails():
    def boom(symbols, datalen=160):
        raise RuntimeError("network down")

    out = _service(index_bars=boom).payload()
    assert out["degraded"]["index"] is True
    assert out["indices"] == []
    assert out["sectors"]["fired"]          # 板块面板不受影响


def test_payload_degrades_when_sector_source_fails():
    def boom(end_date=None, limit=60):
        raise RuntimeError("db locked")

    out = _service(sector_series=boom).payload()
    assert out["degraded"]["sector"] is True
    assert out["sectors"]["fired"] == []
    assert len(out["indices"]) == 4         # 指数面板不受影响


def test_payload_degrades_quote_without_killing_index_panel():
    def boom(symbols):
        raise RuntimeError("sina down")

    out = _service(index_quotes=boom).payload()
    assert out["degraded"]["index_quote"] is True
    assert len(out["indices"]) == 4
    assert all(i["realtime"] is False for i in out["indices"])


def test_payload_marks_realtime_when_quote_present():
    quotes = {s: {"close": 4000.0, "pct_chg": 1.0, "date": "2026-08-21"}
              for s in ("sh000001", "sz399001", "sz399006", "sh000688",
                        "sh000300", "sh000852")}
    out = _service(index_quotes=lambda symbols: quotes).payload()
    assert all(i["realtime"] is True for i in out["indices"])


def test_payload_without_rule_stats_shows_nothing():
    """校验结果缺失时不能默认全开 —— 未经校验的判据不得上线(spec §8)。

    判据表为空时服务直接短路,连 turning_rules 都不调用。
    """
    called = []

    def turning(series_by_sector, weights=None, enabled=None):
        called.append(1)
        return {"fired": [{"sector": "不该出现"}], "watch": []}

    out = _service(rule_stats=lambda: {}, turning_rules=turning).payload()
    assert called == []
    assert out["sectors"]["enabled"] == []
    assert out["sectors"]["fired"] == []
    assert out["sectors"]["watch"] == []


def test_payload_provisional_flag_from_sector_rows():
    series = _series()
    series[-1]["provisional"] = 1
    out = _service(sector_series=lambda end_date=None, limit=60: {
        ("银行", "行业"): series}).payload()
    assert out["as_of"]["provisional"] is True


def test_payload_includes_disclaimer():
    assert "不构成投资建议" in _service().payload()["disclaimer"]


def test_payload_uses_cache_within_ttl():
    calls = []

    def bars(symbols, datalen=160):
        calls.append(1)
        return {s: _bars() for s in symbols}

    svc = _service(index_bars=bars)
    svc.payload()
    svc.payload()
    assert len(calls) == 1


def test_payload_force_bypasses_cache():
    calls = []

    def bars(symbols, datalen=160):
        calls.append(1)
        return {s: _bars() for s in symbols}

    svc = _service(index_bars=bars)
    svc.payload()
    svc.payload(force=True)
    assert len(calls) == 2


def test_landscape_survives_absent_turning_events_and_retains_lagged_sectors():
    from tests.test_sector_opportunities import series
    from datetime import date
    seen = {}

    def turning(rows, **kwargs):
        seen.update(rows)
        return {"fired": [], "watch": []}

    out = _service(sector_series=lambda **kwargs: {
        ("银行", "行业"): series(),
        ("旧板块", "行业"): series(end=date(2026, 9, 10))}, turning_rules=turning).payload(as_of="2026-10-01")
    assert out["sectors"]["status"] == "no_trigger"
    assert len(out["sectors"]["landscape"]) == 2
    assert out["sectors"]["coverage"]["eligible"] == 1
    assert out["sectors"]["coverage"]["stale"] == 1
    assert ("旧板块", "行业") not in seen


def test_validation_failure_and_empty_source_are_distinct():
    def boom():
        raise RuntimeError("failed to read validation")

    failed = _service(rule_stats=boom).payload()
    assert failed["sectors"]["status"] == "validation_error"
    assert failed["sectors"]["landscape"]
    assert not failed["sectors"]["fired"]
    assert _service(sector_series=lambda **kwargs: {}).payload()["sectors"]["status"] == "no_data"
