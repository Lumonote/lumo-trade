"""条件选股服务纯函数测试 —— 离线,不联网,全部数据源注入。

覆盖 :mod:`webui.services.stock_screener_service`:
维度地图构建(资金/异动/吸筹/龙虎榜)、行情/资金/异动/吸筹/龙虎榜/机会分/技术
七维条件过滤与 AND 组合、技术候选封顶、缺数据降级 note、排序与截断。
"""

import pytest

from webui.services import stock_screener_service as sc


@pytest.fixture(autouse=True)
def _isolate_feature_cache(monkeypatch):
    """隔离选股缓存:关掉磁盘层并清空进程内特征缓存,避免用例之间互相命中。"""
    monkeypatch.setenv("LUMO_SCREENER_CACHE", "0")
    monkeypatch.setattr(sc, "_DISK_CACHE_READY", True)
    monkeypatch.setattr(sc, "_DISK_CACHE", None)
    sc._FEATURE_MEMORY.clear()
    yield
    sc._FEATURE_MEMORY.clear()


def _market_rows():
    return [
        {"code": "600000", "name": "浦发银行", "price": 10.0, "change_pct": 2.5,
         "turnover_rate": 3.0, "industry": "银行"},
        {"code": "300005", "name": "探路者", "price": 12.0, "change_pct": 6.0,
         "turnover_rate": 12.0, "industry": "纺织服装"},
        {"code": "688981", "name": "中芯国际", "price": 90.0, "change_pct": -1.0,
         "turnover_rate": 1.5, "industry": "半导体"},
        {"code": "000001", "name": "ST平安", "price": 5.0, "change_pct": 9.9,
         "turnover_rate": 8.0, "industry": "银行"},
    ]


def _flow_rows():
    rows = []
    for date, net in [("2026-07-14", 500), ("2026-07-15", 800), ("2026-07-16", 1200)]:
        rows.append({"trade_date": date, "ts_code": "600000.SH", "name": "浦发银行",
                     "net_amount": net, "net_amount_rate": 5.0, "close": 10.0,
                     "pct_change": 2.5})
    rows.append({"trade_date": "2026-07-15", "ts_code": "300005.SZ", "name": "探路者",
                 "net_amount": -300, "net_amount_rate": -2.0, "close": 12.0,
                 "pct_change": 6.0})
    rows.append({"trade_date": "2026-07-16", "ts_code": "300005.SZ", "name": "探路者",
                 "net_amount": 2000, "net_amount_rate": 8.0, "close": 12.0,
                 "pct_change": 6.0})
    return rows


def _radar_rows():
    return [
        {"code": "600000", "trade_date": "2026-07-16", "activity": 55, "level": "活跃",
         "direction": "拉抬", "changes_total": 12, "industry": "银行"},
        {"code": "300005", "trade_date": "2026-07-16", "activity": 80, "level": "高危",
         "direction": "砸盘", "changes_total": 30, "industry": "纺织服装"},
    ]


def _bars(n=30, up=True, base=10.0):
    out = []
    for i in range(n):
        c = base + (i * 0.1 if up else -i * 0.05)
        out.append({"date": f"2026-06-{i + 1:02d}", "open": c - 0.05, "close": c,
                    "high": c + 0.1, "low": c - 0.1, "volume": 1e6 * (2 if i == n - 1 else 1)})
    return out


def _screen(cond, **overrides):
    kwargs = dict(
        market_rows_fn=_market_rows,
        flow_window_fn=lambda days: _flow_rows(),
        radar_rows_fn=_radar_rows,
        accum_fn=lambda w: {"data_date": "2026-07-16", "stocks": [
            {"code": "600000", "score": 72, "qualified": True, "accum_days": 25,
             "total_net_wan": 68000}]},
        dragon_fn=lambda days: [
            {"ts_code": "300005.SZ", "net_amount": 8.6e7, "list_count": 2}],
        quant_seats_fn=lambda days: {"300005": True},
        opportunity_fn=lambda: {"date": "2026-07-16", "by_code": {
            "600000": {"score": 81.5, "tier": "A"},
            "688981": {"score": 66.0, "tier": "B"}}},
        kline_fetcher=lambda code, limit: _bars(up=(code == "600000")),
    )
    kwargs.update(overrides)
    return sc.screen(cond, **kwargs)


