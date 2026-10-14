"""恐慌强度的方向、边界、缺失数据和各行情入口回归。"""
from datetime import date, timedelta
import copy
import json

import pytest

from analysis.panic_indicator import (
    panic_indicator, panic_from_bars, panic_from_quotes, panic_from_sector_series,
)


def _bars(changes):
    close, rows = 100.0, []
    for i, pct in enumerate(changes):
        close *= 1+pct/100
        rows.append({"date": (date(2026, 9, 1)+timedelta(days=i)).isoformat(),
                     "close": close, "volume": 1000.0})
    return rows


@pytest.mark.parametrize("scope,scale", [("index",3), ("market",3), ("sector",5), ("stock",10)])
def test_snapshot_score_direction_and_full_scale(scope,scale):
    assert panic_indicator(change_pct=-scale,scope=scope)["score"] == 100
    assert panic_indicator(change_pct=scale,scope=scope)["score"] == 0
    assert panic_indicator(change_pct=0,scope=scope)["score"] == 50


@pytest.mark.parametrize("value", [None, "", "N/A", "—", float("nan"), float("inf"), -float("inf"), True])
def test_missing_and_nonfinite_are_unavailable_not_neutral(value):
    out = panic_indicator(change_pct=value)
    assert out["score"] is None and out["label"] == "数据不足"
    assert out["available"] is False and out["factors"] == []
    json.dumps(out, allow_nan=False)


def test_missing_factors_renormalize_without_inventing_data():
    out = panic_indicator(change_pct=-3,scope="index",breadth="N/A",drawdown_pct=None)
    assert out["score"] == 100 and out["quality"] == "snapshot" and out["coverage"] == .4
    assert [f["key"] for f in out["factors"]] == ["change"]


def test_down_volume_increases_panic_but_up_volume_does_not():
    quiet = panic_indicator(change_pct=-5,volume_ratio=1)
    selling = panic_indicator(change_pct=-5,volume_ratio=4)
    assert selling["score"] > quiet["score"]
    assert panic_indicator(change_pct=5,volume_ratio=4)["score"] == panic_indicator(change_pct=5,volume_ratio=1)["score"]


def test_downtrend_is_more_panicked_than_uptrend_and_inputs_are_immutable():
    falling, rising = _bars([-2]*25), _bars([2]*25)
    before = copy.deepcopy(falling)
    falling[-1]["volume"] = 4000
    expected = copy.deepcopy(falling)
    out = panic_from_bars(falling)
    assert out["score"] > panic_from_bars(rising)["score"]
    assert {f["key"] for f in out["factors"]} == {"change","drawdown","volume"}
    assert falling == expected and before != falling


def test_latest_missing_close_does_not_use_previous_bar_as_latest():
    rows = _bars([1]*25)
    rows[-1]["close"] = None
    out = panic_from_bars(rows)
    assert out["available"] is False
    assert out["as_of"] == rows[-1]["date"]


def test_history_gap_does_not_generate_twenty_period_drawdown():
    rows = _bars([-1]*25)
    rows[-8]["close"] = None
    assert "drawdown" not in {f["key"] for f in panic_from_bars(rows)["factors"]}


def test_quote_only_works_without_kline_and_non_applied_quote_is_not_live():
    out = panic_from_bars([],quote={"change_pct":-8,"updated_at":"2026-10-14T10:00:00"})
    assert out["available"] and out["score"] == 90 and out["quality"] == "snapshot"
    out = panic_from_bars(_bars([1]*25),quote={"applied":False})
    assert out["provisional"] is False
    assert panic_from_bars(_bars([1]*25),quote={"applied":True})["provisional"] is True


