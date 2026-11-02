"""v2.1.9 上线验收反例固化测试。

D-001 / D-002 为已证实但尚未修复的缺陷，使用 ``xfail(strict=True)``：
- 当前运行显示 xfail（已知失败，可追溯）；
- 一旦缺陷被修复，strict xfail 会转为 XPASS 并报错，提醒及时移除标记，防止标记被遗忘。
D-003 经核查是“代码正确、文档错误”，这里固化 rqye 的正确累加行为（正常通过）。
"""
from pathlib import Path

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
def _reset_sqlite(monkeypatch, db_path: Path):
    """把当前线程的 thread-local 连接切到隔离库，避免污染真实 data/lumo_data.sqlite。"""
    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(db_path))
    from data_store import connection
    monkeypatch.setattr(connection._local, "conn", None, raising=False)
    # 进程内 schema 标记也重置，确保隔离库独立迁移
    monkeypatch.setattr(connection, "_schema_applied", False)


@pytest.mark.xfail(strict=True,
                   reason="D-002 P3: _cached_bars 只校验 len>=max(21,min(limit,60))，"
                          "缓存不足实际 limit 时静默 cached[-limit:] 截断，不重新 fetch")
def test_d002_get_bars_should_refetch_when_cache_shorter_than_limit(tmp_path, monkeypatch):
    from data_store import screener_cache_repo
    from webui.services.kline_service import StockKlineService

    _reset_sqlite(monkeypatch, tmp_path / "iso.sqlite")

    def bar(i):
        return {"date": f"2026-08-{i:02d}", "open": 10, "close": 10 + i,
                "high": 11, "low": 9, "volume": 100}

    screener_cache_repo.put_kline("600000", [bar(i) for i in range(1, 61)])  # 仅 60 根

    svc = StockKlineService(tmp_path)
    wanted = 250
    # 用受控 stub 替换网络与本地回退，返回足量记录，避免真实网络依赖。
    monkeypatch.setattr(svc, "_fetch_sina_kline_cached",
                        lambda code, period, limit: [bar(i % 28 + 1) for i in range(limit)])
    monkeypatch.setattr(svc, "load_local_kline",
                        lambda code, period, limit: ([bar(i % 28 + 1) for i in range(limit)], "local"))

    got = svc.get_bars("600000", period="daily", limit=wanted, cache_write=False)
    assert len(got) == wanted  # 缓存不足时应重新 fetch，而非静默返回 60 根


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
