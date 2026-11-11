"""牛股挖掘(底部埋伏)因子与策略的纯函数测试 —— 合成面板,离线,不读任何真实库。

覆盖 :mod:`analysis.bull_stock_factors`:成交额反推、面板对齐、盘中快照日与单股缺行的修复、
涨停阈值(当日 ST 状态)、位置/波动/量能、逆势资金因子、三档策略的命中与互斥、准入过滤、无未来函数。
"""
import numpy as np
import pytest

from analysis import bull_stock_factors as bf


def _row(code, pct, amount_wan=20000.0, net_wan=600.0, name=None, close=None):
    """一行 moneyflow:成交额通过 净额÷净占比 表达(与真实表一致,没有成交额列)。"""
    if amount_wan is None:   # 停牌日:真实表里是整行 0(涨跌幅是 0.0,不是空)
        return {"ts_code": code, "name": name or code[:6], "pct_change": 0.0,
                "net_amount": 0.0, "net_amount_rate": 0.0, "close": 0.0}
    return {"ts_code": code, "name": name or code[:6], "pct_change": pct, "close": close,
            "net_amount": net_wan, "net_amount_rate": net_wan / amount_wan * 100.0}


def _date(t):
    return f"2026-{1 + t // 28:02d}-{1 + t % 28:02d}"


def _panel(series, days=61, amount=None, net=None, names=None):
    """{code: [日涨跌幅…]}(不足 days 的在前面补 0)→ Panel。amount/net 可按 code 给序列或标量。"""
    slices = []
    for t in range(days):
        rows = []
        for code, pcts in series.items():
            pcts = [0.0] * (days - len(pcts)) + list(pcts)
            amt = (amount or {}).get(code, 20000.0)
            flow = (net or {}).get(code, 600.0)
            rows.append(_row(code, pcts[t],
                             amt[t] if isinstance(amt, (list, tuple)) else amt,
                             flow[t] if isinstance(flow, (list, tuple)) else flow,
                             (names or {}).get(code)))
        slices.append((_date(t), bf.build_day_slice(rows)))
    return bf.assemble_panel(slices)


def _features(series, **kwargs):
    panel = _panel(series, **kwargs)
    return panel, bf.compute_features(panel)


def _at(panel, features, code, key):
    return features[key][panel.codes.index(code)]


# ----------------------------- 切片 / 面板 -----------------------------

def test_amount_is_recovered_from_the_bucket_with_the_largest_rate():
    # 主力净额 50万 占比 0.1%(舍入误差大);超大单 -1200万 占比 -6% → 成交额 2亿
    row = {"ts_code": "600000.SH", "pct_change": 1.0, "close": 9.5, "net_amount": 50.0, "net_amount_rate": 0.1,
           "buy_elg_amount": -1200.0, "buy_elg_amount_rate": -6.0}
    sl = bf.build_day_slice([row])
    assert sl["codes"] == ["600000"] and sl["amt"] == [20000.0] and sl["net"] == [50.0]
    assert sl["close"] == [9.5] and sl["st"] == [False]


def test_untraded_row_has_no_amount_and_no_close():
    sl = bf.build_day_slice([{"ts_code": "000003.SZ", "pct_change": 0.0, "net_amount": 0.0,
                              "net_amount_rate": 0.0, "close": 0.0}, {"ts_code": "bad", "pct_change": 1}])
    assert sl["codes"] == ["000003"] and sl["amt"] == [None] and sl["close"] == [None]


