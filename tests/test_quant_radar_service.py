"""量化交易分析(Quant Radar)服务纯函数测试 —— 离线,不联网。

覆盖 :mod:`webui.services.quant_radar_service`:
盘口异动聚合、五机制评分(幌骗/高频/订单簿/情绪/行为偏差)、综合活跃度与预警等级、
个股与市场行为预测文案、板块聚合、知识卡、kv 快照回退,以及
:func:`data_store.dragon_tiger_repo.get_quant_by_date`。
设计 spec: docs/superpowers/specs/2026-07-13-quant-radar-tab-design.md
"""

import pytest
import datetime as dt
from types import SimpleNamespace

from webui.services import quant_radar_service as qr


def freeze_trade_day(monkeypatch,day):
    """历史场景显式固定交易日，避免运行日期变化使固定样本落出窗口。"""
    from data_store import quant_radar_repo
    class FixedDate(dt.date):
        @classmethod
        def today(cls):return cls.fromisoformat(day)
    frozen=SimpleNamespace(date=FixedDate,datetime=dt.datetime,timedelta=dt.timedelta,timezone=dt.timezone)
    monkeypatch.setattr(quant_radar_repo,"_dt",frozen)
    monkeypatch.setattr(qr,"_dt",frozen)
    monkeypatch.setattr(qr,"_current_trade_date_key",lambda:day.replace("-",""))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(tmp_path / "k.sqlite"))
    # connection 是 thread-local 单例,换路径后需重置
    import data_store.connection as conn_mod

    if getattr(conn_mod._local, "conn", None) is not None:
        conn_mod._local.conn.close()
        conn_mod._local.conn = None
    conn_mod._schema_applied = False
    yield
    if getattr(conn_mod._local, "conn", None) is not None:
        conn_mod._local.conn.close()
        conn_mod._local.conn = None
    conn_mod._schema_applied = False


def _change(code, t, name="测试股", tm=93500):
    return {"c": code, "n": name, "t": t, "tm": tm, "i": ""}


def _bar(pct_chg=0.0, open_=10.0, close=10.0, high=None, low=None, volume=1e6, date="2026-07-10"):
    high = high if high is not None else max(open_, close)
    low = low if low is not None else min(open_, close)
    return {"date": date, "open": open_, "close": close, "high": high, "low": low,
            "volume": volume, "amount": volume * close, "pct_chg": pct_chg}


# ----------------------------- 异动聚合 -----------------------------

def test_aggregate_changes_counts_direction_and_ignores_unknown():
    rows = [
        _change("600000", 8193), _change("600000", 8193), _change("600000", "8201"),
        _change("600000", 8194), _change("600000", 16),
        _change("600000", 99999),  # 未知类型忽略
        _change("000001", 4, name="平安银行"),
    ]
    agg = qr._aggregate_changes(rows)
    a = agg["600000"]
    assert a["counts"]["big_buy"] == 2
    assert a["counts"]["rocket"] == 1
    assert a["counts"]["open_up"] == 1
    assert a["total"] == 5  # 未知类型不计
    assert a["bull"] == 3 and a["bear"] == 1  # 炸板算中性
    assert agg["000001"]["bull"] == 1


# ----------------------------- 五机制评分 -----------------------------

def test_spoof_score_flags_heavy_buy_orders_without_price_follow_through():
    agg = {"counts": {"big_buy": 3, "buy_queue": 1}, "total": 4, "bull": 4, "bear": 0}
    score, reasons = qr._spoof_score(agg, pct_chg=0.2, upper_shadow_ratio=0.1)
    assert score >= 55
    assert any("诱多" in r for r in reasons)


def test_spoof_score_low_when_price_confirms_buying():
    agg = {"counts": {"big_buy": 4}, "total": 4, "bull": 4, "bear": 0}
    score, reasons = qr._spoof_score(agg, pct_chg=6.0, upper_shadow_ratio=0.05)
    assert score <= 20


def test_spoof_score_flags_sell_wall_absorption():
    agg = {"counts": {"big_sell": 3, "sell_queue": 1}, "total": 4, "bull": 0, "bear": 4}
    score, reasons = qr._spoof_score(agg, pct_chg=0.3, upper_shadow_ratio=0.0)
    assert score >= 50
    assert any("吸筹" in r or "诱空" in r for r in reasons)


def test_hft_score_stacks_burst_reversal_and_volume_ratio():
    agg = {"counts": {"rocket": 2, "dive": 1, "big_buy": 3}, "total": 6, "bull": 5, "bear": 1}
    score, reasons = qr._hft_score(agg, volume_ratio=3.5)
    assert score >= 90
    assert any("拉升" in r and "跳水" in r for r in reasons)
    quiet, _ = qr._hft_score({"counts": {}, "total": 0, "bull": 0, "bear": 0}, volume_ratio=1.0)
    assert quiet == 0


def test_orderbook_score_uses_bar_features_and_limit_reopen():
    bars = [_bar(pct_chg=0.5, open_=10, close=10.05, high=10.9, low=9.4, date=f"2026-07-{i:02d}")
            for i in range(1, 11)]
    features = qr._bar_features(bars)
    agg = {"counts": {"open_up": 2}, "total": 2, "bull": 0, "bear": 0}
    score, reasons = qr._orderbook_score(agg, features=features)
    assert score >= 70  # 长影线 + 高振幅低净涨 + 炸板
    assert any("影线" in r for r in reasons)
    assert any("炸板" in r for r in reasons)


def test_sentiment_score_detects_intraday_reversal_pair():
    agg = {"counts": {"rocket": 1, "dive": 1}, "total": 2, "bull": 1, "bear": 1}
    score, reasons = qr._sentiment_score(agg, features=None)
    assert score >= 35
    assert any("急涨急跌" in r or "反转" in r for r in reasons)


def test_bias_score_flags_retail_bagholding_divergence():
    features = {"up_streak": 4, "vol_spike": True, "big_reversals": 0,
                "shadow_ratio_mean": 0.2, "amp_sum": 10, "net_pct": 8,
                "amplitude_expanding": False}
    score, reasons = qr._bias_score(
        main_net_inflow=-5e7, sm_net_inflow=3e7, features=features,
        quant_seats=[{"side": "sell", "inst_name": "某量化"}])
    assert score >= 85  # 背离40 + 追高放量30 + 量化席位卖方20
    assert any("散户" in r for r in reasons)
    assert any("量化席位" in r for r in reasons)


def test_bias_score_light_path_divergence_without_flow_detail():
    score, reasons = qr._bias_score(
        main_net_inflow=-3e7, sm_net_inflow=None, features=None,
        quant_seats=(), pct_chg=4.2)
    assert score >= 25
    assert any("主力" in r for r in reasons)


# ----------------------------- 综合与预测 -----------------------------

def test_composite_weights_and_quant_seat_bonus():
    scores = {"spoof": 100, "hft": 100, "orderbook": 100, "sentiment": 100, "bias": 100}
    top = qr._composite(scores, has_quant_seat=True)
    assert top["activity"] == 100  # 封顶
    mid = qr._composite({"spoof": 0, "hft": 100, "orderbook": 0, "sentiment": 0, "bias": 0})
    assert mid["activity"] == 30  # hft 权重 0.30
    assert qr._composite({k: 80 for k in scores})["level"] == "高危"
    assert qr._composite({k: 0 for k in scores})["level"] == "常态"