# ----------------------------- 地图构建 -----------------------------

def test_build_flow_map_streak_and_units():
    m = sc.build_flow_map(_flow_rows())
    assert m["600000"]["streak"] == 3
    assert m["600000"]["main_net_wan"] == 1200
    assert m["300005"]["streak"] == 1  # 前一日为流出,连续中断
    assert m["600000"]["date"] == "2026-07-16"


def test_build_dragon_map_converts_yuan_to_wan():
    m = sc.build_dragon_map([{"ts_code": "300005.SZ", "net_amount": 8.6e7, "list_count": 2}])
    assert m["300005"]["net_wan"] == 8600.0
    assert m["300005"]["days"] == 2


def test_code6_normalizes_variants():
    assert sc._code6("600000.SH") == "600000"
    assert sc._code6("sh600000") == "600000"
    assert sc._code6("600000") == "600000"
    assert sc._code6("BK0475") == ""
    assert sc._code6("") == ""


# ----------------------------- 各维条件 -----------------------------

def test_no_conditions_returns_all_base_sorted():
    payload = _screen({})
    assert payload["ok"] is True
    assert payload["applied"] == []
    assert payload["base_count"] == 4
    assert payload["total_matched"] == 4
    # 默认按主力净流入降序:探路者2000 > 浦发1200 > 无资金数据沉底
    assert [s["code"] for s in payload["stocks"][:2]] == ["300005", "600000"]


def test_market_conditions_filter_pct_st_board():
    payload = _screen({"market": {"pct_min": 0, "exclude_st": True, "boards": ["main", "chinext"]}})
    codes = {s["code"] for s in payload["stocks"]}
    assert codes == {"600000", "300005"}  # 688981 负涨幅+科创板, ST平安被排除
    payload2 = _screen({"market": {"pct_max": 3}})
    assert {s["code"] for s in payload2["stocks"]} == {"600000", "688981"}


def test_flow_conditions_main_net_and_streak():
    payload = _screen({"flow": {"main_net_min": 1000}})
    assert {s["code"] for s in payload["stocks"]} == {"600000", "300005"}
    payload2 = _screen({"flow": {"streak_days": 2}})
    assert {s["code"] for s in payload2["stocks"]} == {"600000"}
    assert "连续净流入3日" in payload2["stocks"][0]["hits"][1]


def test_radar_condition_activity_and_direction():
    payload = _screen({"radar": {"activity_min": 50, "direction": "排除砸盘"}})
    assert {s["code"] for s in payload["stocks"]} == {"600000"}
    payload2 = _screen({"radar": {"direction": "砸盘"}})
    assert {s["code"] for s in payload2["stocks"]} == {"300005"}


def test_accum_condition_score_min():
    payload = _screen({"accum": {"score_min": 60}})
    assert {s["code"] for s in payload["stocks"]} == {"600000"}
    s = payload["stocks"][0]
    assert s["accum_score"] == 72 and s["accum_days"] == 25
    assert payload["dates"]["accum"] == "2026-07-16"


def test_dragon_condition_net_and_quant_seat():
    payload = _screen({"dragon": {"net_min_wan": 5000, "quant_seat": True}})
    assert {s["code"] for s in payload["stocks"]} == {"300005"}
    assert payload["stocks"][0]["quant_seat"] is True


def test_opportunity_condition_score_min():
    payload = _screen({"opportunity": {"score_min": 80}})
    assert {s["code"] for s in payload["stocks"]} == {"600000"}
    assert payload["stocks"][0]["opp_tier"] == "A"


def test_tech_condition_uses_kline_features():
    payload = _screen({"tech": {"above_ma20": True, "ma_bull": True}})
    assert {s["code"] for s in payload["stocks"]} == {"600000"}  # 300005 注入下跌K线
    assert "均线多头" in payload["stocks"][0]["hits"]


