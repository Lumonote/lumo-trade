import datetime

from webui.services.kline_service import StockKlineService


class StubKlineService(StockKlineService):
    def __init__(self, records=None, error=None):
        super().__init__("unused")
        self.records = records if records is not None else []
        self.error = error

    def load_local_kline(self, stock_code, period, limit):
        return [], None

    def fetch_sina_kline(self, stock_code, period, limit):
        if self.error:
            raise self.error
        return self.records, ""


def test_sina_symbol_and_scale_mapping():
    assert StockKlineService.sina_symbol_for_code("600000") == "sh600000"
    assert StockKlineService.sina_symbol_for_code("510300") == "sh510300"
    assert StockKlineService.sina_symbol_for_code("159919") == "sz159919"
    assert StockKlineService.sina_symbol_for_code("000001") == "sz000001"
    assert StockKlineService.sina_symbol_for_code("830000") == "bj830000"
    assert StockKlineService.period_to_sina_scale("1h") == "60"
    assert StockKlineService.period_to_sina_scale("daily") == "240"


def test_parse_sina_klines_calculates_pct_change():
    records = StockKlineService.parse_sina_klines(
        [
            {"day": "2026-05-19", "open": "10", "close": "10", "high": "11", "low": "9"},
            {"day": "2026-05-20", "open": "10", "close": "11", "high": "11", "low": "10"},
        ]
    )

    assert records[0]["pct_chg"] == 0.0
    assert records[1]["pct_chg"] == 10.0


def test_get_payload_validates_code_without_network():
    service = StubKlineService()

    payload, error = service.get_payload("bad")

    assert payload is None
    assert error == "Invalid stock code"


def test_get_payload_handles_sina_success_and_failure():
    service = StubKlineService(records=[{"date": "2026-05-20", "open": 1, "close": 1}])
    fail_service = StubKlineService(error=RuntimeError("offline"))

    payload, error = service.get_payload("600000", limit="999")
    failed, failed_error = fail_service.get_payload("600000")

    assert error is None
    assert payload["source"] == "sina"
    assert payload["available"] is True
    assert failed_error is None
    assert failed["available"] is False
    assert failed["detail"] == "offline"


def _today_str() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


def test_apply_realtime_quote_updates_today_bar():
    """最后一根是当日bar时，用实时价改写 close/high/pct_chg。"""
    records = [
        {"date": "2026-06-01", "open": 10, "close": 10.0, "high": 10.5, "low": 9.5, "pct_chg": 0.0},
        {"date": "2026-06-02", "open": 10, "close": 10.2, "high": 10.3, "low": 9.9, "pct_chg": 2.0},
    ]

    applied = StockKlineService._apply_quote_to_records(
        records, {"price": 10.8, "change_pct": 5.88}, "2026-06-02"
    )

    assert applied is True
    assert records[-1]["close"] == 10.8
    assert records[-1]["high"] == 10.8  # 实时价高于原高点，须顶上去
    assert records[-1]["pct_chg"] == 5.88  # 用报价的当日涨跌幅(对昨收)


def test_apply_realtime_quote_skips_when_last_bar_not_today():
    """当日bar尚未生成(盘前/隔日)时不伪造，原样保留。"""
    records = [{"date": "2026-06-02", "open": 10, "close": 10.2, "high": 10.3, "low": 9.9}]

    applied = StockKlineService._apply_quote_to_records(
        records, {"price": 11.0, "change_pct": 7.8}, "2026-06-03"
    )

    assert applied is False
    assert records[-1]["close"] == 10.2


def test_apply_realtime_quote_ignores_zero_price():
    """0 价 = 停牌/无效，按未取到处理。"""
    records = [{"date": "2026-06-03", "open": 10, "close": 10.2, "high": 10.3, "low": 9.9}]

    assert StockKlineService._apply_quote_to_records(records, {"price": 0}, "2026-06-03") is False


