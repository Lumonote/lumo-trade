# tests/test_capital_rankings_service.py
"""CapitalRankingsService:主力净流入榜 / 龙虎榜 的单日·多日聚合查询、实时价叠加、回填。

全部用注入式 quote_provider / fetcher / dates,离线运行。
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from data_store.schema import migrate


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "kronos_test.sqlite"
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    migrate(c)

    _getter = lambda: c  # noqa: E731
    from data_store import (
        calendar_repo, connection, moneyflow_repo, dragon_tiger_list_repo, dragon_tiger_repo, sync_log_repo, tushare_client,
    )
    for mod in (calendar_repo, connection, moneyflow_repo, dragon_tiger_list_repo, dragon_tiger_repo, sync_log_repo):
        monkeypatch.setattr(mod, "get_conn", _getter)
    monkeypatch.setattr(tushare_client, "available", lambda: False)
    yield c


def _svc(quote_provider=None):
    from webui.services.capital_rankings_service import CapitalRankingsService
    return CapitalRankingsService(quote_provider=quote_provider)


def _seed_moneyflow(rows):
    from data_store import moneyflow_repo
    moneyflow_repo.upsert_df(pd.DataFrame(rows), top_n=0)


def _seed_dragon_tiger(rows):
    from data_store import dragon_tiger_list_repo
    dragon_tiger_list_repo.upsert_df(pd.DataFrame(rows))


def _seed_dragon_tiger_inst(rows):
    from data_store import dragon_tiger_repo
    dragon_tiger_repo.upsert_rows(rows)


# ---------- 主力净流入榜 ----------

def test_moneyflow_ranking_single(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "close": 10.0, "pct_change": 5.0},
        {"trade_date": "2026-06-04", "ts_code": "000002.SZ", "name": "乙", "net_amount": 9e7, "close": 20.0, "pct_change": 9.9},
    ])
    res = _svc().moneyflow_ranking(date="2026-06-04", top_n=10, mode="single")
    assert res["kind"] == "moneyflow"
    assert res["mode"] == "single"
    assert res["as_of"] == "2026-06-04"
    rows = res["rows"]
    assert [r["code"] for r in rows] == ["000002", "000001"]   # 裸码 + 按净流入降序
    assert rows[0]["rank"] == 1
    assert rows[0]["name"] == "乙"
    assert rows[0]["net_amount"] == pytest.approx(9e7)


def test_moneyflow_ranking_aggregate_has_list_count(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7},
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7},
        {"trade_date": "2026-06-04", "ts_code": "000002.SZ", "name": "乙", "net_amount": 1e7},
    ])
    res = _svc().moneyflow_ranking(date="2026-06-04", days=2, top_n=10, mode="aggregate")
    assert res["mode"] == "aggregate"
    top = res["rows"][0]
    assert top["code"] == "000001"
    assert top["net_amount"] == pytest.approx(5e7)
    assert top["list_count"] == 2


def test_stock_capital_summary_combines_moneyflow_and_dragon_tiger(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 1e7, "buy_elg_amount": 2e7, "buy_lg_amount": 2e7},
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 2e7, "buy_elg_amount": 3e7, "buy_lg_amount": 3e7},
        {"trade_date": "2026-06-04", "ts_code": "000002.SZ", "name": "乙",
         "net_amount": 9e7, "buy_elg_amount": 8e7, "buy_lg_amount": 8e7},
    ])
    _seed_dragon_tiger([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲",
         "l_buy": 5e7, "l_sell": 1e7, "net_amount": 4e7, "reason": "日涨幅偏离7%"},
    ])
    _seed_dragon_tiger_inst([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "inst_name": "机构专用",
         "side": "buy", "buy_amount": 2e7, "sell_amount": 0.0, "net_amount": 2e7,
         "is_quant": 0, "quant_confidence": 0.0, "reason": "日涨幅偏离7%"},
    ])

    res = _svc().stock_capital_summary("000001", date="2026-06-04", days=2)

    assert res["data_status"] == "fresh"
    assert res["moneyflow"]["row"]["rank"] == 2
    assert res["moneyflow"]["row"]["main_buy_amount"] == pytest.approx(1e8)
    dt_row = res["dragon_tiger"]["row"]
    assert dt_row["rank"] == 1
    assert dt_row["l_buy"] == pytest.approx(5e7)
    assert dt_row["institution_rows"][0]["inst_name"] == "机构专用"


def test_stock_capital_summary_supports_explicit_range(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-01", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 8e7, "buy_elg_amount": 8e7},
        {"trade_date": "2026-06-02", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 2e7, "buy_elg_amount": 2e7},
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 3e7, "buy_lg_amount": 3e7},
    ])

    res = _svc().stock_capital_summary(
        "000001",
        start_date="2026-06-02",
        end_date="2026-06-03",
    )

    assert res["mode"] == "range"
    assert res["moneyflow"]["row"]["net_amount"] == pytest.approx(5e7)
    assert res["moneyflow"]["row"]["first_date"] == "2026-06-02"
    assert res["moneyflow"]["row"]["last_date"] == "2026-06-03"
    assert res["moneyflow"]["row"]["list_count"] == 2


def test_moneyflow_ranking_exposes_all_fields_and_5_30_day_windows(conn):
    rows = []
    for day in range(1, 7):
        rows.extend([
            {
                "trade_date": f"2026-06-{day:02d}", "ts_code": "000001.SZ", "name": "甲",
                "net_amount": 1e7, "buy_elg_amount": 100.0, "buy_lg_amount": 50.0,
                "buy_md_amount": 20.0, "buy_sm_amount": 10.0, "extra_metric": "alpha",
            },
            {
                "trade_date": f"2026-06-{day:02d}", "ts_code": "000002.SZ", "name": "乙",
                "net_amount": 5e6, "buy_elg_amount": 10.0, "buy_lg_amount": 5.0,
                "buy_md_amount": 3.0, "buy_sm_amount": 2.0, "extra_metric": "beta",
            },
        ])
    _seed_moneyflow(rows)

    res = _svc().moneyflow_ranking(date="2026-06-06", top_n=10, mode="single", with_quotes=False)
    top = res["rows"][0]
    assert top["main_buy_amount"] == pytest.approx(150.0)
    assert top["retail_buy_amount"] == pytest.approx(30.0)
    assert top["amount_unit"] == "万元"
    assert top["detail_rows"][0]["amount_unit"] == "万元"
    assert top["raw"]["extra_metric"] == "alpha"

    five = res["windows"]["5"]["rows"][0]
    thirty = res["windows"]["30"]["rows"][0]
    assert five["code"] == "000001"
    assert five["main_buy_amount"] == pytest.approx(750.0)
    assert five["list_count"] == 5
    assert len(five["detail_rows"]) == 5
    assert five["detail_rows"][0]["amount_unit"] == "万元"
    assert thirty["main_buy_amount"] == pytest.approx(900.0)
    assert thirty["list_count"] == 6


def test_moneyflow_windows_rank_by_net_inflow_not_buy_alias(conn):
    """moneyflow_dc 的 buy_elg/buy_lg 与 net_amount 同为资金流向净额口径。

    5/30 日窗口应按主力净流入 net_amount 排序；main_buy_amount 只是兼容旧前端
    的显示别名，不能让榜单变成误导性的“买入额榜”。
    """
    _seed_moneyflow([
        {"trade_date": "2026-06-02", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 100.0, "buy_elg_amount": 10000.0, "buy_lg_amount": 0.0},
        {"trade_date": "2026-06-03", "ts_code": "000002.SZ", "name": "乙",
         "net_amount": 200.0, "buy_elg_amount": 1.0, "buy_lg_amount": 0.0},
        {"trade_date": "2026-06-04", "ts_code": "000003.SZ", "name": "丙",
         "net_amount": 300.0, "buy_elg_amount": 2.0, "buy_lg_amount": 0.0},
    ])

    res = _svc().moneyflow_ranking(date="2026-06-04", top_n=10, mode="single", with_quotes=False)

    assert [r["code"] for r in res["windows"]["5"]["rows"][:3]] == ["000003", "000002", "000001"]


def test_moneyflow_ranking_default_date_uses_latest(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-01", "ts_code": "000001.SZ", "net_amount": 1e7},
        {"trade_date": "2026-06-04", "ts_code": "000002.SZ", "net_amount": 2e7},
    ])
    res = _svc().moneyflow_ranking(date=None, top_n=10, mode="single")
    assert res["as_of"] == "2026-06-04"
    assert [r["code"] for r in res["rows"]] == ["000002"]


# ---------- 龙虎榜 ----------

def test_dragon_tiger_ranking_single(conn):
    _seed_dragon_tiger([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "l_buy": 4e7, "l_sell": 1e7, "reason": "r"},
        {"trade_date": "2026-06-04", "ts_code": "300750.SZ", "name": "丙", "net_amount": 8e7, "l_buy": 9e7, "l_sell": 1e7, "reason": "r"},
    ])
    res = _svc().dragon_tiger_ranking(date="2026-06-04", top_n=10, mode="single")
    assert res["kind"] == "dragon_tiger"
    rows = res["rows"]
    assert [r["code"] for r in rows] == ["300750", "000001"]
    assert rows[0]["net_amount"] == pytest.approx(8e7)


def test_dragon_tiger_ranking_aggregate_has_list_count(conn):
    _seed_dragon_tiger([
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7, "reason": "r"},
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "reason": "r"},
    ])
    res = _svc().dragon_tiger_ranking(date="2026-06-04", days=2, top_n=10, mode="aggregate")
    top = res["rows"][0]
    assert top["net_amount"] == pytest.approx(5e7)
    assert top["list_count"] == 2


def test_dragon_tiger_ranking_aggregate_accepts_explicit_date_range(conn):
    _seed_dragon_tiger([
        {"trade_date": "2026-06-01", "ts_code": "000001.SZ", "name": "甲", "net_amount": 9e7, "l_buy": 9e7, "reason": "old"},
        {"trade_date": "2026-06-02", "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7, "l_buy": 3e7, "reason": "r1"},
        {"trade_date": "2026-06-03", "ts_code": "300750.SZ", "name": "丙", "net_amount": 6e7, "l_buy": 7e7, "reason": "r2"},
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "l_buy": 4e7, "reason": "r3"},
    ])
    res = _svc().dragon_tiger_ranking(
        start_date="2026-06-02",
        end_date="2026-06-04",
        top_n=10,
        mode="aggregate",
        with_quotes=False,
    )
    assert res["start_date"] == "2026-06-02"
    assert res["end_date"] == "2026-06-04"
    assert res["as_of"] == "2026-06-04"
    by_code = {r["code"]: r for r in res["rows"]}
    assert by_code["000001"]["net_amount"] == pytest.approx(5e7)  # 排除 06-01
    assert by_code["000001"]["list_count"] == 2
    assert res["rows"][0]["code"] == "300750"  # 区间榜按龙虎榜买入额排序


def test_dragon_tiger_ranking_exposes_buy_sell_and_5_30_day_windows(conn):
    rows = []
    for day in range(1, 7):
        rows.extend([
            {
                "trade_date": f"2026-06-{day:02d}", "ts_code": "000001.SZ", "name": "甲",
                "net_amount": 1e7, "l_buy": 4e7, "l_sell": 3e7,
                "reason": f"r{day}", "seat_tag": "quant",
            },
            {
                "trade_date": f"2026-06-{day:02d}", "ts_code": "000002.SZ", "name": "乙",
                "net_amount": 2e7, "l_buy": 2e7, "l_sell": 0,
                "reason": f"x{day}", "seat_tag": "retail",
            },
        ])
    _seed_dragon_tiger(rows)

    res = _svc().dragon_tiger_ranking(date="2026-06-06", top_n=10, mode="single", with_quotes=False)
    row = {r["code"]: r for r in res["rows"]}["000001"]
    assert row["l_buy"] == pytest.approx(4e7)
    assert row["l_sell"] == pytest.approx(3e7)
    assert row["raw"]["seat_tag"] == "quant"
    assert row["detail_rows"][0]["seat_tag"] == "quant"

    five = res["windows"]["5"]["rows"][0]
    thirty = res["windows"]["30"]["rows"][0]
    assert five["code"] == "000001"  # 窗口榜按龙虎榜买入额排序
    assert five["l_buy"] == pytest.approx(2e8)
    assert five["list_count"] == 5
    assert len(five["detail_rows"]) == 5
    assert five["detail_rows"][0]["seat_tag"] == "quant"
    assert thirty["l_buy"] == pytest.approx(2.4e8)
    assert thirty["list_count"] == 6


def test_dragon_tiger_ranking_attaches_institution_amounts_and_names(conn):
    _seed_dragon_tiger([
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7, "l_buy": 3e7, "reason": "r1"},
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "l_buy": 4e7, "reason": "r2"},
    ])
    _seed_dragon_tiger_inst([
        {
            "ts_code": "000001.SZ", "trade_date": "2026-06-04",
            "inst_name": "华泰证券股份有限公司总部", "side": "buy",
            "buy_amount": 1.2e8, "sell_amount": 2e7, "net_amount": 1e8,
            "is_quant": 1, "quant_confidence": 0.9, "reason": "r2",
        },
        {
            "ts_code": "000001.SZ", "trade_date": "2026-06-04",
            "inst_name": "国泰君安证券上海分公司", "side": "buy",
            "buy_amount": 8e7, "sell_amount": 0.0, "net_amount": 8e7,
            "is_quant": 0, "quant_confidence": 0.0, "reason": "r2",
        },
        {
            "ts_code": "000001.SZ", "trade_date": "2026-06-03",
            "inst_name": "机构买入(2家)", "side": "buy",
            "buy_amount": 5e7, "sell_amount": 1e7, "net_amount": 4e7,
            "is_quant": 0, "quant_confidence": 0.0, "reason": "r1",
        },
    ])

    single = _svc().dragon_tiger_ranking(date="2026-06-04", top_n=10, mode="single", with_quotes=False)
    row = single["rows"][0]
    assert row["institution_buy_amount"] == pytest.approx(2.0e8)
    assert row["institution_sell_amount"] == pytest.approx(2.0e7)
    assert row["institution_amount"] == pytest.approx(2.2e8)
    assert row["institution_count"] == 2
    assert row["main_institution_names"].startswith("华泰证券股份有限公司总部")
    assert row["institution_daily_rows"] == [{
        "trade_date": "2026-06-04",
        "institution_buy_amount": pytest.approx(2.0e8),
        "institution_sell_amount": pytest.approx(2.0e7),
        "institution_net_amount": pytest.approx(1.8e8),
        "institution_amount": pytest.approx(2.2e8),
        "institution_count": 2,
        "buy_institution_count": 2,
    }]
    assert [r["inst_name"] for r in row["institution_rows"]] == [
        "华泰证券股份有限公司总部", "国泰君安证券上海分公司",
    ]
    assert row["institution_rows"][0]["buy_amount"] == pytest.approx(1.2e8)

    aggregate = _svc().dragon_tiger_ranking(date="2026-06-04", days=2, top_n=10, mode="aggregate", with_quotes=False)
    agg_row = aggregate["rows"][0]
    assert agg_row["institution_buy_amount"] == pytest.approx(2.5e8)
    assert agg_row["institution_amount"] == pytest.approx(2.8e8)
    by_date = {r["trade_date"]: r for r in agg_row["institution_daily_rows"]}
    assert by_date["2026-06-04"]["institution_buy_amount"] == pytest.approx(2.0e8)
    assert by_date["2026-06-03"]["institution_buy_amount"] == pytest.approx(5e7)
    assert by_date["2026-06-03"]["institution_amount"] == pytest.approx(6e7)
    assert len(agg_row["institution_rows"]) == 3


def test_dragon_tiger_institution_totals_merge_duplicate_reason_rows(conn):
    """同一机构同日同方向命中多个上榜原因时,席位金额应合计而不是被后写记录覆盖。"""
    _seed_dragon_tiger([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲",
         "net_amount": 3e7, "l_buy": 4e7, "reason": "日涨幅偏离7%"},
    ])
    _seed_dragon_tiger_inst([
        {
            "ts_code": "000001.SZ", "trade_date": "2026-06-04",
            "inst_name": "机构专用", "side": "buy",
            "buy_amount": 1.0e7, "sell_amount": 1.0e6, "net_amount": 9.0e6,
            "is_quant": 0, "quant_confidence": 0.0, "reason": "日涨幅偏离7%",
        },
        {
            "ts_code": "000001.SZ", "trade_date": "2026-06-04",
            "inst_name": "机构专用", "side": "buy",
            "buy_amount": 2.0e7, "sell_amount": 2.0e6, "net_amount": 1.8e7,
            "is_quant": 0, "quant_confidence": 0.0, "reason": "换手率达20%",
        },
    ])

    row = _svc().dragon_tiger_ranking(
        date="2026-06-04", top_n=10, mode="single", with_quotes=False,
    )["rows"][0]

    assert row["institution_buy_amount"] == pytest.approx(3.0e7)
    assert row["institution_sell_amount"] == pytest.approx(3.0e6)
    assert row["institution_net_amount"] == pytest.approx(2.7e7)
    assert row["institution_rows"][0]["reason"] == "日涨幅偏离7% / 换手率达20%"


def test_moneyflow_ranking_can_surface_matching_institution_names(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "buy_elg_amount": 100.0, "buy_lg_amount": 50.0},
    ])
    _seed_dragon_tiger_inst([
        {
            "ts_code": "000001.SZ", "trade_date": "2026-06-04",
            "inst_name": "中信证券上海分公司", "side": "buy",
            "buy_amount": 6e7, "sell_amount": 1e7, "net_amount": 5e7,
            "is_quant": 0, "quant_confidence": 0.0, "reason": "r",
        },
    ])

    row = _svc().moneyflow_ranking(date="2026-06-04", top_n=10, mode="single", with_quotes=False)["rows"][0]
    assert row["main_buy_amount"] == pytest.approx(150.0)
    assert row["amount_unit"] == "万元"
    assert row["institution_buy_amount"] == pytest.approx(6e7)
    assert row["main_institution_names"] == "中信证券上海分公司"


# ---------- 实时价叠加 ----------

def test_ranking_attaches_live_quotes(conn):
    _seed_moneyflow([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "close": 10.0},
        {"trade_date": "2026-06-04", "ts_code": "000002.SZ", "name": "乙", "net_amount": 9e7, "close": 20.0},
    ])
    captured = {}

    def fake_quotes(codes):
        captured["codes"] = list(codes)
        return {"000002": {"price": 21.5, "change_pct": 3.2, "main_net_inflow": 1e8}}

    res = _svc(quote_provider=fake_quotes).moneyflow_ranking(
        date="2026-06-04", top_n=10, mode="single", with_quotes=True,
    )
    by_code = {r["code"]: r for r in res["rows"]}
    assert set(captured["codes"]) == {"000001", "000002"}      # 用裸码批量询价
    assert by_code["000002"]["quoted"] is True
    assert by_code["000002"]["last_price"] == pytest.approx(21.5)
    assert by_code["000002"]["change_pct"] == pytest.approx(3.2)
    assert by_code["000001"]["quoted"] is False                # 无报价的票降级


# ---------- 回填 ----------

def test_backfill_persists_both_kinds(conn):
    from data_store import moneyflow_repo, dragon_tiger_list_repo

    def mf_fetcher(d):
        return pd.DataFrame([{"trade_date": d, "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7}])

    def dt_fetcher(d):
        return pd.DataFrame([{"trade_date": d, "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7, "reason": "r"}])

    summary = _svc().backfill(
        kinds=("moneyflow", "dragon_tiger"),
        dates=["20260603", "20260604"],
        moneyflow_fetcher=mf_fetcher,
        dragon_tiger_fetcher=dt_fetcher,
    )
    # moneyflow 落在哨兵 top_n=0,日期归一化为 ISO
    assert moneyflow_repo.count() == 2
    assert len(moneyflow_repo.get_ranking("2026-06-04", limit=10, snapshot_top_n=0)) == 1
    # dragon_tiger 落库
    assert dragon_tiger_list_repo.count() == 2
    assert summary["moneyflow"]["rows"] == 2
    assert summary["dragon_tiger"]["rows"] == 2


def test_backfill_persists_dragon_tiger_institution_rows(conn):
    from data_store import dragon_tiger_repo

    def dt_fetcher(d):
        return pd.DataFrame([{"trade_date": d, "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7, "reason": "r"}])

    def inst_fetcher(d):
        return pd.DataFrame([
            {
                "trade_date": d, "ts_code": "000001.SZ", "exalter": "华泰证券股份有限公司总部",
                "buy": 1.2e8, "sell": 2e7, "net_buy": 1e8, "side": "0", "reason": "r",
            },
            {
                "trade_date": d, "ts_code": "600000.SH", "exalter": "国泰君安证券上海分公司",
                "buy": 1e7, "sell": 5e7, "net_buy": -4e7, "side": "1", "reason": "r",
            },
        ])

    summary = _svc().backfill(
        kinds=("dragon_tiger",),
        dates=["20260604"],
        dragon_tiger_fetcher=dt_fetcher,
        dragon_tiger_inst_fetcher=inst_fetcher,
    )
    assert summary["dragon_tiger"]["rows"] == 1
    assert summary["dragon_tiger"]["inst_rows"] == 2
    df = dragon_tiger_repo.get_by_code("000001")
    assert df.iloc[0]["trade_date"] == "2026-06-04"
    assert df.iloc[0]["inst_name"] == "华泰证券股份有限公司总部"
    assert df.iloc[0]["side"] == "buy"
    assert df.iloc[0]["buy_amount"] == pytest.approx(1.2e8)
    other = dragon_tiger_repo.get_by_code("600000")
    assert other.iloc[0]["side"] == "sell"


def test_backfill_skips_existing_dates_when_dates_are_implicit(conn, monkeypatch):
    from data_store import tushare_client
    _seed_moneyflow([
        {"trade_date": "2026-06-04", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7},
    ])

    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: ["20260604", "20260605"])
    calls = []

    def mf_fetcher(d):
        calls.append(d)
        return pd.DataFrame([{"trade_date": d, "ts_code": "000002.SZ", "name": "乙", "net_amount": 4e7}])

    summary = _svc().backfill(kinds=("moneyflow",), days=2, moneyflow_fetcher=mf_fetcher)
    assert calls == ["20260605"]
    assert summary["moneyflow"]["requested_dates"] == 2
    assert summary["moneyflow"]["skipped_dates"] == 1
    assert summary["moneyflow"]["rows"] == 1


def test_backfill_reports_error_when_no_trade_dates(conn, monkeypatch):
    """trade_cal 失败(常见于无效 / 缺失 Token)拿不到交易日 → 回填必须显式报错,
    不能像旧逻辑那样静默 0/0 行(让 UI 误以为成功却「暂无数据」)。"""
    from data_store import tushare_client
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: [])
    monkeypatch.setattr(tushare_client, "available", lambda: True)  # pro 建好了但 trade_cal 拒绝

    summary = _svc().backfill(kinds=("moneyflow",), days=5)  # dates=None → 解析为空
    mf = summary["moneyflow"]
    assert mf["rows"] == 0
    assert mf["errors"], "拿不到交易日时必须带错误信息,不能静默成功"
    assert "Token" in mf["errors"][0][1]


def test_backfill_outcome_flags_total_failure():
    """全部 0 行且有错误 → ok=False,让 job 标记为失败而非「回填完成」。"""
    from webui.services.capital_rankings_service import backfill_outcome
    out = backfill_outcome({"moneyflow": {"rows": 0, "errors": [("", "Tushare Token 无效")]}})
    assert out["ok"] is False
    assert out["rows"] == 0
    assert any("Token" in e for e in out["errors"])


def test_backfill_outcome_ok_when_any_rows_persisted():
    """有行入库即视为成功(部分日期失败只记日志,不整单判失败)。"""
    from webui.services.capital_rankings_service import backfill_outcome
    out = backfill_outcome({
        "moneyflow": {"rows": 5, "errors": []},
        "dragon_tiger": {"rows": 0, "errors": [("20260601", "x")]},
    })
    assert out["ok"] is True
    assert out["rows"] == 5
    assert out["errors"]  # 仍带上部分错误供日志


def test_backfill_outcome_counts_institution_rows():
    from webui.services.capital_rankings_service import backfill_outcome
    out = backfill_outcome({"dragon_tiger": {"rows": 0, "inst_rows": 3, "errors": []}})
    assert out["ok"] is True
    assert out["rows"] == 3


# ---------- 「无量化」过滤 ----------

_QINFO = {"codes": {"000002"}, "as_of": "2026-06-04", "available": True}


def _svc_nq(qinfo=_QINFO):
    from webui.services.capital_rankings_service import CapitalRankingsService
    return CapitalRankingsService(quote_provider=None, quant_codes_fn=lambda: qinfo)


def _seed_three(date="2026-06-04"):
    _seed_moneyflow([
        {"trade_date": date, "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7, "close": 10.0, "pct_change": 5.0},
        {"trade_date": date, "ts_code": "000002.SZ", "name": "乙", "net_amount": 9e7, "close": 20.0, "pct_change": 9.9},
        {"trade_date": date, "ts_code": "000003.SZ", "name": "丙", "net_amount": 1e7, "close": 5.0, "pct_change": 1.0},
    ])


def test_moneyflow_no_quant_filters_rows_and_windows(conn):
    _seed_three()
    res = _svc_nq().moneyflow_ranking(date="2026-06-04", top_n=10, mode="single", no_quant=True)
    codes = [r["code"] for r in res["rows"]]
    assert "000002" not in codes and set(codes) == {"000001", "000003"}
    assert res["no_quant"] is True
    assert res["quant_filtered"] == 1
    assert res["quant_criteria_available"] is True
    assert res["quant_as_of"] == "2026-06-04"
    for win in ("5", "30"):
        assert res["windows"][win]["quant_filtered"] == 1
        assert all(r["code"] != "000002" for r in res["windows"][win]["rows"])


def test_moneyflow_no_quant_overfetch_refills_top_n(conn):
    # top_n=1 且榜首 000002 是量化股:over-fetch 后仍能给出 1 行(次名 000001)
    _seed_three()
    res = _svc_nq().moneyflow_ranking(date="2026-06-04", top_n=1, mode="single", no_quant=True)
    assert [r["code"] for r in res["rows"]] == ["000001"]
    assert res["top_n"] == 1 and res["count"] == 1


def test_moneyflow_default_no_quant_off_and_lazy(conn):
    _seed_three()
    called = []
    from webui.services.capital_rankings_service import CapitalRankingsService
    svc = CapitalRankingsService(quant_codes_fn=lambda: called.append(1) or _QINFO)
    res = svc.moneyflow_ranking(date="2026-06-04", top_n=10, mode="single")
    assert res["no_quant"] is False and res["quant_filtered"] == 0
    assert {r["code"] for r in res["rows"]} == {"000001", "000002", "000003"}
    assert not called  # 未勾选时不触碰量化代码集


def test_moneyflow_no_quant_criteria_unavailable_noop(conn):
    _seed_three()
    res = _svc_nq({"codes": set(), "as_of": None, "available": False}).moneyflow_ranking(
        date="2026-06-04", top_n=10, mode="single", no_quant=True)
    assert res["quant_criteria_available"] is False
    assert res["quant_filtered"] == 0
    assert {r["code"] for r in res["rows"]} == {"000001", "000002", "000003"}


def test_dragon_tiger_no_quant(conn):
    _seed_dragon_tiger([
        {"trade_date": "2026-06-04", "ts_code": "000002.SZ", "name": "乙", "l_buy": 9e7, "l_sell": 1e7, "net_rate": 5.0},
        {"trade_date": "2026-06-04", "ts_code": "600001.SH", "name": "丁", "l_buy": 5e7, "l_sell": 2e7, "net_rate": 3.0},
    ])
    res = _svc_nq().dragon_tiger_ranking(date="2026-06-04", top_n=10, mode="single", no_quant=True)
    assert [r["code"] for r in res["rows"]] == ["600001"]
    assert res["quant_filtered"] == 1


# ---------- 按需自动补齐（auto_backfill_if_stale） ----------

def test_auto_backfill_if_stale_triggers_when_data_stale(conn, monkeypatch):
    """资金榜/龙虎榜最新日期落后于最近交易日 → 自动触发回填。"""
    from data_store import tushare_client
    from webui.services import capital_rankings_service
    from webui.services.capital_rankings_service import (
        auto_backfill_if_stale, CapitalRankingsService,
    )
    # 已有 3 个交易日前数据（滞后）
    _seed_moneyflow([
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7},
    ])
    _seed_dragon_tiger([
        {"trade_date": "2026-06-03", "ts_code": "000001.SZ", "name": "甲",
         "l_buy": 5e7, "l_sell": 1e7, "net_amount": 4e7, "reason": "r"},
    ])
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: ["20260610"])
    monkeypatch.setattr(capital_rankings_service, "_auto_backfill_last_ts", {})
    # 短路真实回填（避免网络），记录触发
    calls = []
    monkeypatch.setattr(CapitalRankingsService, "backfill", lambda self, **kw: calls.append(kw) or {
        "moneyflow": {"rows": 1, "errors": []},
        "dragon_tiger": {"rows": 1, "errors": []},
    })

    res = auto_backfill_if_stale(kinds=("moneyflow", "dragon_tiger"), max_days=2)
    assert res["triggered"] is True
    assert res["kinds"] == ["moneyflow", "dragon_tiger"]
    assert calls, "数据滞后时应当真正触发一次回填"


def test_auto_backfill_if_stale_skips_when_fresh(conn, monkeypatch):
    """数据已新鲜（最新交易日已入库）→ 不触发回填。"""
    from data_store import tushare_client
    from webui.services.capital_rankings_service import auto_backfill_if_stale
    _seed_moneyflow([
        {"trade_date": "2026-06-10", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7},
    ])
    _seed_dragon_tiger([
        {"trade_date": "2026-06-10", "ts_code": "000001.SZ", "name": "甲",
         "l_buy": 5e7, "l_sell": 1e7, "net_amount": 4e7, "reason": "r"},
    ])
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: ["20260610"])
    res = auto_backfill_if_stale(kinds=("moneyflow", "dragon_tiger"), max_days=2)
    assert res["triggered"] is False
    assert res["reason"] == "fresh"


def test_auto_backfill_if_stale_detects_skipped_middle_days(conn, monkeypatch):
    """最新日期已有数据，但中间某交易日被跳过 → 仍应检测到缺口并补齐。

    这是「有些日期实际是交易日但直接跳过了」的核心场景：不能只看最新日期。
    """
    from data_store import tushare_client
    from webui.services import capital_rankings_service
    from webui.services.capital_rankings_service import (
        auto_backfill_if_stale, CapitalRankingsService,
    )
    # 最新交易日 2026-06-10 已入库（最新日期不滞后），但中间 06-08/06-09 缺失
    _seed_moneyflow([
        {"trade_date": "2026-06-10", "ts_code": "000001.SZ", "name": "甲", "net_amount": 3e7},
        {"trade_date": "2026-06-05", "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7},
    ])
    _seed_dragon_tiger([
        {"trade_date": "2026-06-10", "ts_code": "000001.SZ", "name": "甲",
         "l_buy": 5e7, "l_sell": 1e7, "net_amount": 4e7, "reason": "r"},
    ])
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: [
        "20260610", "20260609", "20260608", "20260605",
    ])
    monkeypatch.setattr(capital_rankings_service, "_auto_backfill_last_ts", {})
    calls = []
    monkeypatch.setattr(CapitalRankingsService, "backfill", lambda self, **kw: calls.append(kw) or {
        "moneyflow": {"rows": 1, "errors": []},
        "dragon_tiger": {"rows": 1, "errors": []},
    })

    res = auto_backfill_if_stale(kinds=("moneyflow", "dragon_tiger"), max_days=10)
    assert res["triggered"] is True, "最新日期有数据但中间有缺口时也必须触发"
    # 补齐的日期应包含被跳过的中间交易日（YYYYMMDD）
    mf_missing = res["missing"].get("moneyflow", [])
    assert "20260608" in mf_missing
    assert "20260609" in mf_missing
    dt_missing = res["missing"].get("dragon_tiger", [])
    assert "20260608" in dt_missing and "20260609" in dt_missing
    # backfill 应按缺失日期精确传入
    assert calls, "检测到缺口时应当触发回填"
    assert calls[0]["dates"] == ["20260608", "20260609"] or calls[0]["dates"]


def test_auto_backfill_if_stale_skips_when_tushare_unavailable(conn, monkeypatch):
    """Tushare 不可用 → 静默跳过，不报错。"""
    from data_store import tushare_client
    from webui.services.capital_rankings_service import auto_backfill_if_stale
    monkeypatch.setattr(tushare_client, "available", lambda: False)
    res = auto_backfill_if_stale(kinds=("moneyflow", "dragon_tiger"), max_days=2)
    assert res["triggered"] is False
    assert res["reason"] == "tushare_unavailable"


def test_request_window_returns_yyyymmdd_filtered_by_range(conn, monkeypatch):
    """_request_window 返回 YYYYMMDD 且按显式区间过滤。"""
    from data_store import calendar_repo, market_daily_fetch
    from webui.services.capital_rankings_service import CapitalRankingsService
    calendar_repo.upsert(["20071231", "20080102", "20080103", "20080104", "20080107"])
    calls = []
    monkeypatch.setattr(market_daily_fetch, "open_days", lambda start, end: calls.append((start, end)) or ["20071226", "20071227"])
    svc = CapitalRankingsService()
    assert svc._request_window(1, start_date="2008-01-02", end_date="2008-01-04") == [
        "20080104", "20080103", "20080102",
    ]
    assert not calls
    assert svc._request_window(2, date="2008-01-04") == ["20080104", "20080103"]
    assert not calls
    assert svc._request_window(2, date="2007-12-27") == ["20071227", "20071226"]
    assert calls[0][1] == "20071227"


def test_historical_dragon_tiger_ranking_backfills_exact_days_and_reads_sqlite(conn, monkeypatch):
    from data_store import calendar_repo, tushare_client
    from webui.services import capital_rankings_service as capital

    calendar_repo.upsert(["20080102", "20080103", "20080104", "20080107"])
    _seed_dragon_tiger([{"trade_date": "2008-01-02", "ts_code": "600001.SH", "name": "甲", "l_buy": 11}])
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(capital, "_history_attempts", {})
    calls = []
    service = _svc()

    def backfill(*, kinds, dates):
        calls.append((kinds, dates))
        _seed_dragon_tiger([
            {"trade_date": capital._to_iso(day), "ts_code": "600001.SH", "name": "甲", "l_buy": 5}
            for day in dates
        ])

    monkeypatch.setattr(service, "backfill", backfill)
    monkeypatch.setattr(capital, "auto_backfill_background", lambda *_: pytest.fail("Historical queries never fetch recent days"))
    args = {"mode": "aggregate", "start_date": "2008-01-02", "end_date": "2008-01-07", "with_quotes": False}
    report = service.dragon_tiger_ranking(**args)
    again = service.dragon_tiger_ranking(**args)
    assert calls == [(('dragon_tiger',), ["20080107", "20080104", "20080103"])]
    assert report == again and report["rows"][0]["list_count"] == 4
    assert report["coverage"]["stored_days"] == 4 and report["reason"] is None


def test_moneyflow_before_coverage_does_not_substitute_a_recent_date(conn, monkeypatch):
    from data_store import tushare_client

    _seed_moneyflow([{"trade_date": "2026-06-08", "ts_code": "600001.SH", "name": "甲", "net_amount": 20}])
    monkeypatch.setattr(tushare_client, "available", lambda: pytest.fail("Provider cannot cover this date"))
    result = _svc().moneyflow_ranking(date="2008-01-02", with_quotes=False)
    assert result["as_of"] == "2008-01-02"
    assert result["rows"] == [] and "2023-09-11" in result["reason"]


# ---------- 按需自动补齐:缺口判据(残缺日/当日/分 kind 冷却) ----------

def _stub_backfill(monkeypatch, calls):
    """短路真实回填,记录每次 backfill 的 kwargs。"""
    from webui.services.capital_rankings_service import CapitalRankingsService
    monkeypatch.setattr(
        CapitalRankingsService, "backfill",
        lambda self, **kw: calls.append(kw) or {
            k: {"rows": 1, "errors": []} for k in kw.get("kinds", ())
        },
    )


def _reset_cooldown(monkeypatch):
    from webui.services import capital_rankings_service as C
    monkeypatch.setattr(C, "_auto_backfill_last_ts", {}, raising=False)


def test_auto_backfill_refetches_partial_day(conn, monkeypatch):
    """某交易日只入库了零星几行(上次抓取被截断) → 必须判为缺失并重抓。

    实测 moneyflow_dc 正常日约 5900 行,而 2026-08-12 只有 550 行、08-14 只有
    1050 行;若只按「该日期有没有行」判定,残缺日会被永久当成已完成,榜单
    从此在残缺的全集上排名。
    """
    from data_store import tushare_client
    from webui.services.capital_rankings_service import auto_backfill_if_stale
    _seed_moneyflow([
        {"trade_date": "2026-06-10", "ts_code": f"{i:06d}.SZ", "name": f"股{i}",
         "net_amount": 1e7} for i in range(1, 4001)
    ])                                                    # 完整日:4000 行
    _seed_moneyflow([
        {"trade_date": "2026-06-09", "ts_code": "000001.SZ", "name": "甲", "net_amount": 2e7},
    ])                                                    # 残缺日:1 行
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates",
                        lambda *a, **k: ["20260610", "20260609"])
    _reset_cooldown(monkeypatch)
    calls = []
    _stub_backfill(monkeypatch, calls)

    res = auto_backfill_if_stale(kinds=("moneyflow",), max_days=5)
    assert res["triggered"] is True, "残缺日必须被判为缺口"
    assert res["missing"]["moneyflow"] == ["20260609"]
    assert "20260610" not in res["missing"]["moneyflow"], "完整日不应重抓"


def test_auto_backfill_dragon_tiger_row_count_not_required(conn, monkeypatch):
    """龙虎榜每天上榜数天然只有几十条,不能套用行数阈值,有行即算已补。"""
    from data_store import tushare_client
    from webui.services.capital_rankings_service import auto_backfill_if_stale
    _seed_dragon_tiger([
        {"trade_date": "2026-06-10", "ts_code": "000001.SZ", "name": "甲",
         "l_buy": 5e7, "l_sell": 1e7, "net_amount": 4e7, "reason": "r"},
    ])
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: ["20260610"])
    _reset_cooldown(monkeypatch)
    res = auto_backfill_if_stale(kinds=("dragon_tiger",), max_days=5)
    assert res["triggered"] is False and res["reason"] == "fresh"


def test_auto_backfill_skips_today_before_publish_cutoff(conn, monkeypatch):
    """当日资金流/龙虎榜要收盘后才发布 → 截止时刻前不得把「今天」当缺口。

    否则每次请求都会去拉一个注定拉不到的今天,还会吃掉冷却窗口,
    真正缺的历史交易日反而永远排不上。
    """
    import datetime as dt
    from data_store import tushare_client
    from webui.services import capital_rankings_service as C
    today = dt.date.today().strftime("%Y%m%d")
    _seed_moneyflow([
        {"trade_date": "2026-01-05", "ts_code": "000001.SZ", "name": "甲", "net_amount": 1e7},
    ])
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: [today])
    monkeypatch.setattr(C, "_publish_cutoff_passed", lambda: False)
    _reset_cooldown(monkeypatch)
    calls = []
    _stub_backfill(monkeypatch, calls)

    res = C.auto_backfill_if_stale(kinds=("moneyflow",), max_days=5)
    assert res["triggered"] is False, "发布时刻前不应为「今天」触发回填"
    assert not calls


def test_auto_backfill_cooldown_is_per_kind(conn, monkeypatch):
    """冷却必须按 kind 分桶:资金榜刚补过,不能把龙虎榜的补齐机会一起堵死。

    资金榜单页会同时打 moneyflow 与 dragon_tiger 两个接口,共用一个全局
    时间戳时,先到的那个独占,另一个 10 分钟内永远拿不到补数机会。
    """
    from data_store import tushare_client
    from webui.services.capital_rankings_service import auto_backfill_if_stale
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: ["20260610"])
    _reset_cooldown(monkeypatch)
    calls = []
    _stub_backfill(monkeypatch, calls)

    first = auto_backfill_if_stale(kinds=("moneyflow",), max_days=5)
    assert first["triggered"] is True
    second = auto_backfill_if_stale(kinds=("dragon_tiger",), max_days=5)
    assert second["triggered"] is True, "另一个 kind 不应被 moneyflow 的冷却挡住"
    again = auto_backfill_if_stale(kinds=("moneyflow",), max_days=5)
    assert again["triggered"] is False and again["reason"] == "cooldown"


def test_auto_backfill_budget_caps_days_per_run(conn, monkeypatch):
    """一次最多补 max_fetch 天,避免个股查询被整月回填拖死。"""
    from data_store import tushare_client
    from webui.services.capital_rankings_service import auto_backfill_if_stale
    monkeypatch.setattr(tushare_client, "available", lambda: True)
    monkeypatch.setattr(tushare_client, "recent_trade_dates", lambda *a, **k: [
        "20260610", "20260609", "20260608", "20260605", "20260604",
    ])
    _reset_cooldown(monkeypatch)
    calls = []
    _stub_backfill(monkeypatch, calls)

    res = auto_backfill_if_stale(kinds=("moneyflow",), max_days=10, max_fetch=2)
    assert res["triggered"] is True
    assert calls[0]["dates"] == ["20260610", "20260609"], "应只补最近 2 天"
    assert res["remaining"] == 3