def test_panel_aligns_codes_across_days_and_tracks_st_by_day():
    day1 = bf.build_day_slice([_row("600000.SH", 1.0, name="旧名", close=10.0)])
    day2 = bf.build_day_slice([_row("300001.SZ", 2.0), _row("600000.SH", -1.0, name="*ST新名", close=9.9)])
    panel = bf.assemble_panel([("2026-01-02", day2), ("2026-01-01", day1)])
    assert panel.dates == ["2026-01-01", "2026-01-02"]
    old = panel.codes.index("600000")
    assert panel.names[old] == "*ST新名"
    assert list(panel.st[:, old]) == [False, True]          # ST 状态按当日名称,不用最新名称回看
    assert list(panel.close[:, old]) == [10.0, 9.9]
    late = panel.codes.index("300001")
    assert np.isnan(panel.pct[0, late]) and panel.pct[1, late] == 2.0 and np.isnan(panel.close[1, late])
    assert bf.assemble_panel([]) is None


def test_history_shorter_than_lookback_yields_no_features():
    assert bf.compute_features(_panel({"600000.SH": [1.0]}, days=bf.LOOKBACK - 1)) is None
    out = bf.evaluate(_panel({"600000.SH": [1.0]}, days=30))
    assert out["stocks"] == {} and "至少" in out["reason"]
    assert bf.evaluate(None)["reason"]


# ----------------------------- 数据缺陷修复 -----------------------------

def _priced_slices(pct, stale_day=None, missing=(), drop=None):
    """(T,N) 真实涨跌幅 → 带收盘价的逐日切片。stale_day 那天写成盘中快照:只走了一半的涨跌幅、
    收盘价是盘中价、missing 里的股票没有行。drop = {(t, i)} 额外缺行。"""
    t_count, n_count = pct.shape
    close = 10.0 * np.cumprod(1.0 + pct / 100.0, axis=0)
    slices = []
    for t in range(t_count):
        rows = []
        for i in range(n_count):
            if (drop and (t, i) in drop) or (t == stale_day and i in missing):
                continue
            change, price, amount = pct[t, i], close[t, i], 20000.0
            if t == stale_day:
                change = pct[t, i] / 2.0
                price = (close[t - 1, i] if t else 10.0) * (1.0 + change / 100.0)
                amount = 9000.0
            rows.append(_row(f"60{i:04d}.SH", round(float(change), 2), amount, 400.0, close=float(price)))
        slices.append((_date(t), bf.build_day_slice(rows)))
    return slices


def test_intraday_snapshot_day_is_detected_and_repaired_from_next_day_preclose():
    rng = np.random.default_rng(11)
    pct = np.round(rng.normal(0, 2, (8, 80)), 2)
    pct[4] = np.round(rng.normal(-3, 1.5, 80), 2)            # 真实的当日大跌
    panel = bf.assemble_panel(_priced_slices(pct, stale_day=4, missing={0, 1, 2}))
    assert abs(float(np.nanmean(panel.pct[4])) - float(pct[4, 3:].mean()) / 2) < 0.01   # 修复前只有一半
    info = bf.repair_panel(panel)
    assert info["stale_days"] == [_date(4)] and info["gap_fills"] == 3
    np.testing.assert_allclose(panel.pct[4], pct[4], atol=0.03)       # 含当日缺行的 3 只(缺口补齐)
    assert np.isnan(panel.amt[4]).all() and np.isnan(panel.net[4]).all()   # 半天的成交额/资金作废
    assert panel.traded[4].all() and panel.traded[3].all()
    np.testing.assert_allclose(panel.pct[[3, 5]], pct[[3, 5]], atol=0.006)   # 邻日不动
    f = bf.compute_features(bf.assemble_panel(_priced_slices(np.vstack([np.zeros((60, 80)), pct]))))
    assert f is not None            # 修复只是可选步骤:没调 repair_panel 的面板照常可算


def test_ex_dividend_stocks_do_not_make_a_day_look_stale():
    rng = np.random.default_rng(5)
    pct = np.round(rng.normal(0, 2, (6, 80)), 2)
    slices = _priced_slices(pct)
    for i in range(6):          # 第 3 日有 6 只(7.5%)除权:收盘价被除权拉低,涨跌幅是复权口径
        slices[3][1]["close"][i] *= 0.9
        for t in (4, 5):
            slices[t][1]["close"][i] *= 0.9
    panel = bf.assemble_panel(slices)
    before = panel.pct.copy()
    assert bf.repair_panel(panel) == {"stale_days": [], "gap_fills": 0}
    np.testing.assert_array_equal(panel.pct, before)