def test_level_thresholds_monotonic():
    levels = [qr._composite({k: v for k in ("spoof", "hft", "orderbook", "sentiment", "bias")})["level"]
              for v in (10, 40, 60, 90)]
    assert levels == ["常态", "轻度", "中度", "高危"]


def test_predict_stock_orders_by_mechanism_severity():
    scores = {"spoof": 20, "hft": 80, "orderbook": 10, "sentiment": 65, "bias": 5}
    tips = qr._predict_stock(scores)
    assert tips[0]["tag"] == "高频博弈"
    assert any(t["tag"] == "情绪狙击" for t in tips)
    calm = qr._predict_stock({k: 10 for k in scores})
    assert len(calm) == 1 and "未见明显" in calm[0]["text"]


def test_predict_market_combines_gauge_and_futures():
    gauge = {"bull": 300, "bear": 100, "total": 400, "active_stocks": 200, "open_up": 5}
    text = qr._predict_market(gauge, futures_signal="偏空")
    assert "拉升" in text or "进攻" in text
    assert "偏空" in text
    balanced = qr._predict_market({"bull": 100, "bear": 100, "total": 200,
                                   "active_stocks": 80, "open_up": 0}, None)
    assert "拉锯" in balanced


# ----------------------------- 板块聚合 / 知识卡 -----------------------------

def test_aggregate_sectors_groups_and_ranks():
    items = [
        {"code": "1", "name": "甲", "industry": "半导体", "activity": 80},
        {"code": "2", "name": "乙", "industry": "半导体", "activity": 60},
        {"code": "3", "name": "丙", "industry": "白酒", "activity": 30},
        {"code": "4", "name": "丁", "industry": "", "activity": 90},  # 无行业跳过
    ]
    sectors = qr._aggregate_sectors(items)
    assert sectors[0]["industry"] == "半导体"
    assert sectors[0]["count"] == 2
    assert sectors[0]["avg_activity"] == 70.0
    assert sectors[0]["top"]["name"] == "甲"
    assert all(s["industry"] for s in sectors)


def test_knowledge_payload_covers_five_mechanisms_and_regulation():
    k = qr.knowledge_payload()
    assert len(k["mechanisms"]) == 5
    assert all({"key", "name", "method", "signals", "defense"} <= set(m) for m in k["mechanisms"])
    timeline_text = "".join(t["event"] for t in k["timeline"])
    assert "15" in timeline_text  # 高频认定 300→15 笔/秒
    assert len(k["defense_rules"]) == 5
    assert k["disclaimer"]


# ----------------------------- kv 快照回退 -----------------------------

def test_day_snapshot_roundtrip_and_fallback(tmp_db):
    qr._save_day_snapshot("20260710", {"gauge": {"total": 12}})
    date, payload = qr._load_recent_snapshot(["20260713", "20260712", "20260711", "20260710"])
    assert date == "20260710"
    assert payload["gauge"]["total"] == 12
    assert qr._load_recent_snapshot(["20260601"]) == (None, None)


# ----------------------------- overview / stock_analysis 组装 -----------------------------

def _fake_snapshot_rows():
    return [
        {"code": "600000", "name": "浦发银行", "price": 10.0, "change_pct": 0.3,
         "amplitude": 8.0, "turnover": 15.0, "volume_ratio": 4.0,
         "industry": "银行", "main_net_inflow": -6e7},
        {"code": "000002", "name": "万科A", "price": 8.0, "change_pct": 5.0,
         "amplitude": 3.0, "turnover": 2.0, "volume_ratio": 1.1,
         "industry": "地产", "main_net_inflow": 2e7},
    ]


def test_overview_assembles_payload_with_injected_fetchers(tmp_db):
    changes = [_change("600000", 8193, "浦发银行"), _change("600000", 8193),
               _change("600000", 8201), _change("600000", 8203), _change("600000", 16)]
    payload = qr.overview(
        force=True,
        fetch_changes=lambda: changes,
        fetch_snapshot=_fake_snapshot_rows,
        futures_signal_fn=lambda: "偏空",
    )
    assert payload["gauge"]["total"] == 5
    assert payload["live"] is True
    codes = [s["code"] for s in payload["stocks"]]
    assert "600000" in codes
    top = payload["stocks"][0]
    assert top["code"] == "600000"  # 异动密集者活跃度更高
    assert {"spoof", "hft", "orderbook", "sentiment", "bias"} <= set(top["scores"])
    assert payload["sectors"] and payload["sectors"][0]["industry"]
    assert payload["knowledge"]["mechanisms"]
    assert "偏空" in payload["market_prediction"]
    assert isinstance(payload["alerts"], list)
    # 快照应已落库(按交易日 key,周末重放不产生幻影日期),休市读取可回退
    date, snap = qr._load_recent_snapshot([qr._current_trade_date_key()])
    assert date == qr._current_trade_date_key()
    assert snap["gauge"]["total"] == 5


def test_overview_falls_back_to_kv_snapshot_when_market_closed(tmp_db):
    qr._save_day_snapshot("20260710", {
        "gauge": {"total": 7, "bull": 5, "bear": 2, "open_up": 0, "active_stocks": 3},
        "stocks": [], "sectors": [], "alerts": [],
    })
    payload = qr.overview(
        force=True,
        fetch_changes=lambda: [],
        fetch_snapshot=lambda: [],
        futures_signal_fn=lambda: None,
        snapshot_dates=["20260713", "20260712", "20260711", "20260710"],
    )
    assert payload["live"] is False
    assert payload["fallback_date"] == "2026-07-10"
    assert payload["gauge"]["total"] == 7
    assert payload["knowledge"]["mechanisms"]  # 知识卡始终返回


def test_stock_analysis_full_path_with_injected_bars(tmp_db):
    bars = [_bar(pct_chg=p, open_=10, close=10 * (1 + p / 100), high=10.9, low=9.5,
                 volume=v, date=f"2026-07-{i + 1:02d}")
            for i, (p, v) in enumerate([(1, 1e6), (2, 1e6), (6, 1e6), (-4, 2e6),
                                        (1, 1e6), (2, 1e6), (3, 1e6), (4, 5e6)])]
    changes = [_change("600000", 8193), _change("600000", 8193), _change("600000", 8193),
               _change("600000", 8201), _change("600000", 8203)]
    payload = qr.stock_analysis(
        "600000",
        kline_fetcher=lambda code, limit: bars,
        fetch_changes=lambda: changes,
    )
    assert payload["ok"] is True
    assert payload["code"] == "600000"
    mech = payload["mechanisms"]
    assert {m["key"] for m in mech} == {"spoof", "hft", "orderbook", "sentiment", "bias"}
    assert all("score" in m and "reasons" in m for m in mech)
    assert payload["activity"] >= 0 and payload["level"]
    assert payload["predictions"]
    assert payload["defense"]
    assert payload["changes"]["total"] == 5


def test_stock_analysis_degrades_without_any_data(tmp_db):
    payload = qr.stock_analysis(
        "300999",
        kline_fetcher=lambda code, limit: [],
        fetch_changes=lambda: [],
    )
    assert payload["ok"] is True
    assert payload["activity"] == 0
    assert payload["level"] == "常态"
    assert payload["notes"]  # 数据不足提示