def test_sector_uses_breadth_and_preserves_stale_state():
    rows = [{"trade_date":r["date"],"pct_chg_mean":-2,"breadth":.1} for r in _bars([-2]*25)]
    out = panic_from_sector_series(rows,stale=True)
    assert out["scope"] == "sector" and out["stale"]
    assert {f["key"] for f in out["factors"]} == {"change","drawdown","breadth"}
    assert out["score"] > 60
    drawdown = next(f["value"] for f in out["factors"] if f["key"] == "drawdown")
    bar_drawdown = next(f["value"] for f in panic_from_bars(_bars([-2]*25))["factors"] if f["key"] == "drawdown")
    assert drawdown == bar_drawdown


def test_aggregate_excludes_missing_unavailable_and_invalid_quotes():
    rows = [{"change_pct":-3},{"change_pct":None},{"change_pct":float("nan")},
            {"change_pct":99,"available":False},{"change_pct":-101}]
    out = panic_from_quotes(rows)
    assert out["sample_count"] == 1 and out["score"] == 100
    assert panic_from_quotes([])["available"] is False


def test_cloud_and_dashboard_share_market_panic_while_stock_scopes_remain_independent(monkeypatch):
    import time
    import webui.core as core

    monkeypatch.setattr(core,"_load_market_intelligence",lambda: {"eastmoney":{"industry_boards":[
        {"code":"BK1","name":"银行","change_pct":-4}, {"code":"BK2","name":"未知"}]}})
    monkeypatch.setitem(core.market_state,"index_last_update",time.time())
    monkeypatch.setitem(core.market_state,"indices",{"sh000001":{"price":3000,"change_pct":-2}})
    dashboard = core._build_market_dashboard()
    assert dashboard["indices"][0]["panic"]["scope"] == "index"
    assert dashboard["panic"]["available"]
    assert dashboard["sectors"][0]["panic"]["available"]
    assert dashboard["sectors"][1]["panic"]["available"] is False
    monkeypatch.setitem(core.market_state,"indices",{"sh000001":{"price":3000}})
    unavailable = core._build_market_dashboard()
    assert unavailable["indices"][0]["panic"]["available"] is False
    assert unavailable["panic"]["available"] is False
    monkeypatch.setattr(core.MARKET_INTELLIGENCE_SERVICE,"fetch_market_cloud_stocks",lambda **kwargs: [
        {"code":"600001","industry":"银行","change_pct":-5,"trade_date":"20261014"},
        {"code":"600002","industry":"银行","change_pct":2,"trade_date":"20261014"},
        {"code":"600003","industry":"银行","change_pct":None,"trade_date":"20261014"}])
    # 大盘恐慌统一为指数口径：云图徽章与总览消费同一份 dashboard 指标，两页永远一致。
    monkeypatch.setitem(core.market_state,"indices",{"sh000001":{"price":3000,"change_pct":-2}})
    cloud = core._market_cloud_payload()
    assert cloud["panic"] == core._build_market_dashboard()["panic"]
    # 样本数=1 指的是唯一有涨跌的指数（sh000001），不是云图个股样本（3 只里的 2 只）。
    assert cloud["panic"]["sample_count"] == 1
    assert any(f["key"] == "breadth" and "指数" in f["label"] for f in cloud["panic"]["factors"])
    # 板块与个股层仍是各自样本的独立口径。
    assert cloud["sector_panics"]["银行"]["scope"] == "sector"
    assert cloud["stocks"][0]["panic"]["scope"] == "stock"
    assert cloud["stocks"][2]["panic"]["available"] is False
    json.dumps(cloud,allow_nan=False)


def test_kline_service_payload_includes_calculated_panic_without_extra_fetch(tmp_path,monkeypatch):
    from webui.services.kline_service import StockKlineService
    service = StockKlineService(tmp_path)
    calls = []
    monkeypatch.setattr(service,"fetch_sina_kline",lambda *args: (calls.append(args) or _bars([-1]*25),""))
    out,error = service.get_payload("600001")
    assert error is None and len(calls) == 1
    assert out["panic"]["available"] and out["panic"]["scope"] == "stock"