def test_and_combination_across_dimensions():
    payload = _screen({"flow": {"main_net_min": 500}, "radar": {"direction": "排除砸盘"},
                       "opportunity": {"score_min": 70}})
    assert payload["applied"] == ["flow", "radar", "opportunity"]
    assert {s["code"] for s in payload["stocks"]} == {"600000"}


# ----------------------------- 降级与封顶 -----------------------------

def test_missing_dimension_data_notes_and_excludes():
    payload = _screen({"radar": {"activity_min": 10}}, radar_rows_fn=lambda: [])
    assert payload["stocks"] == []
    assert any("量化雷达" in n for n in payload["notes"])


def test_market_rows_empty_falls_back_to_flow_base():
    payload = _screen({}, market_rows_fn=lambda: [])
    assert payload["ok"] is True
    assert payload["base_count"] == 2  # flow 覆盖的两只
    assert any("退化" in n for n in payload["notes"])


def test_tech_cap_limits_candidates_with_note(monkeypatch):
    monkeypatch.setattr(sc, "TECH_CAP", 1)
    calls = []

    def fetcher(code, limit):
        calls.append(code)
        return _bars(up=True)

    payload = _screen({"tech": {"above_ma20": True}}, kline_fetcher=fetcher)
    assert len(calls) == 1
    assert calls[0] == "300005"  # 主力净流入最高者优先评估
    assert any("前 1 只" in n for n in payload["notes"])
    assert payload["partial"] is True
    assert payload["timings"]["kline_candidates"] == 4
    assert payload["timings"]["kline_evaluated"] == 1


def test_sort_key_and_limit():
    payload = _screen({"sort": "change_pct", "limit": 2})
    assert payload["returned"] == 2
    assert payload["total_matched"] == 4
    assert payload["stocks"][0]["code"] == "000001"  # 9.9%


def test_tech_features_requires_enough_bars():
    assert sc.tech_features(_bars(n=10)) is None
    feat = sc.tech_features(_bars(n=30, up=True))
    assert feat["above_ma20"] is True and feat["ma_bull"] is True
    assert feat["new_high_20"] is True and feat["vol_ratio"] == 2.0


# ----------------------------- 机会挖掘同源:指标扩展 + 量化模型信号 -----------------------------

def _v_bars(n=40, base=20.0, reb=4):
    """先跌后涨的V形K线(反弹4根):尾部 MACD/KDJ 均在近3根内金叉,RSI~31。"""
    out = []
    for i in range(n):
        c = base - i * 0.3 if i < n - reb else base - (n - reb) * 0.3 + (i - (n - reb)) * 0.5
        out.append({"date": f"2026-05-{i + 1:02d}", "open": c - 0.05, "close": c,
                    "high": c + 0.15, "low": c - 0.15, "volume": 1e6})
    return out


def test_tech_features_includes_rsi_and_golden_crosses():
    feat = sc.tech_features(_v_bars())
    assert feat is not None
    assert feat["rsi"] is not None and 0 <= feat["rsi"] <= 100
    assert feat["macd_golden"] is True   # V形反转尾部金叉
    assert feat["kdj_golden"] is True
    flat = sc.tech_features(_bars(n=30, up=False))
    assert flat["macd_golden"] is False  # 单边下跌无金叉


def test_tech_condition_rsi_and_golden_cross_filters():
    payload = _screen({"tech": {"macd_golden": True}},
                      kline_fetcher=lambda code, limit: _v_bars() if code == "600000" else _bars(up=False))
    assert {s["code"] for s in payload["stocks"]} == {"600000"}
    assert "MACD金叉" in payload["stocks"][0]["hits"]
    sc._FEATURE_MEMORY.clear()  # 上一轮把其余三只下跌K线的特征写进了进程内缓存
    payload2 = _screen({"tech": {"rsi_max": 30}},
                       kline_fetcher=lambda code, limit: _v_bars())
    assert payload2["stocks"] == []  # V形尾部RSI偏高,30以下无命中