# ----------------------------- repo: 量化席位按日期 -----------------------------


def test_flows_df_to_snapshot_converts_units_and_shapes():
    import pandas as pd

    df = pd.DataFrame([
        {"ts_code": "600000.SH", "name": "浦发银行", "close": 10.0, "pct_change": 3.0,
         "net_amount": -5000.0, "buy_sm_amount": 2000.0, "amount_unit": "万元", "trade_date":"20260710"},
        {"ts_code": "000001.SZ", "name": "平安银行", "close": 12.0, "pct_change": -1.0,
         "net_amount": 8000.0, "buy_sm_amount": -100.0, "amount_unit": "万元"},
    ])
    rows = qr._flows_df_to_snapshot(df)
    by_code = {r["code"]: r for r in rows}
    assert by_code["600000"]["main_net_inflow"] == -5000.0 * 1e4  # 万元 → 元
    assert by_code["600000"]["change_pct"] == 3.0
    assert by_code["600000"]["price"] == 10.0
    assert by_code["600000"]["volume_ratio"] is None  # 本地兜底无量比,规则自动跳过
    assert by_code["600000"]["flow_date"]==by_code["600000"]["price_date"]=="2026-07-10"
    assert rows[0]["code"] == "000001"  # 按主力净额绝对值降序


def test_get_quant_by_date_filters_quant_rows(tmp_db):
    from data_store import dragon_tiger_repo

    dragon_tiger_repo.upsert_rows([
        {"ts_code": "600000.SH", "trade_date": "2026-07-10", "inst_name": "华鑫证券量化专用",
         "side": "sell", "buy_amount": 0.0, "sell_amount": 5e7, "net_amount": -5e7,
         "is_quant": 1, "quant_confidence": "0.9", "reason": ""},
        {"ts_code": "000001.SZ", "trade_date": "2026-07-10", "inst_name": "普通游资",
         "side": "buy", "buy_amount": 3e7, "sell_amount": 0.0, "net_amount": 3e7,
         "is_quant": 0, "quant_confidence": "", "reason": ""},
        {"ts_code": "000002.SZ", "trade_date": "2026-06-01", "inst_name": "某某算法",
         "side": "buy", "buy_amount": 1e7, "sell_amount": 0.0, "net_amount": 1e7,
         "is_quant": 1, "quant_confidence": "0.6", "reason": ""},
    ])
    df = dragon_tiger_repo.get_quant_by_date("2026-07-01", "2026-07-11")
    assert len(df) == 1
    assert df.iloc[0]["inst_name"] == "华鑫证券量化专用"


# ----------------------------- 按天保存与搜索 -----------------------------

def _day_item(code, name, activity, industry="半导体", **kw):
    base = {"code": code, "name": name, "industry": industry, "activity": activity,
            "level": "高危" if activity >= 70 else "常态",
            "level_rank": 3 if activity >= 70 else 0,
            "scores": {"spoof": 10, "hft": activity, "orderbook": 0, "sentiment": 0, "bias": 0},
            "changes_total": 5, "changes_bull": 3, "changes_bear": 2, "quant_seat": True,
            "price": 10.5, "change_pct": 1.0, "volume_ratio": 2.0, "turnover": 5.0,
            "amplitude": 4.0, "main_net_inflow": -1e7,
            "badges": ["hft"], "reasons": ["异动密集"]}
    base.update(kw)
    return base


def test_repo_day_roundtrip_search_and_history(tmp_db,monkeypatch):
    freeze_trade_day(monkeypatch,"2026-07-14")
    from data_store import quant_radar_repo as repo

    repo.upsert_day("2026-07-10", [_day_item("600000", "浦发银行", 80),
                                   _day_item("000001", "平安银行", 40, industry="银行")])
    repo.upsert_day("2026-07-13", [_day_item("600000", "浦发银行", 55)])
    rows = repo.get_day("2026-07-10")
    assert [r["code"] for r in rows] == ["600000", "000001"]  # activity 降序
    assert rows[0]["badges"] == ["hft"]
    assert rows[0]["scores"]["hft"] == 80
    assert rows[0]["quant_seat"] is True
    assert [r["code"] for r in repo.get_day("2026-07-10", q="平安")] == ["000001"]
    assert [r["code"] for r in repo.get_day("2026-07-10", q="6000")] == ["600000"]
    assert [r["code"] for r in repo.get_day("2026-07-10", q="半导体")] == ["600000"]
    assert [r["code"] for r in repo.get_day("2026-07-10", min_activity=70)] == ["600000"]
    hist = repo.get_stock_history("600000", days=30)
    by_day = {h["trade_date"]: h for h in hist}
    assert by_day["2026-07-13"]["activity"] == 55 and "status" not in by_day["2026-07-13"]
    assert by_day["2026-07-10"]["activity"] == 80
    idx = next(i for i, h in enumerate(hist) if h["trade_date"] == "2026-07-13")
    assert hist[idx + 1]["trade_date"] == "2026-07-10"  # 日历连续,周末不占位
    assert hist[0]["trade_date"] > "2026-07-13"  # 其余日历日补了三态占位行
    assert repo.list_dates() == ["2026-07-13", "2026-07-10"]


def test_overview_persists_daily_rows(tmp_db):
    from data_store import quant_radar_repo as repo

    changes = [_change("600000", 8193, "浦发银行"), _change("600000", 8193),
               _change("600000", 8201), _change("600000", 8203)]
    qr.overview(force=True, fetch_changes=lambda: changes,
                fetch_snapshot=_fake_snapshot_rows,
                futures_signal_fn=lambda: None, volume_fn=lambda: None)
    rows = repo.get_day(qr._current_trade_date_iso())
    assert any(r["code"] == "600000" for r in rows)


def test_overview_historical_date_reads_kv_and_backfills_table(tmp_db):
    from data_store import quant_radar_repo as repo

    qr._save_day_snapshot("20260701", {
        "gauge": {"total": 9, "bull": 6, "bear": 3, "open_up": 1, "active_stocks": 4},
        "stocks": [_day_item("600000", "浦发银行", 80)], "sectors": [], "alerts": [],
    })
    payload = qr.overview(date="2026-07-01")
    assert payload["live"] is False
    assert payload["snapshot_date"] == "2026-07-01"
    assert payload["gauge"]["total"] == 9
    assert payload["stocks"][0]["code"] == "600000"
    assert payload["knowledge"]["mechanisms"]
    assert repo.get_day("2026-07-01")  # 懒回填进按日表


def test_overview_historical_date_rebuilds_from_table(tmp_db):
    from data_store import quant_radar_repo as repo

    repo.upsert_day("2026-06-20", [_day_item("300750", "宁德时代", 75)])
    payload = qr.overview(date="20260620")
    assert payload["live"] is False
    assert payload["stocks"][0]["code"] == "300750"
    assert payload["alerts"] and payload["alerts"][0]["code"] == "300750"
    assert "重建" in payload["note"]
    empty = qr.overview(date="2026-01-01")
    assert empty["stocks"] == [] and "无" in empty["note"]