def test_get_payload_attaches_and_applies_realtime_quote():
    today = _today_str()
    service = StubKlineService(
        records=[{"date": today, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "pct_chg": 0.0}]
    )
    service.set_quote_provider(
        lambda codes: {"600000": {"name": "测试股", "price": 1.5, "change_pct": 50.0, "change_amount": 0.5}}
    )

    payload, error = service.get_payload("600000")

    assert error is None
    assert payload["quoted"] is True
    assert payload["quote"]["price"] == 1.5
    assert payload["quote"]["applied"] is True
    assert payload["quote_updated_at"]
    assert payload["records"][-1]["close"] == 1.5  # 当日bar被实时价改写
    assert payload["name"] == "测试股"  # 报价名回填


def test_get_payload_attaches_quote_without_applying_when_bar_stale():
    """最后一根不是当日bar：附带实时价徽章，但不改写K线。"""
    service = StubKlineService(
        records=[{"date": "2026-06-02", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0}]
    )
    service.set_quote_provider(lambda codes: {"600000": {"price": 1.5, "change_pct": 50.0}})

    payload, _ = service.get_payload("600000")

    assert payload["quoted"] is True
    assert payload["quote"]["applied"] is False
    assert payload["records"][-1]["close"] == 1.0  # 未改写


def test_get_payload_survives_quote_provider_failure():
    """报价源异常时静默降级，绝不阻断K线。"""
    service = StubKlineService(
        records=[{"date": "2026-06-02", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0}]
    )

    def boom(codes):
        raise RuntimeError("quote offline")

    service.set_quote_provider(boom)

    payload, error = service.get_payload("600000")

    assert error is None
    assert payload["available"] is True
    assert payload["quoted"] is False
    assert payload["quote"] is None


def test_get_payload_without_quote_provider_is_unquoted():
    service = StubKlineService(records=[{"date": "2026-06-02", "open": 1.0, "close": 1.0}])

    payload, _ = service.get_payload("600000")

    assert payload["quoted"] is False
    assert payload["quote"] is None


class CountingKlineService(StubKlineService):
    def __init__(self, records=None):
        super().__init__(records=records)
        self.fetch_calls = 0

    def fetch_sina_kline(self, stock_code, period, limit):
        self.fetch_calls += 1
        return super().fetch_sina_kline(stock_code, period, limit)


def test_get_payload_caches_sina_records_within_ttl():
    """同一(code,period,limit)短时间内复用缓存，不重复打Sina——

    每次点K线都同步打一次Sina(超时8s)会占住后端worker、放大整页卡顿。
    """
    service = CountingKlineService(
        records=[{"date": "2026-06-02", "open": 1.0, "close": 2.0, "high": 2.0, "low": 1.0}]
    )

    first, _ = service.get_payload("600000")
    second, _ = service.get_payload("600000")

    assert service.fetch_calls == 1
    assert second["records"] == first["records"]


def test_get_payload_cache_returns_independent_copies():
    """实时报价叠加会就地改写records，缓存命中必须返回独立副本。"""
    service = CountingKlineService(
        records=[{"date": "2026-06-02", "open": 1.0, "close": 2.0, "high": 2.0, "low": 1.0}]
    )

    first, _ = service.get_payload("600000")
    first["records"][-1]["close"] = 999.0
    second, _ = service.get_payload("600000")

    assert second["records"][-1]["close"] == 2.0


def test_get_payload_cache_expires_after_ttl(monkeypatch):
    service = CountingKlineService(
        records=[{"date": "2026-06-02", "open": 1.0, "close": 2.0, "high": 2.0, "low": 1.0}]
    )

    service.get_payload("600000")
    for key, (stamp, records) in list(service._sina_cache.items()):
        service._sina_cache[key] = (stamp - service.SINA_CACHE_TTL - 1, records)
    service.get_payload("600000")

    assert service.fetch_calls == 2


def test_get_payload_cache_keyed_by_limit_and_period():
    service = CountingKlineService(
        records=[{"date": "2026-06-02", "open": 1.0, "close": 2.0, "high": 2.0, "low": 1.0}]
    )

    service.get_payload("600000", limit=120)
    service.get_payload("600000", limit=240)
    service.get_payload("600000", period="60m", limit=120)

    assert service.fetch_calls == 3


def test_get_payload_does_not_cache_failures():
    service = CountingKlineService(records=[])

    service.get_payload("600000")
    service.get_payload("600000")

    assert service.fetch_calls == 2