def test_quant_dimension_filters_by_model_votes(monkeypatch):
    votes = {"600000": {"buy": 9, "sell": 0}, "300005": {"buy": 3, "sell": 4},
             "688981": {"buy": 6, "sell": 1}, "000001": None}
    fetched = {}

    def fake_signals(bars):
        return votes.get(bars[0]["code"]) if bars else None

    monkeypatch.setattr(sc, "_quant_signals", fake_signals)
    payload = _screen({"quant": {"buy_min": 5, "sell_max": 0}},
                      kline_fetcher=lambda code, limit: [{"code": code, "close": 10, "date": "d"}])
    assert payload["applied"] == ["quant"]
    assert {s["code"] for s in payload["stocks"]} == {"600000"}
    s = payload["stocks"][0]
    assert s["quant_buy"] == 9 and s["quant_sell"] == 0
    assert "量化买入9票" in s["hits"] and "无卖出信号" in s["hits"]


def test_quant_signals_real_pipeline_smoke():
    qs = sc._quant_signals(_bars(n=60, up=True))
    assert qs is not None
    assert set(qs) == {"buy", "sell"}
    assert qs["buy"] >= 0 and qs["sell"] >= 0


def test_limit_absent_returns_all_matches():
    payload = _screen({})
    assert payload["total_matched"] == 4
    assert payload["returned"] == 4  # 不再默认截断
    payload2 = _screen({"limit": 2})
    assert payload2["returned"] == 2 and payload2["total_matched"] == 4


# ----------------------------- 提速:特征缓存 / 时间预算 / 本地库 -----------------------------

def test_feature_cache_skips_second_fetch():
    calls = []

    def fetcher(code, limit):
        calls.append(code)
        return _bars(up=True)

    first = _screen({"preset": {"labels": ["ma_trend"]}}, kline_fetcher=fetcher)
    assert first["ok"] and len(calls) == 4
    second = _screen({"preset": {"labels": ["ma_trend"]}}, kline_fetcher=fetcher)
    assert len(calls) == 4, "第二次选股应全部命中特征缓存,不再取日K"
    assert second["timings"]["kline_cache_hits"] == 4
    assert second["timings"]["kline_ms"] <= first["timings"]["kline_ms"]


def test_refresh_features_bypasses_cache():
    calls = []

    def fetcher(code, limit):
        calls.append(code)
        return _bars(up=True)

    _screen({"preset": {"labels": ["ma_trend"]}}, kline_fetcher=fetcher)
    _screen({"preset": {"labels": ["ma_trend"]}}, kline_fetcher=fetcher)
    assert len(calls) == 4
    _screen({"preset": {"labels": ["ma_trend"]}, "refresh_features": True}, kline_fetcher=fetcher)
    assert len(calls) == 8, "refresh_features 应强制重新取日K"


def test_budget_returns_partial_with_note(monkeypatch):
    import time as _time

    monkeypatch.setattr(sc, "KLINE_WORKERS", 1)

    def slow_fetcher(code, limit):
        _time.sleep(0.4)
        return _bars(up=True)

    payload = _screen({"tech": {"above_ma20": True}, "budget_seconds": 1},
                      kline_fetcher=slow_fetcher)
    assert payload["ok"] is True
    assert payload["partial"] is True
    assert payload["timings"]["kline_evaluated"] < 4  # 预算内只评估了一部分
    assert any("预算" in n for n in payload["notes"])


def test_display_fields_present_without_kline_dimension():
    payload = _screen({"flow": {"main_net_min": 500}})
    row = payload["stocks"][0]
    assert row["ma20_dev"] is not None  # 回填段补上了形态列
    assert row["ret5"] is not None