def test_repo_get_day_limit_zero_returns_all(tmp_db):
    from data_store import quant_radar_repo as repo

    repo.upsert_day("2026-07-10",
                    [_day_item(f"6001{i:02d}", f"股{i}", 30 + i) for i in range(8)])
    assert len(repo.get_day("2026-07-10", limit=0)) == 8   # limit<=0 → 全量
    assert len(repo.get_day("2026-07-10", limit=3)) == 3   # 显式 limit 仍生效


def test_overview_returns_full_stock_list_beyond_legacy_cap(tmp_db):
    """活跃股票榜返回全量评分项(旧实现截断前 50,前端负责分页)。"""
    codes = [f"60{i:04d}" for i in range(60)]
    changes = [_change(c, 8193, f"股{i}") for i, c in enumerate(codes)]
    payload = qr.overview(force=True, fetch_changes=lambda: changes,
                          fetch_snapshot=_fake_snapshot_rows,
                          futures_signal_fn=lambda: None, volume_fn=lambda: None)
    assert len(payload["stocks"]) >= 60


def test_aggregate_sectors_returns_all_industries_by_default():
    items = [{"code": f"60{i:04d}", "name": f"股{i}", "industry": f"行业{i}",
              "activity": 50, "change_pct": 1.0, "smash": 0, "direction": ""}
             for i in range(20)]
    assert len(qr._aggregate_sectors(items)) == 20          # 默认全量(旧实现截 16)
    assert len(qr._aggregate_sectors(items, top_n=5)) == 5  # 显式 top_n 仍生效


def test_historical_overview_expands_from_day_table_beyond_kv_head(tmp_db):
    """kv 快照只存前列;按日表行数更多时,历史回看用按日表补全股票榜/砸盘榜/板块榜。"""
    from data_store import quant_radar_repo as repo

    qr._save_day_snapshot("20260630", {
        "gauge": {"total": 9, "bull": 6, "bear": 3, "open_up": 0, "active_stocks": 5},
        "stocks": [_day_item("600001", "甲", 80), _day_item("600002", "乙", 70)],
        "smashed": [], "sectors": [], "alerts": [],
    })
    repo.upsert_day("2026-06-30", [
        _day_item("600001", "甲", 80), _day_item("600002", "乙", 70),
        _day_item("600003", "丙", 60), _day_item("600004", "丁", 50),
        _day_item("600005", "戊", 45, direction="砸盘", smash=66, change_pct=-5.2),
    ])
    payload = qr.overview(date="2026-06-30")
    assert payload["snapshot_date"] == "2026-06-30"
    assert len(payload["stocks"]) == 5                       # 按日表全量替换 kv 前列
    assert payload["smashed"] and payload["smashed"][0]["code"] == "600005"
    assert payload["sectors"] and payload["sectors"][0]["industry"]


def test_overview_closed_market_fallback_expands_from_day_table(tmp_db):
    """休市回退最近快照时,同样用按日表全量行补全榜单。"""
    from data_store import quant_radar_repo as repo

    qr._save_day_snapshot("20260710", {
        "gauge": {"total": 7, "bull": 5, "bear": 2, "open_up": 0, "active_stocks": 3},
        "stocks": [_day_item("600001", "甲", 80)], "smashed": [], "sectors": [], "alerts": [],
    })
    repo.upsert_day("2026-07-10", [
        _day_item("600001", "甲", 80), _day_item("600002", "乙", 70),
        _day_item("600003", "丙", 60),
    ])
    payload = qr.overview(
        force=True,
        fetch_changes=lambda: [],
        fetch_snapshot=lambda: [],
        futures_signal_fn=lambda: None,
        snapshot_dates=["20260713", "20260712", "20260711", "20260710"],
    )
    assert payload["live"] is False
    assert payload["fallback_date"] == "2026-07-10"
    assert len(payload["stocks"]) == 3


def test_stock_analysis_includes_daily_history(tmp_db,monkeypatch):
    freeze_trade_day(monkeypatch,"2026-07-13")
    from data_store import quant_radar_repo as repo

    repo.upsert_day("2026-07-10", [_day_item("600000", "浦发银行", 80)])
    payload = qr.stock_analysis("600000",
                                kline_fetcher=lambda code, limit: [],
                                fetch_changes=lambda: [])
    assert payload["history"]
    assert "2026-07-10" in [h["trade_date"] for h in payload["history"]]
    row = next(h for h in payload["history"] if h["trade_date"] == "2026-07-10")
    assert row["activity"] == 80


# ----------------------------- 砸盘识别(方向维度) -----------------------------

def test_smash_score_flags_oneway_dumping():
    agg = {"counts": {"dive": 3, "plunge": 2, "big_sell": 4, "seal_down": 1},
           "total": 10, "bull": 0, "bear": 10}
    score, reasons = qr._smash_score(agg, pct_chg=-6.5, volume_ratio=2.0,
                                     main_net_inflow=-8e7)
    assert score >= 70
    assert any("杀跌" in r for r in reasons)
    assert any("主力" in r for r in reasons)
    calm, _ = qr._smash_score({"counts": {"big_buy": 3}, "total": 3, "bull": 3, "bear": 0},
                              pct_chg=2.0)
    assert calm == 0


def test_direction_label_rules():
    assert qr._direction_label(1, 8, -4.0) == "砸盘"
    assert qr._direction_label(8, 1, 4.0) == "拉抬"
    assert qr._direction_label(5, 5, 0.2) == "拉锯"
    assert qr._direction_label(0, 0, None) == ""


def test_light_stock_item_carries_direction_and_smash():
    agg = {"code": "600879", "name": "航天电子", "total": 9, "bull": 1, "bear": 8,
           "counts": {"dive": 3, "plunge": 2, "big_sell": 3, "rocket": 1}, "last_time": "14:30"}
    snap = {"code": "600879", "name": "航天电子", "price": 10.0, "change_pct": -6.0,
            "volume_ratio": 2.2, "turnover": 8.0, "amplitude": 9.0,
            "industry": "航天航空", "main_net_inflow": -5e7}
    item = qr._light_stock_item("600879", agg, snap, [])
    assert item["direction"] == "砸盘"
    assert item["smash"] >= 50
    assert "smash" in item["badges"]


def test_aggregate_sectors_flags_collective_smash():
    def smashed(code):
        return {"code": code, "name": "股" + code, "industry": "航天航空", "activity": 45,
                "change_pct": -5.0, "direction": "砸盘", "smash": 60}

    items = [smashed("1"), smashed("2"), smashed("3"),
             {"code": "9", "name": "茅台", "industry": "白酒", "activity": 80,
              "change_pct": 3.0, "direction": "拉抬", "smash": 0}]
    sectors = qr._aggregate_sectors(items)
    aero = next(s for s in sectors if s["industry"] == "航天航空")
    assert aero["smashed_count"] == 3
    assert aero["collective"] == "集体砸盘"
    assert aero["avg_change_pct"] == -5.0
    assert sectors[0]["industry"] == "航天航空"  # 集体砸盘板块置顶
    wine = next(s for s in sectors if s["industry"] == "白酒")
    assert wine["collective"] == ""


