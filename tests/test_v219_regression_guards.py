"""v2.1.9 上线验收反例固化测试。D-001 仍标为预期失败。"""
from datetime import timedelta

import pytest

from analysis.market_health import market_snapshot
from analysis.market_health_fusion import margin_metrics


# ---------------------------------------------------------------- D-001
@pytest.mark.xfail(strict=True,
                   reason="D-001 P3: market_snapshot 缺 isinstance(r, dict) 防御，非 dict 元素抛 AttributeError")
@pytest.mark.parametrize("bad", [["x"], [123], [None], [["nested"]]])
def test_d001_market_snapshot_tolerates_non_dict_element(bad):
    # 健壮实现应跳过/清洗非 dict 元素，而不是让整个快照函数崩溃。
    market_snapshot(bad)


# ---------------------------------------------------------------- D-002
def test_d002_get_bars_should_refetch_when_cache_shorter_than_limit(isolated_kline, tmp_path, monkeypatch):
    from data_store import price_bars_repo
    from webui.services.kline_service import StockKlineService

    wanted = 250
    records = [{"date": (isolated_kline.date()-timedelta(days=i)).isoformat(), "close": 10}
               for i in reversed(range(wanted))]
    price_bars_repo.store('sina', 'sh600000', '1d', records[-60:])
    svc = StockKlineService(tmp_path)
    monkeypatch.setattr(svc, 'fetch_sina_kline', lambda code, period, limit: (records[-limit:], ''))
    assert len(svc.get_bars('600000', limit=wanted)) == wanted
    assert len(price_bars_repo.read('sina', 'sh600000', live_day=records[-1]['date'])) == wanted


# ---------------------------------------------------------------- D-003
def test_d003_rqye_is_yuan_balance_and_can_be_summed():
    """rqye 官方口径为融券余额(元)，与 rzye 同单位，沪深两所相加成立；代码行为正确。"""
    records = [
        {"trade_date": "20261021", "exchange_id": "SSE", "rzye": 100, "rzmre": 20,
         "rzche": 30, "rqye": 2, "rzrqye": 102},
        {"trade_date": "20261021", "exchange_id": "SZSE", "rzye": 200, "rzmre": 40,
         "rzche": 20, "rqye": 3, "rzrqye": 203},
    ]
    out = margin_metrics(records)
    assert len(out) == 1
    assert out[0]["rqye"] == 5  # 元，单位一致可相加（注意：余量是 rqyl，不是 rqye）
    assert out[0]["net_buy"] == 10  # rzmre 合计 60 - rzche 合计 50