def test_screener_cache_repo_roundtrip_and_ttl(monkeypatch):
    """缓存的写入/读取/TTL 过期/清理四段闭环(用项目内临时库,不碰真实数据)。"""
    import os
    import shutil
    from pathlib import Path

    from data_store import connection
    from data_store import screener_cache_repo as repo

    base = Path(__file__).resolve().parent / ".tmp_screener_cache"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(base / f"cache_{os.getpid()}.sqlite"))
    connection.reset_for_testing()
    try:
        bars = [{"date": "2026-10-23", "close": 10.0}]
        feat = {"ma20_dev": 1.23, "ret5": 2.5}
        assert repo.put_kline("600000", bars) == 1
        assert repo.put_feature("600000", feat) == 1
        assert repo.get_kline("600000", ttl=3600) == bars
        assert repo.get_feature("600000", ttl=3600) == feat

        # 把更新时间推到很久以前 → 读请求按 TTL 判过期
        connection.get_conn().execute(
            "UPDATE kv_cache SET updated_at='2020-01-01T00:00:00' WHERE namespace IN (?,?)",
            (repo.NS_KLINE, repo.NS_FEAT),
        )
        assert repo.get_kline("600000") is None
        assert repo.get_feature("600000") is None
        assert repo.purge(stale_days=1) >= 2
    finally:
        connection.reset_for_testing()
        shutil.rmtree(base, ignore_errors=True)


# ----------------------------- 默认条件与设计口径回归 -----------------------------

def _price_bars(prices):
    import datetime

    start = datetime.date(2026, 1, 1)
    return [{"date": (start + datetime.timedelta(days=i)).isoformat(),
             "open": price - 0.05, "high": price + 0.1, "low": price - 0.1,
             "close": price, "volume": 1e6, "amount": 0}
            for i, price in enumerate(prices)]


def test_default_sustained_volume_can_match_without_sina_amount():
    bars = _price_bars([20.0] * 55 + [20.25, 20.5, 20.75, 21.0, 21.25])
    for bar in bars[-5:]:
        bar["volume"] = 1.8e6
    payload = _screen({"preset": {"labels": ["vol_burst"]}},
                      kline_fetcher=lambda code, limit: bars)
    assert payload["total_matched"] == 4
    row = payload["stocks"][0]
    assert row["amount_ma5_yi"] is None
    assert 1.5 <= row["vol_energy"] < 2
    assert 5 <= row["ret5"] < 15
    assert all("均额" not in hit for hit in row["hits"])
    restricted = _screen({"preset": {"labels": ["vol_burst"], "amount_min": 0.5}},
                         kline_fetcher=lambda code, limit: bars)
    assert restricted["total_matched"] == 0
    assert any("缺少5日成交额" in note for note in restricted["notes"])


def test_sustained_volume_requires_sixty_bars():
    feat = sc.tech_features(_price_bars([10.0] * 30))
    assert feat["vol_energy"] is None
    assert not sc.preset_pass(feat, {"labels": ["vol_burst"]})[0]


def test_breakout_volume_parameter_is_independent_for_new_clients():
    feat = {"above_ma20": True, "prev_above_ma20": False,
            "vol_ratio": 1.4, "vol_energy": 1.7, "ret5": 6, "amount_ma5_yi": None}
    cond = {"labels": ["vol_burst", "break_ma20"], "mode": "all", "vol_mult": 1.6}
    assert sc.preset_pass(feat, cond)[0]
    assert not sc.preset_pass(feat, {**cond, "break_mult": 2})[0]
    assert not sc.preset_pass(feat, {"labels": ["break_ma20"], "vol_mult": 2})[0]


def test_preset_union_reports_only_the_labels_that_matched():
    feat = {"ma20_up": True, "vol_ratio": 1.0}
    labels = ["ma_trend", "vol_spike"]
    ok, hits = sc.preset_pass(feat, {"labels": labels, "mode": "any"})
    assert ok and hits == ["MA20向上"]
    assert sc.preset_pass(feat, {"labels": labels, "mode": "all"}) == (False, [])
    assert sc.preset_pass(feat, {"labels": labels}) == (False, [])  # 旧 API 保留交集