def test_single_stock_gaps_are_filled_but_corporate_actions_and_suspensions_are_not():
    rng = np.random.default_rng(9)
    pct = np.round(rng.normal(0, 2, (9, 60)), 2)
    pct[3, 0], pct[3, 1] = 4.0, -3.0          # 0、1 号股票第 3 日缺行,真实涨跌 +4 / -3
    pct[[4, 5], 2] = 0.0                      # 2 号停牌两天:前后收盘相同
    pct[6, 3] = 0.5
    slices = _priced_slices(pct, drop={(3, 0), (3, 1), (4, 2), (5, 2), (6, 3)})
    i3 = slices[7][1]["codes"].index("600003")
    slices[7][1]["close"][i3] *= 0.5          # 3 号缺行期间 10 送 10:次日隐含前收盘是除权价
    slices[8][1]["close"][slices[8][1]["codes"].index("600003")] *= 0.5
    panel = bf.assemble_panel(slices)
    info = bf.repair_panel(panel)
    col = panel.codes.index
    assert info == {"stale_days": [], "gap_fills": 2}
    assert panel.pct[3, col("600000")] == pytest.approx(4.0, abs=0.02)
    assert panel.pct[3, col("600001")] == pytest.approx(-3.0, abs=0.02)
    assert panel.traded[3, col("600000")] and np.isnan(panel.amt[3, col("600000")])
    assert not panel.traded[[4, 5], col("600002")].any() and np.isnan(panel.pct[4, col("600002")])
    assert not panel.traded[6, col("600003")] and np.isnan(panel.pct[6, col("600003")])   # -50% 超过涨跌停上限,不补


def test_latest_day_coverage_flags_an_incomplete_last_day():
    full = {f"60{i:04d}.SH": [1.0] * 5 for i in range(100)}
    panel = _panel(full, days=61)
    assert bf.latest_day_coverage(panel) == 1.0 and bf.evaluate(panel)["incomplete"] is False
    panel.pct[-1, :10] = np.nan                    # 最新一日少了 10% 的行
    out = bf.evaluate(panel)
    assert out["coverage"] == pytest.approx(0.9) and out["incomplete"] is True
    assert bf.latest_day_coverage(_panel({"600000.SH": [1.0]}, days=1)) is None


def test_repair_is_skipped_without_close_prices():
    panel = _panel({"600000.SH": [1.0, 2.0]}, days=5)        # 合成行没有收盘价:什么都判不了
    before = panel.pct.copy()
    assert bf.repair_panel(panel) == {"stale_days": [], "gap_fills": 0}
    np.testing.assert_array_equal(panel.pct, before)
    np.testing.assert_array_equal(panel.traded, np.isfinite(panel.pct) & np.isfinite(panel.amt))
    legacy = _panel({"600000.SH": [1.0, 2.0]}, days=5)
    legacy.close = None                                       # 旧面板根本没有收盘价矩阵
    assert bf.repair_panel(legacy) == {"stale_days": [], "gap_fills": 0} and legacy.traded is None
    assert bf.repair_panel(None) == {"stale_days": [], "gap_fills": 0}


# ----------------------------- 因子 -----------------------------

def test_limit_threshold_depends_on_board_and_st():
    thr = bf.limit_thresholds(["600000", "300001", "688001", "830001", "000002"],
                              ["浦发", "创业", "科创", "北交", "*ST某某"])
    assert list(thr) == [9.8, 19.8, 19.8, 29.8, 4.8]