def test_overview_surfaces_smashed_list_and_gauge(tmp_db):
    changes = ([_change("600879", t, "航天电子") for t in (8203, 8203, 8204, 8194, 8194, 8194)]
               + [_change("600519", 8201, "贵州茅台"), _change("600519", 8193)])
    snapshot = [
        {"code": "600879", "name": "航天电子", "price": 10.0, "change_pct": -6.0,
         "amplitude": 9.0, "turnover": 8.0, "volume_ratio": 2.2,
         "industry": "航天航空", "main_net_inflow": -5e7},
        {"code": "600519", "name": "贵州茅台", "price": 1400.0, "change_pct": 1.0,
         "amplitude": 2.0, "turnover": 0.5, "volume_ratio": 1.0,
         "industry": "白酒", "main_net_inflow": 2e7},
    ]
    payload = qr.overview(force=True, fetch_changes=lambda: changes,
                          fetch_snapshot=lambda: snapshot,
                          futures_signal_fn=lambda: None, volume_fn=lambda: None,
                          fetch_quotes=lambda codes: {})
    assert payload["smashed"]
    assert payload["smashed"][0]["code"] == "600879"
    assert payload["gauge"]["smashed_stocks"] >= 1
    by_code = {s["code"]: s for s in payload["stocks"]}
    assert by_code["600879"]["direction"] == "砸盘"
    assert by_code["600519"]["direction"] in ("拉抬", "拉锯")


def test_repo_day_direction_filter(tmp_db):
    from data_store import quant_radar_repo as repo

    repo.upsert_day("2026-07-13", [
        _day_item("600879", "航天电子", 45, industry="航天航空",
                  direction="砸盘", smash=60, change_pct=-6.0,
                  changes_bull=1, changes_bear=8),
        _day_item("600519", "贵州茅台", 50, industry="白酒",
                  direction="拉抬", smash=0, change_pct=2.0),
    ])
    rows = repo.get_day("2026-07-13", direction="砸盘")
    assert [r["code"] for r in rows] == ["600879"]
    assert rows[0]["smash"] == 60
    assert rows[0]["direction"] == "砸盘"


def test_stock_analysis_reports_smash(tmp_db):
    changes = [_change("600879", t, "航天电子")
               for t in (8203, 8203, 8204, 8194, 8194, 8194, 8208)]
    bars = [_bar(pct_chg=p, open_=10, close=10 * (1 + p / 100), high=10.4, low=9.2,
                 volume=2e6, date=f"2026-07-{i + 1:02d}")
            for i, p in enumerate([1, -2, -3, -6])]
    payload = qr.stock_analysis("600879",
                                kline_fetcher=lambda code, limit: bars,
                                fetch_changes=lambda: changes)
    assert payload["direction"] == "砸盘"
    assert payload["smash"] >= 50
    assert payload["predictions"][0]["tag"] == "程序化出货"


# ----------------------------- 实时报价覆盖 / 行业补空 -----------------------------

def test_enrich_snapshot_overrides_stale_and_fills_industry():
    rows = [{"code": "600000", "name": "浦发", "price": 9.0, "change_pct": -1.0,
             "volume_ratio": None, "turnover": None, "amplitude": None,
             "industry": "", "main_net_inflow": -1e7}]
    quotes = {"600000": {"price": 10.5, "change_pct": 3.2, "volume_ratio": 2.4,
                         "turnover": 5.1, "amplitude": 6.0},
              "000001": {"price": 12.0, "change_pct": 1.0, "volume_ratio": 1.1,
                         "turnover": 2.0, "amplitude": 3.0}}
    out = qr._enrich_snapshot(rows, quotes, {"600000": "银行"})
    by_code = {r["code"]: r for r in out}
    assert by_code["600000"]["price"] == 10.5  # 陈旧价被实时覆盖
    assert by_code["600000"]["change_pct"] == 3.2
    assert by_code["600000"]["volume_ratio"] == 2.4
    assert by_code["600000"]["industry"] == "银行"  # 空行业被映射补齐
    assert "000001" in by_code  # 报价独有代码追加为新行


def test_enrich_snapshot_keeps_existing_industry_and_ignores_none():
    rows = [{"code": "600000", "industry": "券商", "price": 9.0, "volume_ratio": 2.0}]
    out = qr._enrich_snapshot(rows, {"600000": {"price": None, "turnover": 4.0}},
                              {"600000": "银行"})
    row = out[0]
    assert row["industry"] == "券商"  # 已有行业不被覆盖
    assert row["price"] == 9.0  # None 不覆盖既有值
    assert row["turnover"] == 4.0


def test_tencent_secid_prefix():
    assert qr._tencent_secid("600000") == "sh600000"
    assert qr._tencent_secid("000001") == "sz000001"
    assert qr._tencent_secid("301520") == "sz301520"
    assert qr._tencent_secid("830799") == "bj830799"
    assert qr._tencent_secid("abc") is None


def test_overview_enriches_stocks_with_quotes(tmp_db):
    changes = [_change("600000", 8193, "浦发银行"), _change("600000", 8193),
               _change("600000", 8201)]
    snapshot = [{"code": "600000", "name": "浦发银行", "price": 8.0, "change_pct": -2.0,
                 "amplitude": None, "turnover": None, "volume_ratio": None,
                 "industry": "", "main_net_inflow": -5e7}]
    payload = qr.overview(
        force=True, fetch_changes=lambda: changes, fetch_snapshot=lambda: snapshot,
        futures_signal_fn=lambda: None, volume_fn=lambda: None,
        fetch_quotes=lambda codes: {"600000": {"price": 10.0, "change_pct": 4.0,
                                               "volume_ratio": 3.1, "turnover": 6.0,
                                               "amplitude": 7.0}})
    top = payload["stocks"][0]
    assert top["price"] == 10.0
    assert top["volume_ratio"] == 3.1
    assert top["turnover"] == 6.0
    assert top["amplitude"] == 7.0


# ----------------------------- 模板挂载点 -----------------------------

def test_desktop_template_has_quant_tab():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "webui" / "templates" / "desktop.html").read_text(
        encoding="utf-8")
    assert 'data-cap-tab="quant"' in html
    assert 'quantRadarSection' in html
    # 个股分析套件里的「量化行为」tab
    assert 'data-suite-tab="quant_behavior"' in html
    assert 'suitePaneQuantBehavior' in html
    # 按天回看与按日搜索控件
    assert 'quantDateInput' in html
    assert 'quantSearchInput' in html
    # 吸筹埋伏榜挂载点与窗口切换
    assert 'quantAccumTable' in html
    assert 'quantAccumWindows' in html
    assert 'data-accum-win="40"' in html


def test_desktop_js_wires_accumulation_panel():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "webui" / "static"
          / "lumo_desktop_app.js").read_text(encoding="utf-8")
    assert "/api/quant-radar/accumulation" in js
    assert "function loadQuantAccum" in js
    assert "function renderQuantAccum" in js
    assert "function quantAccumDetailHtml" in js
    # 挂钩:总览渲染后刷新吸筹榜;个股深评追加吸筹区块
    assert "renderQuantRadar=(" in js.replace(" ", "")
    assert "quantStockDetailHtml=(" in js.replace(" ", "")


