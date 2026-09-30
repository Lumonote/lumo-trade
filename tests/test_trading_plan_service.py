from datetime import datetime
import json

import pytest

from tests.test_trading_plan import TODAY, bars, pulse
from webui.services.trading_plan_service import TradingPlanService


def service(tmp_path, **overrides):
    codes = ["000001", "600000", "600036"]
    options = dict(store_path=tmp_path / "plans.json",
        watchlist=lambda: [{"code": c, "name": c} for c in codes],
        opportunities=lambda: {"date": TODAY.isoformat(), "items": []},
        market_pulse=pulse, holdings=lambda: {"account": {"cash": 100000, "total_equity": 100000}, "positions": []},
        quotes=lambda selected: {c: {"price": bars()["records"][-1]["close"], "quote_date": TODAY.isoformat()} for c in selected},
        kline=lambda c: bars(), metadata=lambda c: {"sector": "银行", "boards": ["银行"]},
        now=lambda: datetime(2026, 9, 30, 14, 0))
    return TradingPlanService(**{**options, **overrides})


def lock(svc, item):
    return svc.lock_plan({"code": item["code"], "expected_revision": item["revision"],
                          "basis_key": item["candidate"]["basis_key"]})


def test_batch_allocations_share_sector_and_total_budget(tmp_path):
    out = service(tmp_path).overview()
    items = out["items"]
    assert sum(p["new_position_pct"] for p in items) <= out["market"]["sector_cap_pct"]
    assert sum(p["new_position_pct"] for p in items) <= out["market"]["new_budget_pct"]
    assert items[0]["trial_position_pct"] == pytest.approx(items[0]["new_position_pct"] * .4)
    assert items[-1]["new_position_pct"] < items[0]["new_position_pct"]


def test_saved_stop_does_not_follow_new_prices_and_reads_do_not_write(tmp_path):
    svc = service(tmp_path)
    old = svc.overview(code="000001")["items"][0]
    assert lock(svc, old)[1] == 200
    text = svc.store_path.read_text()
    svc._quotes = lambda codes: {"000001": {"price": old["levels"]["stop_loss"] - .1, "quote_date": TODAY.isoformat()}}
    svc._holdings = lambda: {"account": {"cash": 99000, "total_equity": 100000},
                            "positions": [{"ts_code": "000001.SZ", "qty": 100, "market_value": 1000}]}
    updated = svc.overview(code="000001")["items"][0]
    assert updated["status"] == "exit"
    assert updated["levels"]["stop_loss"] == old["levels"]["stop_loss"]
    assert svc.store_path.read_text() == text


def test_conflicting_revision_and_price_basis_rejected(tmp_path):
    svc = service(tmp_path)
    item = svc.overview(code="000001")["items"][0]
    assert lock(svc, item)[1] == 200
    assert lock(svc, item)[1] == 409
    item["revision"] = 1
    item["candidate"]["basis_key"] = "stale"
    assert lock(svc, item)[1] == 409


def test_corrupt_plan_file_is_not_silently_overwritten(tmp_path):
    svc = service(tmp_path)
    svc.store_path.write_text("broken")
    with pytest.raises(ValueError):
        svc.overview()
    assert svc.store_path.read_text() == "broken"


def test_pagination_avoids_fetching_every_stock(tmp_path):
    seen = []
    svc = service(tmp_path, kline=lambda c: seen.append(c) or bars())
    out = svc.overview(offset=1, limit=1)
    assert seen == ["600000"]
    assert out["total"] == 3


def test_unavailable_market_does_not_turn_unknown_into_sell_signal(tmp_path):
    svc = service(tmp_path, market_pulse=lambda: {}, holdings=lambda: {
        "account": {"cash": 99000, "total_equity": 100000},
        "positions": [{"ts_code": "000001", "qty": 100, "market_value": 1000}]})
    item = svc.overview(code="000001")["items"][0]
    assert item["status"] == "hold"
    assert item["new_position_pct"] == 0


def test_failed_kline_only_degrades_one_plan(tmp_path):
    def kline(code):
        if code == "000001":
            raise RuntimeError("offline")
        return bars()
    out = service(tmp_path, kline=kline).overview()
    assert not out["items"][0]["available"]
    assert out["items"][1]["available"]


def test_held_plan_reset_cannot_lower_stop(tmp_path):
    svc = service(tmp_path)
    item = svc.overview(code="000001")["items"][0]
    assert lock(svc, item)[1] == 200
    saved = json.loads(svc.store_path.read_text())
    old_stop = saved["plans"]["000001"]["levels"]["stop_loss"] + .01
    saved["plans"]["000001"]["levels"]["stop_loss"] = old_stop
    svc.store_path.write_text(json.dumps(saved))
    svc._holdings = lambda: {"account": {"cash": 99000, "total_equity": 100000},
                            "positions": [{"ts_code": "000001", "qty": 100, "market_value": 1000}]}
    item = svc.overview(code="000001")["items"][0]
    assert lock(svc, item)[1] == 200
    assert json.loads(svc.store_path.read_text())["plans"]["000001"]["levels"]["stop_loss"] >= old_stop


def test_missing_quote_for_other_holding_blocks_portfolio_additions(tmp_path):
    svc = service(tmp_path, quotes=lambda codes: {"000001": {
        "price": bars()["records"][-1]["close"], "quote_date": TODAY.isoformat()}},
        holdings=lambda: {"account": {"cash": 99000, "total_equity": 100000},
                          "positions": [{"ts_code": "600000", "qty": 100, "market_value": 1000}]})
    out = svc.overview(code="000001")
    assert out["degraded"]["holdings_quotes"]
    assert not out["market"]["entry_allowed"]
    assert not out["market"]["account_available"]
    assert out["market"]["used_position_pct"] is None
    assert out["market"]["reduce_pct"] == 0
    assert out["items"][0]["new_position_pct"] == 0