def test_limit_ups_use_the_st_status_of_that_day():
    series = {"600001.SH": [5.0, 0.0, 10.0], "300001.SZ": [10.0, 10.0, 10.0], "600002.SH": [10.0, 5.0, 0.0]}
    slices = []
    for t in range(61):
        rows = []
        for code, pcts in series.items():
            pcts = [0.0] * (61 - len(pcts)) + pcts
            name = "*ST样本" if code == "600001.SH" and t == 58 else "样本"    # 只有第 58 日戴帽
            rows.append(_row(code, pcts[t], name=name))
        slices.append((_date(t), bf.build_day_slice(rows)))
    panel = bf.assemble_panel(slices)
    f = bf.compute_features(panel)
    assert _at(panel, f, "600001", "lu20") == 2      # +5% 在 ST 当日算涨停;+10% 在摘帽后也算
    assert _at(panel, f, "300001", "lu20") == 0      # 创业板 10% 不是涨停
    assert _at(panel, f, "600002", "lu20") == 1      # 主板 +5% 不是涨停
    assert not _at(panel, f, "600001", "is_st")      # 准入看最新一日的状态


def test_position_and_drawdown_follow_the_price_path():
    panel, f = _features({
        "600001.SH": [-1.0] * 30,                 # 单边下跌:贴在 60 日最低
        "600002.SH": [1.0] * 30,                  # 单边上涨:贴在 60 日最高
        "600003.SH": [2.0] * 10 + [-1.0] * 10,    # 冲高回落:中间位置
    })
    assert _at(panel, f, "600001", "pos60") == pytest.approx(0.0, abs=1e-9)
    assert _at(panel, f, "600001", "dd60") == pytest.approx((0.99 ** 30 - 1) * 100, rel=1e-6)
    assert _at(panel, f, "600002", "pos60") == pytest.approx(1.0) and _at(panel, f, "600002", "dd60") == 0.0
    assert 0.4 < _at(panel, f, "600003", "pos60") < 0.6
    assert _at(panel, f, "600003", "ret20") == pytest.approx((1.02 ** 10 * 0.99 ** 10 - 1) * 100, rel=1e-6)


def test_volatility_ignores_suspended_days_and_energy_compares_recent_turnover():
    swing = [3.0, -3.0] * 30
    panel, f = _features(
        {"600001.SH": swing, "600002.SH": swing, "600003.SH": swing},
        amount={"600002.SH": [None] * 31 + [20000.0] * 30,            # 前 31 行停牌
                "600003.SH": [20000.0] * 56 + [60000.0] * 5})          # 近 5 日放量 3 倍
    assert _at(panel, f, "600001", "vol60") == pytest.approx(3.0, abs=1e-9)
    assert _at(panel, f, "600002", "vol60") == pytest.approx(3.0, abs=1e-9)    # 只算有成交的 30 天
    assert _at(panel, f, "600002", "valid_days") == 30 and not _at(panel, f, "600002", "eligible")
    assert _at(panel, f, "600001", "energy") == pytest.approx(1.0)
    assert _at(panel, f, "600003", "energy") == pytest.approx(60000.0 / (20000.0 + 40000.0 * 5 / 60), rel=1e-6)


def test_contrarian_flow_factors_tell_dip_buying_from_chasing():
    swing = [2.0, -2.0] * 30
    buy_dips = [600.0] + [-800.0, 800.0] * 30      # 首行是补出来的平盘日;之后涨的日子净卖、跌的日子净买
    chase = [600.0] + [800.0, -800.0] * 30
    few_down = [1.0] * 55 + [-1.0] * 5     # 只有 5 个下跌日:下跌日净占比没有意义
    panel, f = _features({"600001.SH": swing, "600002.SH": swing, "600003.SH": few_down},
                         net={"600001.SH": buy_dips, "600002.SH": chase, "600003.SH": 500.0})
    assert _at(panel, f, "600001", "dip60") == pytest.approx(4.0)        # 800 / 20000
    assert _at(panel, f, "600001", "frc60") == pytest.approx(-1.0)
    assert _at(panel, f, "600002", "dip60") == pytest.approx(-4.0)
    assert _at(panel, f, "600002", "frc60") == pytest.approx(1.0)
    assert np.isnan(_at(panel, f, "600003", "dip60"))