def test_desktop_js_wires_sector_rows_to_the_stock_list():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "webui" / "static" / "lumo_quant_sector_clicks.js").read_text(
        encoding="utf-8")
    template = (root / "webui" / "templates" / "desktop.html").read_text(encoding="utf-8")

    assert 'src="/static/lumo_quant_sector_clicks.js' in template
    # 发布资源会被压缩并重命名局部函数；全局入口才是稳定契约。
    assert "window.quantShowSectorStocks" in js
    assert 'closest("[data-quant-board]")' in js
    assert ".dataset.quantBoard" in js
    assert '$("#quantSearchInput")' in js
    assert '$("#quantStocksTable")' in js


# ----------------------------- 市场环境:期指多空 + 大盘量能 -----------------------------

def test_volume_energy_labels_expansion_and_shrink():
    def bars(amounts):
        return [{"date": f"2026-07-{i + 1:02d}", "amount": a} for i, a in enumerate(amounts)]

    up = qr._volume_energy([bars([100, 100, 100, 100, 100, 140]),
                            bars([100, 100, 100, 100, 100, 120])])
    assert up["label"] == "放量"
    assert up["ratio_5d"] == 1.3  # (140+120)/(200*5/5)/... 今日260 vs 前5日均200
    down = qr._volume_energy([bars([120, 110, 100, 90, 80, 70]),
                              bars([120, 110, 100, 90, 80, 70])])
    assert down["label"] == "缩量"
    assert down["down_streak"] >= 3
    assert qr._volume_energy([]) is None
    assert qr._volume_energy([bars([100])]) is None


def test_futures_context_aggregates_varieties_and_flags_pressure():
    per_variety = {
        "IF": [{"date": "2026-07-10", "net": -12000}, {"date": "2026-07-09", "net": -9000},
               {"date": "2026-07-08", "net": -7000}, {"date": "2026-07-07", "net": -5000}],
        "IM": [{"date": "2026-07-10", "net": -3000}, {"date": "2026-07-09", "net": -1000},
               {"date": "2026-07-08", "net": 0}, {"date": "2026-07-07", "net": 1000}],
    }
    ctx = qr._futures_context_from_series(per_variety)
    by_var = {v["variety"]: v for v in ctx["varieties"]}
    assert by_var["IF"]["net"] == -12000
    assert by_var["IF"]["net_chg_3d"] == -7000  # -12000 - (-5000)
    assert ctx["net_chg_3d_total"] == -11000
    assert ctx["pressure"] == "空压"  # ≤ -8000 张(v25 fut_bear 同阈值)
    assert "空" in ctx["assessment"]
    assert qr._futures_context_from_series({}) is None


def test_env_predictions_combine_market_and_stock():
    scores_hot = {"spoof": 20, "hft": 70, "orderbook": 10, "sentiment": 30, "bias": 55}
    fut = {"pressure": "空压", "assessment": "期指前20席位净持仓3日净减"}
    vol = {"label": "缩量", "ratio_5d": 0.7}
    tips = qr._env_predictions(scores_hot, fut, vol)
    tags = [t["tag"] for t in tips]
    assert "对冲压制" in tags
    assert "缩量拥挤" in tags
    assert qr._env_predictions({k: 10 for k in scores_hot}, None, None) == []


def test_stock_analysis_includes_market_context(tmp_db):
    payload = qr.stock_analysis(
        "600000",
        kline_fetcher=lambda code, limit: [],
        fetch_changes=lambda: [],
        futures_ctx_fn=lambda: {"pressure": "空压", "assessment": "期指偏空",
                                "varieties": [], "net_chg_3d_total": -9000},
        volume_fn=lambda: {"date": "2026-07-10", "label": "缩量", "ratio_5d": 0.75,
                           "total_amount": 5.2e11, "down_streak": 2},
    )
    assert payload["ok"] is True
    mc = payload["market_context"]
    assert mc["futures"]["pressure"] == "空压"
    assert mc["volume"]["label"] == "缩量"


# ----------------------------- 吸筹埋伏榜 -----------------------------

def _accum_market_df(codes_spec):
    """codes_spec: {ts_code: (net, close_start, close_end)} → 30日全市场窗口 DataFrame。"""
    import pandas as pd

    rows = []
    dates = [f"2026-06-{i + 1:02d}" for i in range(30)]
    for ts_code, (net, c0, c1) in codes_spec.items():
        for i, d in enumerate(dates):
            close = c0 + (c1 - c0) * i / 29
            rows.append({"trade_date": d, "ts_code": ts_code, "name": "股" + ts_code[:6],
                         "net_amount": net, "net_amount_rate": 0.6 if net > 0 else -0.3,
                         "buy_elg_amount": net * 0.8, "buy_lg_amount": 0.0,
                         "close": round(close, 3), "pct_change": 0.15,
                         "amount_unit": "万元"})
    return pd.DataFrame(rows)


def test_accumulation_payload_ranks_qualified_stocks(tmp_db):
    df = _accum_market_df({
        "600000.SH": (3000.0, 10.0, 10.5),   # 吸筹:持续流入+横盘
        "000001.SZ": (-2000.0, 10.0, 9.8),   # 派发:不入榜
        "300750.SZ": (8000.0, 50.0, 51.0),   # 吸筹且力度更大
    })
    payload = qr.accumulation_payload(window=40, market_rows_fn=lambda end, days: df,
                                      changes_agg_fn=lambda: {}, seats_fn=lambda: {},
                                      fetch_quotes=lambda codes: {})
    assert payload["ok"] is True
    assert payload["window"] == 40
    assert payload["data_date"] == "2026-06-30"
    codes = [s["code"] for s in payload["stocks"]]
    assert "000001" not in codes
    assert set(codes) == {"600000", "300750"}
    scores = [s["score"] for s in payload["stocks"]]
    assert scores == sorted(scores, reverse=True)
    top = payload["stocks"][0]
    assert "daily" not in top                      # 榜单不携带逐日序列(控体积)
    assert top["qualified"] is True
    assert top["accum_days"] == 30
    assert top["status"] in ("吸筹中", "疑似启动")
    assert payload["disclaimer"]


def test_accumulation_payload_applies_intraday_bonus_and_cap(tmp_db):
    df = _accum_market_df({"600000.SH": (3000.0, 10.0, 10.5)})
    base = qr.accumulation_payload(window=40, market_rows_fn=lambda end, days: df,
                                   changes_agg_fn=lambda: {}, seats_fn=lambda: {},
                                   fetch_quotes=lambda codes: {})
    boosted = qr.accumulation_payload(
        window=40, market_rows_fn=lambda end, days: df,
        changes_agg_fn=lambda: {"600000": {"counts": {"big_sell": 3, "sell_queue": 1},
                                           "total": 4, "bull": 0, "bear": 4}},
        seats_fn=lambda: {"600000": [{"side": "buy", "inst_name": "某量化"}]},
        fetch_quotes=lambda codes: {"600000": {"price": 10.8, "change_pct": 0.2}})
    b, s = base["stocks"][0], boosted["stocks"][0]
    assert s["score"] == min(100, b["score"] + 10)   # 盘口+5 席位+5
    assert any("压单吸筹" in r or "大单" in r for r in s["reasons"])
    assert any("量化席位" in r for r in s["reasons"])
    assert s["price"] == 10.8                        # 实时报价覆盖