@pytest.mark.parametrize("n,prices,expected", [
    (5, [20.0] * 16 + [5.0, 6.0, 7.0, 8.0, 10.0], True),
    (30, [9.0] * 50 + [15.0] + [8.0] * 28 + [10.0], False),
    (90, [9.0] * 10 + [15.0] + [8.0] * 78 + [10.0], False),
    (120, [9.0] * 89 + [10.0], False),
])
@pytest.mark.parametrize("label,param", [("new_high_n", "high_n"), ("new_low_n", "low_n")])
def test_new_extreme_uses_the_requested_window(n, prices, expected, label, param):
    if label == "new_low_n":
        prices = [30 - price for price in prices]
    feat = sc.tech_features(_price_bars(prices))
    assert sc.preset_pass(feat, {"labels": [label], param: n})[0] is expected


def test_first_above_ma20_checks_the_previous_five_days():
    prices = [10.0] * 19 + [9.0] * 5 + [11.0]
    feat = sc.tech_features(_price_bars(prices))
    assert feat["first_above_ma20"] is True
    assert sc.preset_pass(feat, {"labels": ["first_above_ma20"]})[0]
    prices[-2] = 10.0  # 昨日已经站上,不能称首次
    assert sc.tech_features(_price_bars(prices))["first_above_ma20"] is False


def test_zero_change_and_turnover_are_available_values():
    payload = _screen({"market": {"pct_min": 0, "pct_max": 0, "turnover_min": 0}},
                      market_rows_fn=lambda: [{"code": "600000", "price": 10,
                                              "change_pct": 0, "turnover_rate": 0}],
                      kline_fetcher=None)
    assert payload["total_matched"] == 1
    assert payload["stocks"][0]["change_pct"] == 0
    assert payload["stocks"][0]["turnover"] == 0


@pytest.mark.parametrize("conditions,reason", [
    ({"market": {"pct_min": 5, "pct_max": 1}}, "涨跌幅下限"),
    ({"market": {"price_min": 20, "price_max": 10}}, "现价下限"),
    ({"tech": {"rsi_min": 70, "rsi_max": 30}}, "RSI下限"),
    ({"preset": {"labels": ["new_high_n"], "high_n": 30.5}}, "整数"),
    ({"preset": {"labels": ["new_high_n"], "high_n": 121}}, "整数"),
    ({"preset": {"labels": ["vol_spike"], "spike_mult": float("nan")}}, "有限数值"),
    ({"preset": {"labels": ["vol_spike"], "spike_mult": -1}}, "不能为负数"),
    ({"preset": {"labels": ["vol_shrink"], "shrink_ratio": 1.2}}, "0 至 1"),
    ({"preset": {"labels": ["ma_trend"], "mode": "invalid"}}, "组合方式"),
])
def test_invalid_conditions_are_rejected_before_loading_data(conditions, reason):
    def unexpected_load():
        pytest.fail("条件无效时不应读取行情")

    payload = _screen(conditions, market_rows_fn=unexpected_load)
    assert payload["ok"] is False
    assert reason in payload["error"]


def test_missing_kline_service_does_not_silently_skip_requested_filters():
    payload = _screen({"tech": {"above_ma20": True}}, kline_fetcher=None)
    assert payload["total_matched"] == 0
    assert payload["timings"]["kline_evaluated"] == 0
    assert payload["funnel"] == [{"dimension": "kline", "before": 4, "after": 0, "missing": 4}]


def test_funnel_distinguishes_missing_data_from_threshold_rejection():
    missing = _screen({"radar": {"activity_min": 10}}, radar_rows_fn=lambda: [])
    assert missing["funnel"] == [{"dimension": "radar", "before": 4, "after": 0, "missing": 4}]
    strict = _screen({"radar": {"activity_min": 99}})
    assert strict["funnel"] == [{"dimension": "radar", "before": 4, "after": 0, "missing": 2}]


def test_previous_feature_schema_is_not_reused():
    import time

    sc._FEATURE_MEMORY["600000"] = (time.time(), {"_version": 1, "first_above_ma20": False})
    assert sc.cached_features("600000") is None
    fresh = sc.tech_features(_price_bars([10.0] * 19 + [9.0] * 5 + [11.0]))
    sc._remember_feature("600000", fresh)
    assert sc.cached_features("600000")["first_above_ma20"] is True