# ----------------------------- 策略 -----------------------------

PLANTED = {
    "600001": "ambush_steady",       # 低波动 + 底部 + 逆势承接
    "600002": "ambush_balanced",     # 中等波动
    "600003": "ambush_aggressive",   # 高波动
}


@pytest.fixture(scope="module")
def market():
    """600 只随机游走(日波动 1%~5% 均匀铺开、整体略向下、资金追涨杀跌)+ 若干植入样本。"""
    rng = np.random.default_rng(42)
    n, days = 600, 66
    sigma = np.linspace(1.0, 5.0, n)
    z = rng.normal(0, 1, (days, n))
    pct = np.clip(z * sigma - 0.12 * sigma, -9.5, 9.5)
    amount = 20000.0 * (1.0 - 0.004 * np.arange(days))[:, None] * rng.uniform(0.9, 1.1, (days, n))
    rate = 2.5 * z + rng.normal(0, 1.5, (days, n))                       # 主力净占比 %:随涨跌同向
    rate = np.where(np.abs(rate) < 0.2, 0.2, rate)
    codes = [f"60{i + 100:04d}.SH" for i in range(n)]

    def plant(down, up, flow_sign=-1.0, tail=None, amount_tail=None, name=None):
        moves = np.array([down, up] * (days // 2))
        if tail:
            moves[-len(tail):] = tail
        amt = np.full(days, 20000.0) * (1.0 - 0.004 * np.arange(days))
        if amount_tail:
            amt[-5:] *= amount_tail
        return moves, amt, flow_sign * np.sign(moves) * 8.0, name      # 逆势:跌买涨卖,净占比 ±8%

    planted = {
        "600001.SH": plant(-1.2, 0.8),
        "600002.SH": plant(-3.9, 3.3),
        "600003.SH": plant(-5.0, 4.5),
        "600004.SH": plant(-1.2, 0.8, flow_sign=1.0),                   # 底部但资金追涨杀跌
        "600005.SH": plant(-1.2, 0.8, tail=[10.0, -1.2, 0.8, -1.2, 0.8, -1.2, 0.8, -1.2]),   # 近期涨停过
        "600006.SH": plant(-1.2, 0.8, amount_tail=3.0),                 # 近 5 日放量
        "600007.SH": plant(-0.8, 1.2),                                  # 一路向上:不在底部
        "600008.SH": plant(-1.2, 0.8, name="ST样本"),                    # ST
    }
    slices = []
    for t in range(days):
        rows = [_row(codes[i], round(float(pct[t, i]), 2), float(amount[t, i]),
                     float(amount[t, i] * rate[t, i] / 100.0)) for i in range(n)]
        for code, (moves, amt, flow_rate, name) in planted.items():
            rows.append(_row(code, float(moves[t]), float(amt[t]), float(amt[t] * flow_rate[t] / 100.0), name))
        slices.append((_date(t), bf.build_day_slice(rows)))
    panel = bf.assemble_panel(slices)
    return panel, bf.evaluate(panel)


def test_each_risk_tier_picks_bottom_stocks_with_contrarian_flow(market):
    panel, out = market
    stocks = out["stocks"]
    for code, key in PLANTED.items():
        assert stocks[code]["tags"] == [key], code
        assert stocks[code]["ambush_score"] > 0.97 and stocks[code]["pos60"] <= bf.POS_MAX
    label = bf.STRATEGIES["ambush_steady"].label
    assert stocks["600001"]["hits"][0].startswith(f"{label}·位置") and "承接分位" in stocks["600001"]["hits"][0]
    assert stocks["600001"]["vol_rank"] < 0.5 <= stocks["600002"]["vol_rank"] < 0.8 <= stocks["600003"]["vol_rank"]
    assert out["date"] == panel.dates[-1] and out["repaired_days"] == [] and out["incomplete"] is False


def test_started_expanded_or_chasing_stocks_are_left_out(market):
    _panel_, out = market
    stocks = out["stocks"]
    assert stocks["600004"]["tags"] == [] and stocks["600004"]["ambush_score"] < 0.05   # 在门槛内但承接分垫底
    assert stocks["600004"]["dip60"] == pytest.approx(-8.0, abs=0.01)
    assert stocks["600005"]["tags"] == [] and stocks["600005"]["lu20"] == 1 and stocks["600005"]["ambush_score"] is None
    assert stocks["600006"]["tags"] == [] and stocks["600006"]["energy"] > bf.ENERGY_MAX
    assert stocks["600007"]["tags"] == [] and stocks["600007"]["pos60"] > bf.POS_MAX
    assert stocks["600008"]["tags"] == [] and stocks["600008"]["eligible"] is False
    assert stocks["600008"]["dip60"] == pytest.approx(8.0, abs=0.01)       # 因子照常下发,只是不参与策略


def test_tiers_are_exclusive_and_take_the_top_fraction_of_each_gate(market):
    panel, out = market
    f = bf.compute_features(panel)
    assert all(len(row["tags"]) <= 1 for row in out["stocks"].values())
    for k, (key, _label, _risk, _lo, _hi) in enumerate(bf.TIERS):
        gate = int((f["tier"] == k).sum())
        assert gate >= bf.MIN_RANK_N, (key, gate)
        assert abs(out["counts"][key] - bf.TOP_FRAC * gate) <= 1.5, (key, gate, out["counts"][key])
        picked = bf.STRATEGIES[key].mask(f)
        assert f["ambush_score"][picked].min() >= 1.0 - bf.TOP_FRAC
        assert (f["pos60"][picked] <= bf.POS_MAX).all() and (f["lu20"][picked] == 0).all()
        assert (f["energy"][picked] < bf.ENERGY_MAX).all() and f["eligible"][picked].all()


def test_features_never_look_ahead(market):
    full, _out = market
    cut = bf.Panel(full.dates[:63], full.codes, full.names, full.pct[:63], full.amt[:63], full.net[:63],
                   close=full.close[:63], st=full.st[:63])
    a, b = bf.compute_features(full, 62), bf.compute_features(cut)
    for key in a:
        np.testing.assert_array_equal(np.asarray(a[key]), np.asarray(b[key]), err_msg=key)


def test_small_universe_yields_no_picks_instead_of_noisy_ranks():
    series = {f"60{i:04d}.SH": [-1.2, 0.8] * 30 for i in range(20)}
    out = bf.evaluate(_panel(series, net={code: [800.0, -800.0] * 30 + [800.0] for code in series}))
    assert out["counts"] == {key: 0 for key in bf.STRATEGIES}
    assert all(row["tags"] == [] and row["ambush_score"] is None for row in out["stocks"].values())


def test_stock_suspended_on_the_latest_day_is_left_out():
    out = bf.evaluate(_panel({"600001.SH": [1.0] * 5, "600002.SH": [1.0] * 5},
                             amount={"600002.SH": [20000.0] * 60 + [None]}))
    assert set(out["stocks"]) == {"600001"}


def test_catalog_matches_strategies_and_template_chips():
    from pathlib import Path
    import re

    keys = [item["key"] for item in bf.catalog()]
    assert keys == list(bf.STRATEGIES) == [tier[0] for tier in bf.TIERS]
    assert [item["risk"] for item in bf.catalog()] == ["low", "mid", "high"]
    assert all("回测" not in item["summary"] and "%" not in item["summary"].replace("50%~80%", "")
               for item in bf.catalog())          # 前端文案不带回测数字
    template = (Path(__file__).resolve().parents[1] / "webui/templates/desktop.html").read_text("utf-8")
    chips = re.findall(r'data-bull="([a-z_]+)"\s+data-risk="(low|mid|high)">([^<]+)<', template)
    assert chips == [(s.key, s.risk, s.label) for s in bf.STRATEGIES.values()]