def test_accum_bonus_rules():
    agg = {"counts": {"big_sell": 3, "sell_queue": 1}}
    bonus, reasons = qr._accum_bonus(agg, [], change_pct=0.2)
    assert bonus == 5 and reasons
    bonus2, _ = qr._accum_bonus(agg, [], change_pct=-3.0)   # 压单且真跌 → 不加分
    assert bonus2 == 0
    bonus3, _ = qr._accum_bonus({"counts": {"big_buy": 4}},
                                [{"side": "buy"}], change_pct=None)
    assert bonus3 == 10
    assert qr._accum_bonus({}, [{"side": "sell"}], None) == (0, [])


def test_accumulation_payload_invalid_window_falls_back(tmp_db):
    df = _accum_market_df({"600000.SH": (3000.0, 10.0, 10.5)})
    payload = qr.accumulation_payload(window=33, market_rows_fn=lambda end, days: df,
                                      changes_agg_fn=lambda: {}, seats_fn=lambda: {},
                                      fetch_quotes=lambda codes: {})
    assert payload["window"] == 40


def test_accumulation_payload_historical_reads_kv_snapshot(tmp_db):
    from data_store import kv_repo

    canned = {"ok": True, "window": 40, "data_date": "2026-07-01", "count": 1,
              "stocks": [{"code": "600000", "score": 88}], "note": "", "disclaimer": "d"}
    kv_repo.set_(qr._KV_NAMESPACE, "accum:20260701:40", canned)
    payload = qr.accumulation_payload(window=40, date="2026-07-01")
    assert payload["stocks"][0]["code"] == "600000"
    assert payload["data_date"] == "2026-07-01"


def test_accumulation_payload_empty_market_notes(tmp_db):
    import pandas as pd

    payload = qr.accumulation_payload(window=40,
                                      market_rows_fn=lambda end, days: pd.DataFrame(),
                                      changes_agg_fn=lambda: {}, seats_fn=lambda: {},
                                      fetch_quotes=lambda codes: {})
    assert payload["ok"] is True
    assert payload["stocks"] == []
    assert payload["note"]


def test_stock_analysis_includes_accumulation_block(tmp_db,monkeypatch):
    freeze_trade_day(monkeypatch,"2026-07-13")
    from data_store import moneyflow_repo

    moneyflow_repo.upsert_df(_accum_market_df({"600000.SH": (3000.0, 10.0, 10.5)}), top_n=0)
    payload = qr.stock_analysis("600000",
                                kline_fetcher=lambda code, limit: [],
                                fetch_changes=lambda: [])
    accum = payload["accumulation"]
    assert accum is not None
    assert accum["qualified"] is True
    assert accum["accum_days"] == 30
    assert accum["daily"] and accum["daily"][0]["date"] == "2026-06-01"


def test_stock_analysis_accumulation_none_without_flow_history(tmp_db):
    payload = qr.stock_analysis("300999",
                                kline_fetcher=lambda code, limit: [],
                                fetch_changes=lambda: [])
    assert payload["accumulation"] is None
    assert any("吸筹" in n for n in payload["notes"])


# ----------------------------- 异动时间线点阵(拉抬过程) -----------------------------

def test_parse_change_info_heuristic():
    # 买卖盘类 [量,价,幅,额]:幅=|v|<0.5 比例值,价=首个 [0.5,10000)
    assert qr._parse_change_info("8834493,10.52,0.052100,92918766") == (10.52, 5.21)
    # 速度类 [幅,价,幅]
    assert qr._parse_change_info("0.031500,22.40,0.031500") == (22.40, 3.15)
    assert qr._parse_change_info("") == (None, None)
    assert qr._parse_change_info(None) == (None, None)


def test_aggregate_changes_collects_timeline_events():
    rows = [
        {"c": "600000", "n": "浦发银行", "t": 8201, "tm": 94512, "i": "0.031500,10.52,0.031500"},
        {"c": "600000", "n": "浦发银行", "t": 8193, "tm": 93005, "i": "8834493,10.20,0.012100,92918766"},
        {"c": "600000", "n": "浦发银行", "t": 8203, "tm": 141530, "i": ""},
        {"c": "600000", "n": "浦发银行", "t": 99999, "tm": 100000, "i": ""},  # 未知类型不进时间线
        {"c": "600000", "n": "浦发银行", "t": 8194, "tm": 0, "i": ""},        # 无时间戳跳过
    ]
    agg = qr._aggregate_changes(rows)
    events = agg["600000"]["events"]
    assert [e[0] for e in events] == [93005, 94512, 141530]  # 按时间升序
    assert events[0][1] == "big_buy" and events[0][2] == 10.20 and events[0][3] == 1.21
    assert events[1][1] == "rocket"
    assert events[2][1] == "dive" and events[2][2] is None and events[2][3] is None
    # 无时间戳的事件不进时间线,但仍计入类型计数
    assert agg["600000"]["counts"]["big_sell"] == 1
    assert agg["600000"]["total"] == 4


def test_aggregate_changes_caps_timeline_events_evenly():
    rows = [{"c": "600000", "n": "浦发银行", "t": 8193, "tm": 93000 + i, "i": ""}
            for i in range(qr._EVENTS_CAP + 60)]
    agg = qr._aggregate_changes(rows)
    events = agg["600000"]["events"]
    assert len(events) == qr._EVENTS_CAP
    assert events[0][0] == 93000                      # 保头
    assert events[-1][0] >= 93000 + qr._EVENTS_CAP    # 均匀采样覆盖尾部
    assert agg["600000"]["counts"]["big_buy"] == qr._EVENTS_CAP + 60  # 计数不受截断影响


def test_stock_analysis_timeline_events_sorted_with_labels(tmp_db):
    changes = [
        {"c": "600000", "n": "浦发银行", "t": 8201, "tm": 100210, "i": "0.045000,11.00,0.045000"},
        {"c": "600000", "n": "浦发银行", "t": 8193, "tm": 93110, "i": "1250000,10.60,0.021000,13250000"},
        {"c": "600000", "n": "浦发银行", "t": 8203, "tm": 143001, "i": ""},
    ]
    payload = qr.stock_analysis("600000",
                                kline_fetcher=lambda code, limit: [],
                                fetch_changes=lambda: changes)
    events = payload["changes"]["events"]
    assert [e["time"] for e in events] == ["09:31", "10:02", "14:30"]
    assert events[0]["label"] == "大笔买入" and events[0]["direction"] == "bull"
    assert events[0]["price"] == 10.60 and events[0]["pct"] == 2.10
    assert events[1]["key"] == "rocket"
    assert events[2]["direction"] == "bear"


def test_stock_analysis_timeline_survives_snapshot_fallback(tmp_db):
    live = [
        {"c": "600000", "n": "浦发银行", "t": 8201, "tm": 95900, "i": "0.030000,10.30,0.030000"},
        {"c": "600000", "n": "浦发银行", "t": 4, "tm": 145600, "i": "10.99,3210000,10.99,0.100000"},
    ]
    qr._save_day_snapshot("20260717", {"changes_agg": qr._aggregate_changes(live)})
    payload = qr.stock_analysis("600000",
                                kline_fetcher=lambda code, limit: [],
                                fetch_changes=lambda: [],
                                snapshot_dates=["20260718", "20260717"])
    assert payload["changes"]["date"] == "2026-07-17"
    events = payload["changes"]["events"]
    assert [e["time"] for e in events] == ["09:59", "14:56"]
    assert events[1]["label"] == "封涨停板" and events[1]["direction"] == "bull"
    assert any("快照" in n for n in payload["notes"])


def test_stock_analysis_payload_carries_kline_bars(tmp_db):
    bars = [_bar(pct_chg=1.0, open_=10 + i * 0.1, close=10.1 + i * 0.1,
                 date=f"2026-06-{i + 1:02d}") for i in range(8)]
    payload = qr.stock_analysis("600000",
                                kline_fetcher=lambda code, limit: bars,
                                fetch_changes=lambda: [])
    out = payload["bars"]
    assert len(out) == 8
    assert out[0]["date"] == "2026-06-01" and out[-1]["date"] == "2026-06-08"
    assert set(out[0]) == {"date", "open", "high", "low", "close", "pct_chg"}
    assert out[-1]["close"] == 10.8


def test_payload_bars_sorts_filters_and_trims():
    rows = [{"date": "2026-06-03", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "pct_chg": 1.0},
            {"date": "2026-06-01", "open": 1, "high": 2, "low": 0.5, "close": 1.2, "pct_chg": 0.5},
            {"date": "2026-06-02", "open": 1, "high": 2, "low": 0.5, "close": None},  # 无收盘剔除
            ]
    out = qr._payload_bars(rows, limit=2)
    assert [b["date"] for b in out] == ["2026-06-01", "2026-06-03"]


# ----------------------------- 收盘后自动落库(收盘态标记) -----------------------------

def test_autosave_once_marks_closed_after_intraday_rows(tmp_db, monkeypatch):
    """当日按日表已有盘中态但无收盘标记 → 收盘后必须重跑并打标记。

    盘中打开过雷达页就会留下盘中态的按日行;若以「当日表是否为空」作判据,
    收盘态将永远缺席,个股「量化行为历史」的当日数据停在打开页面的时刻。
    """
    import datetime as dt
    from data_store import calendar_repo, quant_radar_repo

    day = dt.date(2026, 8, 18)  # 周二
    key = day.strftime("%Y%m%d")
    calendar_repo.upsert([key], is_open=1)
    quant_radar_repo.upsert_day(day.isoformat(), [{
        "code": "000001", "name": "甲", "industry": "银行",
        "activity": 62, "level": "中度", "level_rank": 2,
        "changes_total": 5, "changes_bull": 3, "changes_bear": 2,
    }])
    monkeypatch.setattr(qr, "_trade_date_memo", {"day": "", "key": ""})
    monkeypatch.setattr(qr, "overview", lambda force=False: {"live": True})

    now = dt.datetime(2026, 8, 18, 15, 30)
    assert qr._autosave_once(now=now) is True
    assert qr._day_closed(key) is True
    assert qr._autosave_once(now=now) is False, "已存过收盘态后不应再跑"


def test_autosave_once_skips_when_not_a_trade_day(tmp_db, monkeypatch):
    """周末/节假日不落库(交易日历优先,周末重放会产生幻影日期)。"""
    import datetime as dt
    from data_store import calendar_repo

    calendar_repo.upsert(["20260817"], is_open=1)  # 最近开市日 = 周一
    monkeypatch.setattr(qr, "_trade_date_memo", {"day": "", "key": ""})
    monkeypatch.setattr(qr, "overview", lambda force=False: {"live": True})

    sunday = dt.datetime(2026, 8, 16, 15, 30)  # 周日
    assert qr._autosave_once(now=sunday) is False
    assert qr._day_closed("20260816") is False


def test_autosave_once_no_mark_when_live_fails(tmp_db, monkeypatch):
    """盘后抓不到当日异动(live=False)→ 不打收盘标记,留待下次轮询重试。"""
    import datetime as dt
    from data_store import calendar_repo

    day = dt.date(2026, 8, 18)
    key = day.strftime("%Y%m%d")
    calendar_repo.upsert([key], is_open=1)
    monkeypatch.setattr(qr, "_trade_date_memo", {"day": "", "key": ""})
    monkeypatch.setattr(qr, "overview", lambda force=False: {"live": False})

    now = dt.datetime(2026, 8, 18, 15, 30)
    assert qr._autosave_once(now=now) is False
    assert qr._day_closed(key) is False, "抓不到当日数据不能谎称已保存收盘态"


# ----------------------------- 个股历史三态合成 -----------------------------

def _seed_day_row(date_iso, code, activity=60, changes=5, pct=None):
    from data_store import quant_radar_repo
    quant_radar_repo.upsert_day(date_iso, [{
        "code": code, "name": "甲", "industry": "软件",
        "activity": activity, "level": "中度", "level_rank": 2,
        "changes_total": changes, "changes_bull": 3, "changes_bear": 2,
        "change_pct": pct,
    }])


def _seed_moneyflow_day(date_iso, code, pct):
    import pandas as pd
    from data_store import moneyflow_repo
    moneyflow_repo.upsert_df(pd.DataFrame([{
        "trade_date": date_iso, "ts_code": f"{code}.SZ", "name": "甲",
        "net_amount": 1e7, "pct_change": pct,
    }]), top_n=0)


def test_get_stock_history_three_states(tmp_db,monkeypatch):
    """按日表只落有异动的股票,历史要按交易日历补全并区分三态:
    有异动(正常行)/页面运行过但无异动(no_activity,补真实涨跌)/当天未运行(unrecorded)。"""
    freeze_trade_day(monkeypatch,"2026-08-18")
    from data_store import calendar_repo
    from data_store.quant_radar_repo import get_stock_history

    for d in ("20260818", "20260817", "20260814", "20260813"):
        calendar_repo.upsert([d], is_open=1)
    _seed_day_row("2026-08-18", "300339", activity=34, changes=4, pct=1.2)  # 有异动
    _seed_day_row("2026-08-17", "000001", activity=50, changes=8)          # 别的股活跃 → 300339 无异动
    _seed_day_row("2026-08-14", "000002", activity=40, changes=6)          # 同理
    _seed_moneyflow_day("2026-08-17", "300339", 3.2)                       # 无异动日补真实涨跌
    _seed_moneyflow_day("2026-08-13", "300339", -1.1)                      # 未运行日也补

    hist = get_stock_history("300339", days=10)
    assert [h["trade_date"] for h in hist] == [
        "2026-08-18", "2026-08-17", "2026-08-14", "2026-08-13"]
    assert hist[0]["activity"] == 34 and "status" not in hist[0]
    assert hist[1]["status"] == "no_activity" and hist[1]["change_pct"] == 3.2
    assert hist[2]["status"] == "no_activity" and hist[2]["change_pct"] is None
    assert hist[3]["status"] == "unrecorded" and hist[3]["change_pct"] == -1.1
    assert hist[1]["changes_total"] == 0 and hist[1]["badges"] == []


def test_get_stock_history_weekday_fallback_without_calendar(tmp_db):
    """本地无交易日历 → 工作日近似,全部标 unrecorded,不抛错。"""
    from data_store.quant_radar_repo import get_stock_history

    hist = get_stock_history("000001", days=5)
    assert len(hist) == 5
    assert all(h["status"] == "unrecorded" for h in hist)
