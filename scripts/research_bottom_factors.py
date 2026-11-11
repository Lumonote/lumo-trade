#!/usr/bin/env python3
"""底部挖掘因子研究框架 —— 「底部埋伏」策略的证据来源(只读桌面软件实库,不写任何数据)。

数据面:``moneyflow_dc`` 全市场日频面板(收盘涨跌幅 / 成交额 / 主力·超大单·大单·中单·小单净额),
逐日完整、无选股偏差。``ohlcv`` 表是「被分析过才缓存」的懒加载子集,带未来函数式的入选偏差,不用。

口径(与实盘可成交一致,全部无未来函数):
  信号   = t 日收盘后用 [.., t] 的数据计算;ST 状态取当日名称(不用最新名称回看)
  买入   = t+1 日收盘;t+1 日停牌或收盘封涨停(买不进)的样本剔除,基准同口径剔除
  收益   = t+1 收盘 → t+1+N 收盘;超额 = 个股收益 − 当日同 cohort 等权平均
  MFE    = 持有期内收盘价相对买入价的最大涨幅(「期间是否启动过」);MAE = 最大回撤
  稳定性 = 信号日切成不重叠的 N 日块(持有期重叠 → 块内相关),按块统计 IC 的 t 值与同号比例

本文件是库 + 命令行(每个阶段对应结论里的一类证据):
  python scripts/research_bottom_factors.py --stage data      # 数据质量交叉校验、基础发生率、市场环境时间线
  python scripts/research_bottom_factors.py --stage scan      # 单因子扫描(全市场 / 底部 cohort)
  python scripts/research_bottom_factors.py --stage fm        # 多元回归:各因子对 超额/大涨/大跌 的独立贡献
  python scripts/research_bottom_factors.py --stage strategy  # 底部埋伏最终方案:三档×三持有期、逐块、市场状态、消融、出场
  python scripts/research_bottom_factors.py --stage parity    # 实盘代码路径(analysis/bull_stock_factors)逐日选股是否与研究一致
面板会缓存到 --cache 目录(默认 /tmp/lumo_bottom_research),库内出现新交易日即自动失效。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from analysis import bull_stock_factors as bf  # noqa: E402  数据修复与最终策略口径和实盘共用

# 桌面软件(com.lumo.trade)实库:验证以用户实际在用的数据为准
DESKTOP_DB = os.path.expanduser("~/Library/Application Support/com.lumo.trade/data/lumo_data.sqlite")
DEFAULT_CACHE = "/tmp/lumo_bottom_research"

MIN_VALID_DAYS = 55     # 61 行窗口内有效交易日下限(剔次新 / 长期停牌)—— 与实盘口径一致
MIN_AMOUNT_WAN = 3000   # 5日均成交额下限(万元)
HORIZONS = (5, 10, 20)
BOOM = {5: 15.0, 10: 20.0, 20: 30.0}    # 各持有期「大涨」阈值(%)
CRASH = {5: -10.0, 10: -12.0, 20: -15.0}  # 各持有期「大跌」阈值(%)
INDEX_CODES = {"csi1000": "000852.SH", "gz2000": "399303.SZ", "hs300": "000300.SH", "sh": "000001.SH"}

_TIERS = ("net", "elg", "lg", "md", "sm")
_SQL = """
SELECT ts_code, name, pct_change, close,
       net_amount, net_amount_rate, buy_elg_amount, buy_elg_amount_rate,
       buy_lg_amount, buy_lg_amount_rate, buy_md_amount, buy_md_amount_rate,
       buy_sm_amount, buy_sm_amount_rate
FROM moneyflow_dc WHERE top_n=0 AND trade_date=?
"""


# ============================== 面板 ==============================

@dataclass
class RPanel:
    dates: List[str]          # 升序交易日(ISO)
    codes: List[str]
    names: List[str]          # 最新名称(仅展示用;ST 判定用 st 矩阵)
    pct: np.ndarray           # (T,N) 日涨跌幅 %,该日无行 → NaN
    close: np.ndarray         # (T,N) 未复权收盘价
    amt: np.ndarray           # (T,N) 成交额 万元(净额÷净占比反推),无成交 → NaN
    net: np.ndarray           # (T,N) 主力净额 万元(= 超大单 + 大单)
    elg: np.ndarray
    lg: np.ndarray
    md: np.ndarray
    sm: np.ndarray
    st: np.ndarray            # (T,N) bool 当日名称含 ST/退
    index: Dict[str, np.ndarray]   # 指数收盘 (T,),缺 → NaN
    traded: Optional[np.ndarray] = None   # (T,N) bool 有效交易日(bf.repair_panel 产出)
    mv: Optional[np.ndarray] = None       # (N,) 最近一期流通市值(万元),缺 → NaN;仅用于规模稳健性检验
    mv_date: str = ""
    stale_days: Tuple[str, ...] = ()      # 被识别为盘中快照并修复的交易日
    gap_fills: int = 0                    # 补回的单股缺行数


def _connect(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)


def load_panel(db: str = DESKTOP_DB, cache_dir: Optional[str] = DEFAULT_CACHE, refresh: bool = False) -> RPanel:
    """读库装配研究面板;带 npz 缓存(以库内最新交易日为缓存键,数据更新即失效)。"""
    conn = _connect(db)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM moneyflow_dc WHERE top_n=0 ORDER BY trade_date")]
    if not dates:
        raise SystemExit("moneyflow_dc 全市场桶(top_n=0)无数据")
    cache = os.path.join(cache_dir, f"panel_v3_{dates[0]}_{dates[-1]}_{len(dates)}.npz") if cache_dir else None
    if cache and os.path.exists(cache) and not refresh:
        z = np.load(cache, allow_pickle=False)
        return RPanel(dates=[str(d) for d in z["dates"]], codes=[str(c) for c in z["codes"]],
                      names=[str(n) for n in z["names"]],
                      **{k: z[k] for k in ("pct", "close", "amt", "net", "elg", "lg", "md", "sm", "st",
                                           "traded", "mv")},
                      index={k: z[f"idx_{k}"] for k in INDEX_CODES}, mv_date=str(z["mv_date"]),
                      stale_days=tuple(str(d) for d in z["stale_days"]), gap_fills=int(z["gap_fills"]))
    index: Dict[str, int] = {}
    names: List[str] = []
    days: List[Tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    started = time.time()
    for i, date in enumerate(dates):
        cols: List[int] = []
        st_flags: List[bool] = []
        nums: List[List[float]] = []
        seen = set()
        for row in conn.execute(_SQL, (date,)):
            code = str(row[0] or "").split(".")[0].strip()
            if len(code) != 6 or not code.isdigit() or code in seen:
                continue
            seen.add(code)
            name = str(row[1] or "")
            pos = index.get(code)
            if pos is None:
                pos = index[code] = len(names)
                names.append(name)
            elif name:
                names[pos] = name
            cols.append(pos)
            st_flags.append("ST" in name.upper() or "退" in name)
            nums.append([np.nan if v is None else float(v) for v in row[2:]])
        days.append((np.asarray(cols, dtype=np.int64), np.asarray(st_flags, dtype=bool),
                     np.asarray(nums, dtype=float).reshape(len(cols), 12)))
        if (i + 1) % 60 == 0:
            print(f"  已载入 {i + 1}/{len(dates)} 日 ({time.time() - started:.0f}s)", file=sys.stderr)
    t_count, n_count = len(dates), len(index)
    arr = {k: np.full((t_count, n_count), np.nan) for k in ("pct", "close", "amt", *_TIERS)}
    st = np.zeros((t_count, n_count), dtype=bool)
    for t, (cols, st_flags, nums) in enumerate(days):
        arr["pct"][t, cols] = nums[:, 0]
        arr["close"][t, cols] = nums[:, 1]
        amounts, rates = np.abs(nums[:, 2::2]), np.abs(nums[:, 3::2])     # 5 档:净额 / 净占比
        usable = np.isfinite(amounts) & np.isfinite(rates) & (rates >= 0.05)
        rates_ok = np.where(usable, rates, 0.0)
        best = rates_ok.argmax(axis=1)                                     # 净占比绝对值最大的一档 → 舍入误差最小
        rows = np.arange(len(cols))
        best_rate = rates_ok[rows, best]
        with np.errstate(divide="ignore", invalid="ignore"):
            amount = np.where(best_rate > 0, amounts[rows, best] / best_rate * 100.0, np.nan)
        arr["amt"][t, cols] = amount
        for j, tier in enumerate(_TIERS):
            arr[tier][t, cols] = nums[:, 2 + 2 * j]
        st[t, cols] = st_flags
    iso = {d.replace("-", ""): k for k, d in enumerate(dates)}
    idx: Dict[str, np.ndarray] = {}
    for key, ts_code in INDEX_CODES.items():
        series = np.full(t_count, np.nan)
        for trade_date, close in conn.execute(
                "SELECT trade_date, close FROM market_daily WHERE ts_code=?", (ts_code,)):
            k = iso.get(str(trade_date).replace("-", ""))
            if k is not None and close is not None:
                series[k] = float(close)
        idx[key] = series
    panel = RPanel(dates=list(dates), codes=list(index), names=names, st=st, index=idx, **arr)
    # 流通市值(只在「市场资金」页刷新过才有;取最近一期,仅用于规模稳健性检验)
    panel.mv = np.full(n_count, np.nan)
    row = conn.execute("SELECT key, payload FROM kv_cache WHERE namespace='market_capital' "
                       "AND key LIKE 'raw:daily_basic:%' ORDER BY key DESC LIMIT 1").fetchone()
    if row:
        import json

        panel.mv_date = str(row[0]).rsplit(":", 1)[-1]
        for item in json.loads(row[1]):
            pos = index.get(str(item.get("ts_code") or "").split(".")[0])
            if pos is not None and item.get("circ_mv"):
                panel.mv[pos] = float(item["circ_mv"])
    info = bf.repair_panel(panel, flow_fields=("amt",) + _TIERS)      # 与实盘筛选同一个修复函数
    panel.stale_days, panel.gap_fills = tuple(info["stale_days"]), int(info["gap_fills"])
    if cache:
        os.makedirs(cache_dir, exist_ok=True)
        np.savez_compressed(cache, dates=np.array(panel.dates), codes=np.array(panel.codes),
                            names=np.array(panel.names), st=st, traded=panel.traded, mv=panel.mv,
                            mv_date=np.array(panel.mv_date), stale_days=np.array(panel.stale_days, dtype=str),
                            gap_fills=np.array(panel.gap_fills),
                            **{k: getattr(panel, k) for k in arr}, **{f"idx_{k}": v for k, v in idx.items()})
    return panel


# ============================== 滚动算子(只用 ≤ t 的行) ==============================

def _roll_sum(x: np.ndarray, w: int) -> np.ndarray:
    c = np.cumsum(x, axis=0, dtype=np.float64)
    out = np.full(x.shape, np.nan)
    if x.shape[0] >= w:
        out[w - 1] = c[w - 1]
        out[w:] = c[w:] - c[:-w]
    return out


def _roll_apply(x: np.ndarray, w: int, fn: Callable[[np.ndarray], np.ndarray], chunk: int = 16) -> np.ndarray:
    """对每个 w 行窗口(末行为 t)调用 fn;按行分块,避免 fn 物化整块 (T,N,w) 中间数组。"""
    out = np.full(x.shape, np.nan)
    if x.shape[0] >= w:
        view = sliding_window_view(x, w, axis=0)          # (T-w+1, N, w)
        for start in range(0, view.shape[0], chunk):
            out[w - 1 + start:w - 1 + start + chunk] = fn(view[start:start + chunk])
    return out


def _roll_max(x: np.ndarray, w: int) -> np.ndarray:
    return _roll_apply(x, w, lambda v: v.max(axis=-1))


def _roll_min(x: np.ndarray, w: int) -> np.ndarray:
    return _roll_apply(x, w, lambda v: v.min(axis=-1))


def _since_extreme(x: np.ndarray, w: int, kind: str) -> np.ndarray:
    """距窗口内最近一次极值的行数(0 = 今天就是极值)。"""
    pick = np.argmin if kind == "min" else np.argmax
    return _roll_apply(x, w, lambda v: pick(v[..., ::-1], axis=-1).astype(float))


def _shift(x: np.ndarray, k: int) -> np.ndarray:
    """向后平移 k 行:out[t] = x[t-k]。"""
    out = np.full(x.shape, np.nan)
    if k < x.shape[0]:
        out[k:] = x[:-k]
    return out


def _div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    return np.where(np.isfinite(out), out, np.nan)


def limit_matrix(panel: RPanel) -> np.ndarray:
    """(T,N) 涨停阈值 %:北交所 29.8 / 创业·科创 19.8 / 主板 9.8,主板当日 ST 4.8。"""
    base = np.empty(len(panel.codes))
    main = np.zeros(len(panel.codes), dtype=bool)
    for i, code in enumerate(panel.codes):
        if code[:1] in ("4", "8", "9"):
            base[i] = 29.8
        elif code.startswith(("30", "688", "689")):
            base[i] = 19.8
        else:
            base[i], main[i] = 9.8, True
    out = np.broadcast_to(base, panel.pct.shape).copy()
    out[panel.st & main[None, :]] = 4.8
    return out


def board_vector(codes: Sequence[str]) -> np.ndarray:
    """0 主板 / 1 创业板 / 2 科创板 / 3 北交所。"""
    out = np.zeros(len(codes))
    for i, code in enumerate(codes):
        if code[:1] in ("4", "8", "9"):
            out[i] = 3
        elif code.startswith(("688", "689")):
            out[i] = 2
        elif code.startswith("30"):
            out[i] = 1
    return out


# ============================== 因子 ==============================

def build_features(panel: RPanel) -> Dict[str, np.ndarray]:
    """全部候选因子的 (T,N) 矩阵;第 t 行只用到 ≤ t 的数据。另含 valid / eligible / limit_up 等布尔矩阵。"""
    pct, amt = panel.pct, panel.amt
    t_count, n_count = pct.shape
    # 有效交易日:有成交(停牌行是整行 0,无成交额);修复过的面板以 traded 为准
    valid = panel.traded if panel.traded is not None else np.isfinite(pct) & np.isfinite(amt)
    r0 = np.where(valid, pct, 0.0)
    price = np.cumprod(1.0 + r0 / 100.0, axis=0)           # 复权价格指数(涨跌幅链乘,不受除权影响)
    f: Dict[str, np.ndarray] = {"valid": valid, "price": price}

    def trailing(k: int) -> np.ndarray:
        return (_div(price, _shift(price, k)) - 1.0) * 100.0

    def mean(x: np.ndarray, w: int) -> np.ndarray:
        return _roll_sum(x, w) / w

    # ---- 位置 ----
    f["ret1"] = np.where(valid, pct, np.nan)
    for k in (3, 5, 10, 20, 60, 120):
        f[f"ret{k}"] = trailing(k)
    ma = {w: mean(price, w) for w in (5, 10, 20, 60)}
    for w in (5, 10, 20, 60):
        f[f"dev{w}"] = (_div(price, ma[w]) - 1.0) * 100.0
    for w in (20, 60, 120):
        hi, lo = _roll_max(price, w), _roll_min(price, w)
        f[f"pos{w}"] = _div(price - lo, hi - lo)                       # 区间位置 0~1
        f[f"dd{w}"] = (_div(price, hi) - 1.0) * 100.0                  # 距区间高点回撤(≤0)
        f[f"up_low{w}"] = (_div(price, lo) - 1.0) * 100.0              # 距区间低点反弹(≥0)
        f[f"rng{w}"] = (_div(hi, lo) - 1.0) * 100.0                    # 区间振幅
        if w >= 60:
            f[f"since_low{w}"] = _since_extreme(price, w, "min")
            f[f"since_high{w}"] = _since_extreme(price, w, "max")
    f["rng10"] = (_div(_roll_max(price, 10), _roll_min(price, 10)) - 1.0) * 100.0
    f["hl10"] = (_div(_roll_min(price, 10), _shift(_roll_min(price, 10), 10)) - 1.0) * 100.0   # 近10日低点 vs 前10日低点
    f["ma20_slope"] = (_div(ma[20], _shift(ma[20], 5)) - 1.0) * 100.0
    f["ma60_slope"] = (_div(ma[60], _shift(ma[60], 10)) - 1.0) * 100.0
    f["above_ma20"] = (price >= ma[20]).astype(float)
    f["above_ma60"] = (price >= ma[60]).astype(float)
    f["ma5_gt_ma20"] = (ma[5] > ma[20]).astype(float)
    def _runup(v: np.ndarray) -> np.ndarray:
        # 时序正确的窗口内最大涨幅:每个点相对「此前窗口内最低点」的涨幅取最大
        return (v / np.minimum.accumulate(v, axis=-1)).max(axis=-1) * 100.0 - 100.0

    for w in (60, 120):
        f[f"runup{w}"] = _roll_apply(price, w, _runup)

    # ---- 波动 / 收缩 ----
    cnt = {w: _roll_sum(valid.astype(float), w) for w in (5, 10, 20, 60, 61, 121)}
    sq = r0 * r0
    for w in (5, 10, 20, 60):
        m1, m2 = _div(_roll_sum(r0, w), cnt[w]), _div(_roll_sum(sq, w), cnt[w])
        f[f"vol{w}"] = np.sqrt(np.clip(m2 - m1 * m1, 0.0, None))
    f["vcr"] = _div(f["vol10"], f["vol60"])                # 波动收缩比(<1 收缩)
    f["vcr5"] = _div(f["vol5"], f["vol20"])
    absdiff = np.abs(np.diff(price, axis=0, prepend=price[:1]))
    for w in (10, 20, 60):
        f[f"er{w}"] = _div(np.abs(price - _shift(price, w)), _roll_sum(absdiff, w))   # 路径效率 0~1
    down = np.where(r0 < 0, r0, 0.0)
    f["dvol20"] = np.sqrt(_div(_roll_sum(down * down, 20), cnt[20]))   # 下行波动
    f["skew20"] = _div(_roll_sum(np.where(r0 > 0, r0, 0.0) ** 2, 20), _roll_sum(sq, 20))  # 上行方差占比

    # ---- 量能 ----
    a0 = np.where(np.isfinite(amt), amt, 0.0)
    have = np.isfinite(amt).astype(float)
    amean = {w: _div(_roll_sum(a0, w), _roll_sum(have, w)) for w in (5, 10, 20, 60)}
    f["amt5"], f["amt20"], f["amt60"] = amean[5], amean[20], amean[60]
    f["ln_amt60"] = np.log(np.where(amean[60] > 0, amean[60], np.nan))
    f["energy"] = _div(amean[5], amean[60])                # 5日/60日均额
    f["energy20"] = _div(amean[20], amean[60])
    f["energy10_60"] = _div(amean[10], amean[60])
    f["amt_ratio"] = _div(amt, _shift(amean[5], 1))        # 当日额/前5日均额
    amax20 = _roll_max(a0, 20)
    f["amt_peak"] = _div(amt, amax20)                      # 当日额/20日峰值额
    f["amt_dry"] = _div(_roll_min(np.where(np.isfinite(amt), amt, np.inf), 5), amean[60])   # 近5日最小额/60日均额
    up_amt = _roll_sum(a0 * (r0 > 0), 20)
    dn_amt = _roll_sum(a0 * (r0 < 0), 20)
    f["udr20"] = _div(up_amt, dn_amt)                      # 上涨日成交额/下跌日成交额
    f["ln_price"] = np.log(np.where(panel.close > 0, panel.close, np.nan))
    if panel.mv is not None and panel.mv_date in panel.dates:
        # 流通市值的历史近似:最近一期市值 × 复权价格比(忽略期间股本变动)
        anchor = price[panel.dates.index(panel.mv_date)]
        f["ln_mv"] = np.log(np.where(panel.mv > 0, panel.mv, np.nan)[None, :] * _div(price, anchor[None, :]))

    # ---- 资金(主力 = 超大单 + 大单;小单 ≈ 散户) ----
    flow = {k: np.where(np.isfinite(getattr(panel, k)), getattr(panel, k), 0.0) for k in _TIERS}
    for k in ("net", "elg", "lg", "md", "sm"):
        for w in (5, 20, 60):
            f[f"{k}_rate{w}"] = _div(_roll_sum(flow[k], w), _roll_sum(a0, w)) * 100.0
    f["net_rate1"] = _div(panel.net, amt) * 100.0
    f["net_rate10"] = _div(_roll_sum(flow["net"], 10), _roll_sum(a0, 10)) * 100.0
    for w in (5, 10, 20, 60):
        f[f"net_pos{w}"] = _roll_sum((flow["net"] > 0).astype(float), w)
    f["elg_pos20"] = _roll_sum((flow["elg"] > 0).astype(float), 20)
    f["sm_neg20"] = _roll_sum((flow["sm"] < 0).astype(float), 20)
    f["flow_accel"] = f["net_rate5"] - f["net_rate20"]
    f["dip_buy20"] = _div(_roll_sum(flow["net"] * (r0 < 0), 20), dn_amt) * 100.0    # 下跌日的主力净占比(逆势吸筹)
    f["up_net20"] = _div(_roll_sum(flow["net"] * (r0 > 0), 20), up_amt) * 100.0     # 上涨日的主力净占比
    # ---- 逆向资金族:下跌日谁在接、上涨日谁在追(同一想法的不同表达,择优用) ----
    is_dn, is_up = valid & (r0 < 0), valid & (r0 > 0)
    net_rate_day = np.where(valid & (a0 > 0), flow["net"] / np.where(a0 > 0, a0, 1.0) * 100.0, 0.0)
    for w, min_dn in ((20, 4), (40, 8), (60, 12)):
        n_dn = _roll_sum(is_dn.astype(float), w)
        n_up = _roll_sum(is_up.astype(float), w)
        enough = (n_dn >= min_dn) & (n_up >= min_dn)        # 两类日子都要有足够样本,比率才有意义
        dn_a, up_a = _roll_sum(a0 * is_dn, w), _roll_sum(a0 * is_up, w)
        dip = _div(_roll_sum(flow["net"] * is_dn, w), dn_a) * 100.0
        chase = _div(_roll_sum(flow["net"] * is_up, w), up_a) * 100.0
        f[f"dip{w}"] = np.where(enough, dip, np.nan)                         # 下跌日主力净占比
        f[f"chase{w}"] = np.where(enough, chase, np.nan)                     # 上涨日主力净占比
        f[f"contra{w}"] = np.where(enough, dip - chase, np.nan)              # 逆向差:跌时买、涨时不追
        f[f"absorb{w}"] = np.where(enough, -_div(_roll_sum(flow["sm"] * is_dn, w), dn_a) * 100.0, np.nan)  # 下跌日非散户净买
        f[f"dip_cnt{w}"] = np.where(enough, _div(_roll_sum((is_dn & (flow["net"] > 0)).astype(float), w), n_dn), np.nan)
        f[f"chase_cnt{w}"] = np.where(enough, _div(_roll_sum((is_up & (flow["net"] > 0)).astype(float), w), n_up), np.nan)
        f[f"contra_cnt{w}"] = f[f"dip_cnt{w}"] - f[f"chase_cnt{w}"]
        # 日度「主力净占比」与「涨跌幅」的滚动相关:负相关 = 逆向资金,正相关 = 追涨资金
        cnt_w = _roll_sum(valid.astype(float), w)
        mx, my = _div(_roll_sum(net_rate_day, w), cnt_w), _div(_roll_sum(r0, w), cnt_w)
        cov = _div(_roll_sum(net_rate_day * r0, w), cnt_w) - mx * my
        vx = _div(_roll_sum(net_rate_day * net_rate_day, w), cnt_w) - mx * mx
        vy = _div(_roll_sum(r0 * r0, w), cnt_w) - my * my
        f[f"frc{w}"] = np.where(cnt_w >= w * 0.9, _div(cov, np.sqrt(np.clip(vx * vy, 0.0, None))), np.nan)
    streak = np.zeros(pct.shape)
    alive = flow["net"] > 0
    run = np.zeros(n_count)
    for t in range(t_count):
        run = np.where(alive[t], run + 1.0, 0.0)
        streak[t] = run
    f["streak"] = streak

    # ---- 启动迹象 / 股性 ----
    limits = limit_matrix(panel)
    limit_up = valid & (pct >= limits)
    limit_dn = valid & (pct <= -limits)
    f["limit_up"] = limit_up
    for w in (5, 20, 60):
        f[f"lu{w}"] = _roll_sum(limit_up.astype(float), w)
    f["ld20"] = _roll_sum(limit_dn.astype(float), 20)
    since = np.full(pct.shape, 99.0)
    last_seen = np.full(n_count, 99.0)
    for t in range(t_count):
        last_seen = np.where(limit_up[t], 0.0, np.minimum(last_seen + 1.0, 99.0))
        since[t] = last_seen
    f["since_lu"] = since
    for w in (5, 10):
        f[f"up_days{w}"] = _roll_sum((r0 > 0).astype(float), w)
    f["big_up20"] = _roll_sum((r0 >= 5.0).astype(float), 20)
    f["big_dn20"] = _roll_sum((r0 <= -5.0).astype(float), 20)
    rr = np.where(valid, pct, -np.inf)
    for w in (5, 20, 60):
        f[f"max_ret{w}"] = _roll_max(rr, w)
    f["min_ret20"] = _roll_min(np.where(valid, pct, np.inf), 20)
    f["board"] = np.broadcast_to(board_vector(panel.codes), pct.shape).copy()
    delta = np.diff(price, axis=0, prepend=price[:1])
    gain, loss = mean(np.clip(delta, 0, None), 14), mean(np.clip(-delta, 0, None), 14)
    f["rsi"] = np.where(gain + loss > 0, 100.0 * _div(gain, gain + loss), 50.0)

    # ---- 准入(与实盘 eligible 同口径;ST 取当日名称) ----
    f["valid_days"] = cnt[61]
    f["valid_days121"] = cnt[121]
    f["eligible"] = ((cnt[61] >= MIN_VALID_DAYS) & valid & (np.nan_to_num(amean[5]) >= MIN_AMOUNT_WAN)
                     & ~panel.st)
    for key, val in list(f.items()):
        if val.dtype == np.float64 and key != "price":
            f[key] = np.where(np.isinf(val), np.nan, val).astype(np.float32)
    return f


def add_residual_flow(f: Dict[str, np.ndarray]) -> None:
    """资金-价格背离:逐日横截面把资金净占比对同期涨幅回归,取残差(涨幅解释不了的那部分流入)。"""
    elig = f["eligible"]
    for w in (20, 60):
        x_all, y_all = f[f"ret{w}"], f[f"net_rate{w}"]
        out = np.full(x_all.shape, np.nan, dtype=np.float32)
        for t in range(x_all.shape[0]):
            ok = elig[t] & np.isfinite(x_all[t]) & np.isfinite(y_all[t])
            if ok.sum() < 200:
                continue
            x = np.clip(x_all[t, ok], *np.percentile(x_all[t, ok], [1, 99]))
            y = y_all[t, ok]
            slope, intercept = np.polyfit(x, y, 1)
            out[t, ok] = y - (slope * x + intercept)
        f[f"net_resid{w}"] = out


# ============================== 目标 ==============================

def build_targets(panel: RPanel, f: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    """前向收益(%):sig{N}=t→t+N;nxt{N}=t+1→t+1+N(可成交口径);mfe{N}/mae{N} 相对 t+1 买入价。"""
    price, pct = f["price"], panel.pct
    t_count = price.shape[0]
    exists = np.isfinite(pct)
    limits = limit_matrix(panel)
    out: Dict[str, np.ndarray] = {}
    nxt_valid = np.zeros(pct.shape, dtype=bool)
    nxt_valid[:-1] = f["valid"][1:] & ~(pct[1:] >= limits[1:])       # t+1 有成交且收盘未封涨停
    out["buyable"] = nxt_valid
    miss = np.cumsum(~exists, axis=0)                                  # 缺行累计,用于「区间内每天都有行」
    for n in HORIZONS:
        sig = np.full(pct.shape, np.nan)
        nxt = np.full(pct.shape, np.nan)
        mfe = np.full(pct.shape, np.nan)
        mae = np.full(pct.shape, np.nan)
        if t_count > n:
            ok = (miss[n:] - miss[:-n]) == 0
            sig[:-n] = np.where(ok, (price[n:] / price[:-n] - 1.0) * 100.0, np.nan)
        if t_count > n + 1:
            ok = (miss[n + 1:] - miss[1:-n]) == 0
            nxt[:-n - 1] = np.where(ok, (price[n + 1:] / price[1:-n] - 1.0) * 100.0, np.nan)
            path = sliding_window_view(price[2:], n, axis=0)          # 行 k ↔ price[k+2 .. k+1+n]
            rows = path.shape[0]
            entry = price[1:1 + rows]
            mfe[:rows] = np.where(ok[:rows], (path.max(axis=-1) / entry - 1.0) * 100.0, np.nan)
            mae[:rows] = np.where(ok[:rows], (path.min(axis=-1) / entry - 1.0) * 100.0, np.nan)
        out[f"sig{n}"], out[f"nxt{n}"], out[f"mfe{n}"], out[f"mae{n}"] = sig, nxt, mfe, mae
    return out


def market_context(panel: RPanel, f: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    """逐日市场环境(T,):指数趋势 + 全市场宽度;只用 ≤ t 的数据。"""
    out: Dict[str, np.ndarray] = {}
    for key, series in panel.index.items():
        s = series[:, None]
        for k in (5, 20, 60):
            out[f"{key}_ret{k}"] = ((_div(s, _shift(s, k)) - 1.0) * 100.0)[:, 0]
        out[f"{key}_dev20"] = ((_div(s, _roll_sum(s, 20) / 20.0) - 1.0) * 100.0)[:, 0]
        out[f"{key}_dd60"] = ((_div(s, _roll_max(s, 60)) - 1.0) * 100.0)[:, 0]
    elig = f["eligible"]
    with np.errstate(invalid="ignore"):
        n_elig = elig.sum(axis=1).astype(float)
        out["breadth_ma20"] = _div((elig & (f["above_ma20"] > 0)).sum(axis=1).astype(float), n_elig)
        out["low_share"] = _div((elig & (f["pos60"] <= 0.2)).sum(axis=1).astype(float), n_elig)
        out["med_ret20"] = np.array([np.nanmedian(f["ret20"][t, elig[t]]) if elig[t].any() else np.nan
                                     for t in range(elig.shape[0])])
    return out


# ============================== 统计工具 ==============================

def rankdata(x: np.ndarray) -> np.ndarray:
    """平均秩(并列取均值),1..n。"""
    n = x.size
    sorter = np.argsort(x, kind="mergesort")
    inv = np.empty(n, dtype=np.int64)
    inv[sorter] = np.arange(n)
    xs = x[sorter]
    obs = np.r_[True, xs[1:] != xs[:-1]]
    dense = obs.cumsum()[inv]
    count = np.r_[np.nonzero(obs)[0], n]
    return 0.5 * (count[dense] + count[dense - 1] + 1)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / denom) if denom > 0 else np.nan


def signal_days(t_count: int, horizon: int, lookback: int = 60) -> np.ndarray:
    """可用信号日的行号:有足够历史,且 t+1+N 仍在面板内。"""
    return np.arange(lookback, t_count - horizon - 1)


def block_ids(days: np.ndarray, horizon: int) -> np.ndarray:
    """把信号日切成不重叠的 N 日块(持有期重叠 → 同块内的日度统计高度相关)。"""
    return (days - days[0]) // max(1, horizon)


def block_summary(values: np.ndarray, blocks: np.ndarray) -> Tuple[float, float, float, int]:
    """(均值, 块 t 值, 与均值同号的块占比, 块数) —— 以块均值为独立观测。"""
    ok = np.isfinite(values)
    if not ok.any():
        return np.nan, np.nan, np.nan, 0
    ids = np.unique(blocks[ok])
    means = np.array([values[ok & (blocks == b)].mean() for b in ids])
    m = float(means.mean())
    sd = float(means.std(ddof=1)) if means.size > 1 else np.nan
    t = m / (sd / np.sqrt(means.size)) if sd and sd > 0 else np.nan
    same = float((np.sign(means) == np.sign(m)).mean()) if m != 0 else np.nan
    return m, t, same, int(means.size)


@dataclass
class FactorStat:
    name: str
    ic: float
    ic_t: float
    ic_same: float
    blocks: int
    top_excess: float      # 顶十分位(因子最大)平均超额 %
    bot_excess: float
    top_boom: float        # 顶十分位大涨倍率
    top_crash: float
    bot_boom: float
    bot_crash: float
    mono: float            # 十分位均值与序号的秩相关
    n: int


def factor_stat(name: str, factor: np.ndarray, target: np.ndarray, mask: np.ndarray, days: np.ndarray,
                horizon: int, boom: float, crash: float, n_q: int = 10, min_n: int = 100) -> Optional[FactorStat]:
    """单因子在给定 cohort(mask)内的横截面检验。mask 应已含「可成交 + 目标有限」。"""
    blocks = block_ids(days, horizon)
    ics = np.full(days.size, np.nan)
    q_sum, q_cnt = np.zeros(n_q), np.zeros(n_q)
    q_boom, q_crash = np.zeros(n_q), np.zeros(n_q)
    base_boom = base_crash = total = 0.0
    for k, t in enumerate(days):
        ok = mask[t] & np.isfinite(factor[t])
        n = int(ok.sum())
        if n < min_n:
            continue
        x, y = factor[t, ok].astype(float), target[t, ok]
        rx = rankdata(x)
        ics[k] = _corr(rx, rankdata(y))
        ex = y - y.mean()
        q = np.minimum(((rx - 0.5) / n * n_q).astype(int), n_q - 1)
        q_sum += np.bincount(q, weights=ex, minlength=n_q)
        q_cnt += np.bincount(q, minlength=n_q)
        q_boom += np.bincount(q, weights=(y >= boom).astype(float), minlength=n_q)
        q_crash += np.bincount(q, weights=(y <= crash).astype(float), minlength=n_q)
        base_boom += float((y >= boom).sum())
        base_crash += float((y <= crash).sum())
        total += n
    if total == 0 or not np.isfinite(ics).any():
        return None
    ic, ic_t, same, n_blocks = block_summary(ics, blocks)
    with np.errstate(invalid="ignore", divide="ignore"):
        q_mean = q_sum / q_cnt
        boom_r = (q_boom / q_cnt) / max(base_boom / total, 1e-12)
        crash_r = (q_crash / q_cnt) / max(base_crash / total, 1e-12)
    filled = np.isfinite(q_mean)
    mono = _corr(rankdata(np.arange(n_q)[filled].astype(float)), rankdata(q_mean[filled])) if filled.sum() > 2 else np.nan
    top, bot = np.nonzero(filled)[0][-1], np.nonzero(filled)[0][0]
    return FactorStat(name, ic, ic_t, same, n_blocks, float(q_mean[top]), float(q_mean[bot]),
                      float(boom_r[top]), float(crash_r[top]), float(boom_r[bot]), float(crash_r[bot]),
                      mono, int(total))


def rank_in(arr: np.ndarray, mask: np.ndarray, days: Iterable[int], min_n: int = 50) -> np.ndarray:
    """逐日在 mask 内的秩分位 (0,1);mask 外 / 因子缺失 → NaN。"""
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    for t in days:
        ok = mask[t] & np.isfinite(arr[t])
        n = int(ok.sum())
        if n >= min_n:
            out[t, ok] = (rankdata(arr[t, ok].astype(float)) - 0.5) / n
    return out


def top_k(score: np.ndarray, mask: np.ndarray, days: Iterable[int], k: int) -> np.ndarray:
    """逐日取 mask 内 score 最高的 k 只 → (T,N) bool。"""
    out = np.zeros(score.shape, dtype=bool)
    for t in days:
        ok = np.nonzero(mask[t] & np.isfinite(score[t]))[0]
        if ok.size:
            out[t, ok[np.argsort(-score[t, ok], kind="mergesort")[:k]]] = True
    return out


def fama_macbeth(rs: "Research", cohort: np.ndarray, factors: Sequence[str], horizon: int,
                 start: int = 60, min_n: int = 150) -> Tuple[Dict[str, List[Tuple[str, float, float, float]]], int]:
    """逐日横截面回归(因子取 cohort 内秩分位 −0.5~0.5),系数按不重叠块汇总。

    返回 ({excess|boom|crash: [(因子, 系数, 块t, 同号占比)]}, 块数)。系数含义:因子从 cohort 内
    最低到最高,超额(%)或大涨/大跌概率(百分点)的变化 —— 已控制同表其它因子。
    """
    days = rs.days(horizon, start)
    blocks = block_ids(days, horizon)
    mask = rs.tradable(horizon) & cohort
    target = rs.y[f"nxt{horizon}"]
    coefs = {k: np.full((days.size, len(factors)), np.nan) for k in ("excess", "boom", "crash")}
    for k, t in enumerate(days):
        ok = mask[t].copy()
        for name in factors:
            ok &= np.isfinite(rs.f[name][t])
        n = int(ok.sum())
        if n < min_n:
            continue
        x = np.column_stack([np.ones(n)] + [(rankdata(rs.f[name][t, ok].astype(float)) - 0.5) / n - 0.5
                                            for name in factors])
        yy = target[t, ok]
        for key, dep in (("excess", yy), ("boom", (yy >= BOOM[horizon]) * 100.0),
                         ("crash", (yy <= CRASH[horizon]) * 100.0)):
            coefs[key][k] = np.linalg.lstsq(x, dep, rcond=None)[0][1:]
    out = {key: [(name, *block_summary(c[:, j], blocks)[:3]) for j, name in enumerate(factors)]
           for key, c in coefs.items()}
    return out, len(np.unique(blocks[np.isfinite(coefs["excess"][:, 0])]))


def pick_stats(target: np.ndarray, picks: np.ndarray, base: np.ndarray, days: np.ndarray, horizon: int,
               boom: float, crash: float, extra: Optional[Dict[str, np.ndarray]] = None) -> Dict[str, float]:
    """入选集合 picks(T,N bool)相对基准 base(T,N bool)的表现。均已含「可成交 + 目标有限」。

    超额按「当日入选等权组合 − 当日基准等权」逐日计算,再按不重叠块汇总(块 t 值);
    尾部倍率 = 入选样本尾部占比 ÷ 基准占比(样本池化)。
    """
    blocks = block_ids(days, horizon)
    daily = {k: np.full(days.size, np.nan) for k in
             ("ex", "mean", "win", "boom", "crash", "base_boom", "base_crash", "d_boom", "d_crash")}
    vals: List[np.ndarray] = []
    exs: List[np.ndarray] = []
    base_vals: List[np.ndarray] = []
    counts: List[int] = []
    more: Dict[str, List[np.ndarray]] = {k: [] for k in (extra or {})}
    for k, t in enumerate(days):
        b = base[t]
        if not b.any():
            continue
        base_y = target[t, b]
        base_vals.append(base_y)
        p = picks[t] & b
        counts.append(int(p.sum()))
        if not p.any():
            continue
        y = target[t, p]
        daily["ex"][k], daily["mean"][k], daily["win"][k] = y.mean() - base_y.mean(), y.mean(), (y > 0).mean()
        daily["boom"][k], daily["crash"][k] = (y >= boom).mean(), (y <= crash).mean()
        daily["base_boom"][k], daily["base_crash"][k] = (base_y >= boom).mean(), (base_y <= crash).mean()
        daily["d_boom"][k] = (daily["boom"][k] - daily["base_boom"][k]) * 100.0
        daily["d_crash"][k] = (daily["crash"][k] - daily["base_crash"][k]) * 100.0
        vals.append(y)
        exs.append(y - base_y.mean())
        for key, arr in (extra or {}).items():
            more[key].append(arr[t, p])
    if not vals:
        return {"n": 0}
    v, e, bv = np.concatenate(vals), np.concatenate(exs), np.concatenate(base_vals)
    m, t_stat, same, n_blocks = block_summary(daily["ex"], blocks)
    d_boom, t_boom, _, _ = block_summary(daily["d_boom"], blocks)
    d_crash, t_crash, _, _ = block_summary(daily["d_crash"], blocks)
    out = {
        "n": int(v.size), "per_day": float(np.mean(counts)), "days_hit": float(np.mean(np.array(counts) > 0)),
        "mean": float(v.mean()), "excess": float(e.mean()), "day_excess": m, "t": t_stat, "same": same,
        "blocks": n_blocks, "win": float((v > 0).mean()), "beat": float((e > 0).mean()),
        "median": float(np.median(v)), "base_mean": float(bv.mean()),
        "boom": float((v >= boom).mean() / max((bv >= boom).mean(), 1e-12)),
        "crash": float((v <= crash).mean() / max((bv <= crash).mean(), 1e-12)),
        "boom_rate": float((v >= boom).mean()), "crash_rate": float((v <= crash).mean()),
        "p10": float(np.percentile(v, 10)), "p90": float(np.percentile(v, 90)),
        # ---- 逐日等权口径(每个信号日一票):入选只数随行情变化时,池化口径会偏向入选多的日子 ----
        "mean_d": float(np.nanmean(daily["mean"])), "win_d": float(np.nanmean(daily["win"])),
        "boom_d": float(np.nanmean(daily["boom"])), "crash_d": float(np.nanmean(daily["crash"])),
        "base_boom_d": float(np.nanmean(daily["base_boom"])), "base_crash_d": float(np.nanmean(daily["base_crash"])),
        "d_boom": d_boom, "t_boom": t_boom, "d_crash": d_crash, "t_crash": t_crash,
    }
    for key, parts in more.items():
        arr = np.concatenate(parts)
        arr = arr[np.isfinite(arr)]
        out[key] = float(arr.mean()) if arr.size else np.nan
    return out


# ============================== 研究上下文 ==============================

@dataclass
class Research:
    panel: RPanel
    f: Dict[str, np.ndarray]
    y: Dict[str, np.ndarray]
    ctx: Dict[str, np.ndarray]

    def days(self, horizon: int, lookback: int = 60) -> np.ndarray:
        return signal_days(len(self.panel.dates), horizon, lookback)

    def tradable(self, horizon: int) -> np.ndarray:
        """可成交且目标有限的 (T,N) 掩码(入选与基准共用)。"""
        return self.f["eligible"] & self.y["buyable"] & np.isfinite(self.y[f"nxt{horizon}"])


def load_research(db: str = DESKTOP_DB, cache_dir: Optional[str] = DEFAULT_CACHE, refresh: bool = False) -> Research:
    panel = load_panel(db, cache_dir, refresh)
    f = build_features(panel)
    add_residual_flow(f)
    return Research(panel, f, build_targets(panel, f), market_context(panel, f))


FACTOR_SKIP = {"valid", "price", "eligible", "limit_up", "valid_days", "valid_days121"}


def factor_names(f: Dict[str, np.ndarray]) -> List[str]:
    return [k for k in f if k not in FACTOR_SKIP]


COHORTS: Dict[str, Tuple[str, Callable[[Dict[str, np.ndarray]], np.ndarray]]] = {
    "all": ("全部合格股", lambda f: np.ones(f["eligible"].shape, dtype=bool)),
    "pos35": ("60日位置≤0.35", lambda f: f["pos60"] <= 0.35),
    "pos20": ("60日位置≤0.20", lambda f: f["pos60"] <= 0.20),
    "dd20": ("距60日高点回撤≤-20%", lambda f: f["dd60"] <= -20.0),
    "pos120_30": ("120日位置≤0.30", lambda f: (f["pos120"] <= 0.30) & (f["valid_days121"] >= 110)),
}


# ============================== 阶段:数据质量 ==============================

def stage_data(rs: Research, db: str) -> None:
    p, f = rs.panel, rs.f
    print(f"面板 {p.dates[0]} ~ {p.dates[-1]} · {len(p.dates)} 日 × {len(p.codes)} 只")
    elig = f["eligible"]
    print(f"日均合格股 {elig[60:].sum(axis=1).mean():.0f} 只 · 当日 ST {p.st[-1].sum()} 只")
    # 与 market_daily 全市场日线交叉校验(只有 65 个交易日有全市场行)
    conn = _connect(db)
    full = [r[0] for r in conn.execute(
        "SELECT trade_date FROM market_daily GROUP BY trade_date HAVING COUNT(*)>=4000 ORDER BY trade_date")]
    col = {c: i for i, c in enumerate(p.codes)}
    row = {d.replace("-", ""): i for i, d in enumerate(p.dates)}
    d_pct: List[float] = []
    ratios: List[float] = []
    for d in full[::4]:
        t = row.get(d)
        if t is None:
            continue
        for ts_code, pct_chg, amount, pre_close, close in conn.execute(
                "SELECT ts_code, pct_chg, amount, pre_close, close FROM market_daily WHERE trade_date=?", (d,)):
            i = col.get(str(ts_code).split(".")[0])
            if i is None or not np.isfinite(p.amt[t, i]):
                continue
            real_pct = pct_chg if pct_chg is not None else (
                (close / pre_close - 1.0) * 100.0 if pre_close and close else None)
            if real_pct is not None:
                d_pct.append(p.pct[t, i] - real_pct)
            if amount:
                ratios.append(p.amt[t, i] / amount)
    if d_pct:
        d = np.array(d_pct)
        print(f"涨跌幅校验(对 market_daily,{len(full[::4])} 日抽样 {d.size} 行): "
              f"|差|中位 {np.median(np.abs(d)):.3f}pp · >0.1pp 占 {(np.abs(d) > 0.1).mean() * 100:.2f}%")
    if ratios:
        r = np.array(ratios)
        unit = np.median(r)
        print(f"成交额校验: 反推额/真实额 中位 {unit:.4g}(量纲比) · 相对量纲中位的偏差 "
              f"P5 {np.percentile(r / unit, 5):.3f} / P95 {np.percentile(r / unit, 95):.3f}")
    # 基础发生率:按 cohort 与持有期
    print("\n基础发生率(可成交口径,t+1 买入):")
    print(f"{'cohort':<22}{'持有':>4}{'样本':>9}{'日均':>7}{'均值%':>8}{'胜率':>7}{'大涨率':>8}{'大跌率':>8}"
          f"{'MFE≥30':>8}{'MAE≤-15':>9}")
    for name, (label, fn) in COHORTS.items():
        for n in HORIZONS:
            days = rs.days(n, 120 if "120" in name else 60)
            m = rs.tradable(n) & fn(f)
            y = rs.y[f"nxt{n}"][days][m[days]]
            mfe = rs.y[f"mfe{n}"][days][m[days]]
            mae = rs.y[f"mae{n}"][days][m[days]]
            print(f"{label:<22}{n:>4}{y.size:>9}{m[days].sum(axis=1).mean():>7.0f}{y.mean():>+8.2f}"
                  f"{(y > 0).mean() * 100:>6.1f}%{(y >= BOOM[n]).mean() * 100:>7.2f}%"
                  f"{(y <= CRASH[n]).mean() * 100:>7.2f}%{(mfe >= 30).mean() * 100:>7.2f}%"
                  f"{(mae <= -15).mean() * 100:>8.2f}%")
    # 市场环境时间线(每 20 个交易日一行)
    print("\n市场环境时间线(每20个交易日):")
    print(f"{'日期':<12}{'中证1000':>9}{'20日%':>8}{'国证2000 20日%':>15}{'MA20上方占比':>12}{'低位股占比':>10}"
          f"{'全市场20日后均值%':>16}")
    y20 = rs.y["nxt20"]
    for t in range(60, len(p.dates), 20):
        m = rs.tradable(20)[t]
        fwd = f"{y20[t, m].mean():+.2f}" if m.any() else "--"
        print(f"{p.dates[t]:<12}{p.index['csi1000'][t]:>9.0f}{rs.ctx['csi1000_ret20'][t]:>+8.1f}"
              f"{rs.ctx['gz2000_ret20'][t]:>+15.1f}{rs.ctx['breadth_ma20'][t] * 100:>11.0f}%"
              f"{rs.ctx['low_share'][t] * 100:>9.0f}%{fwd:>16}")


# ============================== 阶段:单因子扫描 ==============================

def scan_cohort(rs: Research, cohort: str, horizon: int, names: Optional[Iterable[str]] = None,
                lookback: int = 60) -> List[FactorStat]:
    f = rs.f
    mask = rs.tradable(horizon) & COHORTS[cohort][1](f)
    days = rs.days(horizon, lookback)
    target = rs.y[f"nxt{horizon}"]
    out = []
    for name in (names or factor_names(f)):
        stat = factor_stat(name, f[name], target, mask, days, horizon, BOOM[horizon], CRASH[horizon])
        if stat:
            out.append(stat)
    return out


def print_scan(stats: List[FactorStat], title: str, top: Optional[int] = None) -> None:
    stats = sorted(stats, key=lambda s: -abs(s.ic_t if np.isfinite(s.ic_t) else 0.0))
    print(f"\n-- {title} --")
    print(f"{'因子':<14}{'IC':>7}{'块t':>6}{'同号':>6}{'单调':>6}{'顶超额':>8}{'底超额':>8}"
          f"{'顶大涨':>7}{'顶大跌':>7}{'底大涨':>7}{'底大跌':>7}")
    for s in stats[:top]:
        print(f"{s.name:<14}{s.ic:>+7.3f}{s.ic_t:>+6.1f}{s.ic_same * 100:>5.0f}%{s.mono:>+6.2f}"
              f"{s.top_excess:>+8.2f}{s.bot_excess:>+8.2f}{s.top_boom:>7.2f}{s.top_crash:>7.2f}"
              f"{s.bot_boom:>7.2f}{s.bot_crash:>7.2f}")


def stage_scan(rs: Research, cohorts: Sequence[str], horizons: Sequence[int], top: Optional[int]) -> None:
    for cohort in cohorts:
        for n in horizons:
            lookback = 120 if "120" in cohort else 60
            stats = scan_cohort(rs, cohort, n, lookback=lookback)
            blocks = stats[0].blocks if stats else 0
            print_scan(stats, f"cohort[{cohort}] {COHORTS[cohort][0]} · 持有{n}日 · {blocks} 个独立块 "
                              f"· 大涨≥+{BOOM[n]:.0f}% 大跌≤{CRASH[n]:.0f}%", top)


# ============================== 阶段:多元回归 / 分桶剖面 ==============================

FM_BOTTOM = ("pos60", "runup60", "lu20", "vol60", "er20", "ln_amt60", "ln_price", "energy", "since_low60",
             "dip60", "frc60", "skew20")


def stage_fm(rs: Research, horizons: Sequence[int]) -> None:
    """底部 cohort 内的多元横截面回归:分离各因子对 超额 / 大涨概率 / 大跌概率 的独立贡献。"""
    f = rs.f
    volrk = rank_in(f["vol60"], f["eligible"], range(60, len(rs.panel.dates)))
    cohorts = (("底部(pos60≤0.35)", f["pos60"] <= 0.35),
               ("高弹性底部(pos60≤0.35 且 波动分位≥0.6)", (f["pos60"] <= 0.35) & (volrk >= 0.6)))
    for label, cohort in cohorts:
        for h in horizons:
            res, n_blocks = fama_macbeth(rs, cohort, FM_BOTTOM, h)
            print(f"\n== {label} · 持有{h}日 · {n_blocks} 个独立块 ==  "
                  "系数 = 因子从 cohort 内最低到最高时结果的变化(已控制同表其它因子)")
            print(f"{'因子':<13}|{'超额%':>7}{'t':>6}{'同号':>5} |{'大涨pp':>7}{'t':>6} |{'大跌pp':>7}{'t':>6} | 判读")
            for (name, e, et, es), (_, b, bt, _), (_, c, ct, _) in zip(res["excess"], res["boom"], res["crash"]):
                tag = ("正偏度" if b > 0 > c else "负偏度" if b < 0 < c else "双尾变厚" if b > 0 else "双尾变薄")
                print(f"{name:<13}|{e:>+7.2f}{et:>+6.1f}{es * 100:>4.0f}% |{b:>+7.2f}{bt:>+6.1f} |{c:>+7.2f}{ct:>+6.1f} | {tag}")
    if "ln_mv" in f:
        print(f"\n== 规模稳健性:加入流通市值(最近一期 {rs.panel.mv_date} × 复权价格比)后,逆向资金因子是否仍成立 ==")
        print(f"{'cohort / 持有期':<34}" + "".join(f"{n:>22}" for n in ("dip60", "frc60", "skew20", "ln_mv")))
        for label, cohort in cohorts:
            for h in horizons:
                res, _nb = fama_macbeth(rs, cohort & np.isfinite(f["ln_mv"]), FM_BOTTOM + ("ln_mv",), h)
                by = {name: (e, t, s) for name, e, t, s in res["excess"]}
                print(f"{label[:24] + ' ' + str(h) + '日':<34}" + "".join(
                    f"{by[n][0]:>+9.2f} (t{by[n][1]:>+5.1f},{by[n][2] * 100:>3.0f}%)" for n in ("dip60", "frc60", "skew20", "ln_mv")))


# ============================== 底部埋伏策略(研究口径,与 analysis/bull_stock_factors 一致) ==============================

# 参数的唯一出处是实盘模块 analysis/bull_stock_factors.py;这里只是用研究面板(向量化)复算同一套规则
POS_MAX, ENERGY_MAX, TOP_FRAC, SCORE_PARTS = bf.POS_MAX, bf.ENERGY_MAX, bf.TOP_FRAC, bf.SCORE_PARTS
TIERS: Tuple[Tuple[str, str, float, float], ...] = tuple(       # (key, 名称, 全市场波动分位下限, 上限)
    (key, label.split("·")[-1], lo, hi) for key, label, _risk, lo, hi in bf.TIERS)


def ambush(rs: Research, parts: Sequence[Tuple[str, int]] = SCORE_PARTS, pos_max: float = POS_MAX,
           energy_max: float = ENERGY_MAX, top: float = TOP_FRAC, no_limit_up: bool = True,
           tiers: Sequence[Tuple[str, str, float, float]] = TIERS) -> Dict[str, Dict[str, np.ndarray]]:
    """按档返回 {gate, score(档内分位 0~1), picks};全部只用 t 日及之前的数据。"""
    f = rs.f
    all_days = range(60, len(rs.panel.dates))
    volrk = rank_in(f["vol60"], f["eligible"], all_days)
    base = f["eligible"] & (f["pos60"] <= pos_max) & (f["energy"] < energy_max)
    if no_limit_up:
        base &= f["lu20"] == 0
    out: Dict[str, Dict[str, np.ndarray]] = {}
    for key, _label, lo, hi in tiers:
        gate = base & (volrk >= lo) & (volrk < hi)
        raw = np.mean(np.stack([rank_in(f[name] * sign, gate, all_days) for name, sign in parts]), axis=0)
        score = rank_in(raw, gate, all_days, min_n=10)
        out[key] = {"gate": gate, "score": score, "picks": score >= 1.0 - top}
    return out


def _fmt_stats(tag: str, st: Dict[str, float], peer: Optional[Dict[str, float]] = None) -> str:
    """全部为逐日等权口径:每个信号日一票,不因某些日子入选多而加权。"""
    if not st.get("n"):
        return f"{tag:<26}  样本不足"
    peer_txt = f"{peer['day_excess']:>+7.2f}{peer['t']:>+5.1f}{peer['same'] * 100:>4.0f}%" if peer and peer.get("n") else " " * 16
    return (f"{tag:<26}{st['per_day']:>5.0f}{st['mean_d']:>+7.2f}{st['day_excess']:>+7.2f}{st['t']:>+5.1f}{peer_txt}"
            f"{st['win_d'] * 100:>6.1f}%{st['boom_d'] * 100:>6.2f}%{st['d_boom']:>+7.2f}{st['t_boom']:>+5.1f}"
            f"{st['crash_d'] * 100:>6.2f}%{st['d_crash']:>+7.2f}{st['t_crash']:>+5.1f}")


_HEAD = (f"{'':<26}{'日均':>5}{'收益%':>7}{'超额%':>7}{'t':>5}{'对同类':>7}{'t':>5}{'同号':>5}{'胜率':>7}"
         f"{'大涨率':>7}{'Δpp':>7}{'t':>5}{'大跌率':>7}{'Δpp':>7}{'t':>5}")


def _eval(rs: Research, picks: np.ndarray, gate: Optional[np.ndarray], h: int) -> Tuple[Dict[str, float], Optional[Dict[str, float]]]:
    """入选相对全市场(及相对同门槛未排序同类)的表现;入选集合只由 t 日信息决定,这里再与可成交取交集。"""
    days, tr, target = rs.days(h), rs.tradable(h), rs.y[f"nxt{h}"]
    extra = {"mfe30": (rs.y[f"mfe{h}"] >= 30).astype(float), "mae15": (rs.y[f"mae{h}"] <= -15).astype(float)}
    st = pick_stats(target, picks & tr, tr, days, h, BOOM[h], CRASH[h], extra)
    peer = pick_stats(target, picks & tr, gate & tr, days, h, BOOM[h], CRASH[h]) if gate is not None else None
    return st, peer


def simulate_exits(rs: Research, picks: np.ndarray, h: int = 20) -> None:
    """出场规则对单笔收益分布的影响:t+1 收盘买入,最长持有 h 日,按收盘价触发(跌停卖不出未建模)。"""
    days, tr, price = rs.days(h), rs.tradable(h), rs.f["price"]
    sel = np.zeros(picks.shape, dtype=bool)
    sel[days] = (picks & tr)[days]
    tt, ii = np.nonzero(sel)
    path = np.stack([price[tt + 1 + k, ii] / price[tt + 1, ii] - 1.0 for k in range(1, h + 1)], axis=1) * 100.0

    def run(stop: Optional[float] = None, take: Optional[float] = None, trail: Optional[float] = None):
        out, hold = path[:, -1].copy(), np.full(path.shape[0], float(h))
        done, peak = np.zeros(path.shape[0], dtype=bool), np.zeros(path.shape[0])
        for k in range(h):
            r = path[:, k]
            peak = np.maximum(peak, r)
            hit = np.zeros(path.shape[0], dtype=bool)
            if stop is not None:
                hit |= r <= -stop
            if take is not None:
                hit |= r >= take
            if trail is not None:
                hit |= ((1 + r / 100) / (1 + peak / 100) - 1) * 100 <= -trail
            hit &= ~done
            out[hit], hold[hit] = r[hit], k + 1
            done |= hit
        return out, hold

    print(f"{'出场规则':<24}{'均值%':>7}{'中位%':>7}{'胜率':>7}{'≥+30%':>7}{'≤-15%':>7}{'P10':>7}{'P90':>7}{'平均持有日':>9}")
    for label, kw in (("持有满期(无出场)", {}), ("止损 -8%", {"stop": 8}), ("止损 -10%", {"stop": 10}),
                      ("止损 -12%", {"stop": 12}), ("止盈 +30%", {"take": 30}),
                      ("止损-10% 止盈+30%", {"stop": 10, "take": 30}), ("移动止损 15%", {"trail": 15})):
        o, hd = run(**kw)
        print(f"{label:<24}{o.mean():>+7.2f}{np.median(o):>+7.2f}{(o > 0).mean() * 100:>6.1f}%"
              f"{(o >= 30).mean() * 100:>6.2f}%{(o <= -15).mean() * 100:>6.2f}%"
              f"{np.percentile(o, 10):>+7.1f}{np.percentile(o, 90):>+7.1f}{hd.mean():>9.1f}")


def simulate_portfolio(rs: Research, picks: np.ndarray, h: int = 20, cost: float = 0.2) -> Dict[str, float]:
    """滚动分批组合:每个信号日的入选在 t+1 收盘等权买入、持有 h 日后卖出,同时存续 h 批,各批等权。

    返回区间累计收益(%)、最大回撤(%)、日波动(%)及扣费后累计(每批一进一出扣 ``cost`` %)。
    基准用同一套机制跑「全部可成交股票」,所以两条曲线的差就是选股的贡献。
    """
    days, tr, price = rs.days(h), rs.tradable(h), rs.f["price"]
    t_count = price.shape[0]
    total, active = np.zeros(t_count), np.zeros(t_count)
    for s_day in days:
        idx = np.nonzero(picks[s_day] & tr[s_day])[0]
        if not idx.size:
            continue
        value = (price[s_day + 1:s_day + 2 + h, idx] / price[s_day + 1, idx]).mean(axis=1)   # V_0..V_h
        daily = value[1:] / value[:-1] - 1.0
        total[s_day + 2:s_day + 2 + h] += daily
        active[s_day + 2:s_day + 2 + h] += 1.0
    span = slice(days[0] + 2, t_count)
    ret = np.where(active[span] > 0, total[span] / np.maximum(active[span], 1.0), 0.0)
    curve = np.cumprod(1.0 + ret)
    peak = np.maximum.accumulate(curve)
    net = np.cumprod(1.0 + ret - np.where(active[span] > 0, cost / 100.0 / h, 0.0))
    return {"cum": (curve[-1] - 1.0) * 100.0, "mdd": ((curve / peak).min() - 1.0) * 100.0,
            "vol": ret.std() * 100.0, "net": (net[-1] - 1.0) * 100.0, "days": float(ret.size),
            "up_days": float((ret > 0).mean() * 100.0)}


def stage_strategy(rs: Research) -> None:
    """底部埋伏最终方案的全套验证:三档 × 三持有期、逐块、市场状态、消融、top-K 对照、出场规则。"""
    f, p = rs.f, rs.panel
    spec = ambush(rs)
    print(f"方案: 合格股 ∧ 60日位置≤{POS_MAX} ∧ 20日无涨停 ∧ 5日/60日均额<{ENERGY_MAX} → 按全市场波动分位分档 → "
          f"档内按({' , '.join(('+' if s > 0 else '−') + n for n, s in SCORE_PARTS)})合成分取前{TOP_FRAC * 100:.0f}%")
    print("超额 = 入选当日等权 − 全市场合格股当日等权;对同类 = 入选 − 同门槛未排序股票(风格中性的选股能力);"
          "t = 不重叠持有期块的 t 值")
    for h in HORIZONS:
        days, tr = rs.days(h), rs.tradable(h)
        extra = {"mfe30": (rs.y[f"mfe{h}"] >= 30).astype(float), "mae15": (rs.y[f"mae{h}"] <= -15).astype(float)}
        print(f"\n=== 持有{h}日(t+1 收盘买入)· 大涨≥+{BOOM[h]:.0f}% 大跌≤{CRASH[h]:.0f}% ===\n{_HEAD}")
        print(_fmt_stats("全市场合格股(基准)", pick_stats(rs.y[f"nxt{h}"], tr, tr, days, h, BOOM[h], CRASH[h], extra)))
        for key, label, lo, hi in TIERS:
            print(_fmt_stats(f"{label}档 门槛内全部(未排序)", *(_eval(rs, spec[key]["gate"], None, h))))
            print(_fmt_stats(f"{label}档 入选(前{TOP_FRAC * 100:.0f}%)", *_eval(rs, spec[key]["picks"], spec[key]["gate"], h)))

    h = 20
    days, tr, target = rs.days(h), rs.tradable(h), rs.y["nxt20"]
    blocks = block_ids(days, h)
    print("\n=== 逐块(每 20 个信号日一块,持有 20 日):入选 − 全市场 / 入选 − 同类 ===")
    print(f"{'信号日区间':<26}{'市场均值%':>9}" + "".join(f"{label + '−市场':>10}{label + '−同类':>10}" for _k, label, *_ in TIERS)
          + f"{'全市场20日涨幅中位':>16}")
    for b in np.unique(blocks):
        bd = days[blocks == b]
        cells = ""
        for key, _label, *_ in TIERS:
            pk, gt = spec[key]["picks"] & tr, spec[key]["gate"] & tr
            ex_m = np.nanmean([target[t, pk[t]].mean() - target[t, tr[t]].mean() for t in bd if pk[t].any()])
            ex_p = np.nanmean([target[t, pk[t]].mean() - target[t, gt[t]].mean() for t in bd if pk[t].any()])
            cells += f"{ex_m:>+10.2f}{ex_p:>+10.2f}"
        mk = np.mean([target[t, tr[t]].mean() for t in bd])
        print(f"{p.dates[bd[0]]} ~ {p.dates[bd[-1]]:<13}{mk:>+9.2f}{cells}{rs.ctx['med_ret20'][bd[0]]:>+16.1f}")

    print("\n=== 档内分数五分位(持有20日;对同类 = 该分位 − 同门槛全部)—— 看分数是否单调、该取多大比例 ===")
    print(f"{'档 / 分位':<18}{'日均':>5}{'收益%':>7}{'对市场':>7}{'t':>5}{'对同类':>7}{'t':>5}{'同号':>5}{'胜率':>7}{'大涨率':>7}{'大跌率':>7}")
    for key, label, *_ in TIERS:
        for q in range(5):
            sel = (spec[key]["score"] > q / 5) & (spec[key]["score"] <= (q + 1) / 5)
            st, peer = _eval(rs, sel, spec[key]["gate"], h)
            print(f"{label + ' Q' + str(q + 1) + ('(分数最高)' if q == 4 else ''):<18}{st['per_day']:>5.0f}{st['mean_d']:>+7.2f}"
                  f"{st['day_excess']:>+7.2f}{st['t']:>+5.1f}{peer['day_excess']:>+7.2f}{peer['t']:>+5.1f}{peer['same'] * 100:>4.0f}%"
                  f"{st['win_d'] * 100:>6.1f}%{st['boom_d'] * 100:>6.2f}%{st['crash_d'] * 100:>6.2f}%")

    print("\n=== 滚动分批组合(每日入选 t+1 收盘等权买入,持有20日,同时存续20批)===")
    print(f"{'组合':<22}{'累计%':>8}{'扣费后%':>8}{'最大回撤%':>9}{'日波动%':>8}{'上涨日占比':>9}")
    bench = simulate_portfolio(rs, tr, h)
    print(f"{'全市场合格股等权(基准)':<22}{bench['cum']:>+8.1f}{bench['net']:>+8.1f}{bench['mdd']:>+9.1f}{bench['vol']:>8.2f}{bench['up_days']:>8.1f}%")
    for key, label, *_ in TIERS:
        for tag, sel in ((f"{label}档 门槛内全部", spec[key]["gate"]), (f"{label}档 入选", spec[key]["picks"])):
            r = simulate_portfolio(rs, sel, h)
            print(f"{tag:<22}{r['cum']:>+8.1f}{r['net']:>+8.1f}{r['mdd']:>+9.1f}{r['vol']:>8.2f}{r['up_days']:>8.1f}%")
    span = rs.days(h)
    print(f"区间 {p.dates[span[0] + 2]} ~ {p.dates[-1]}({len(p.dates) - span[0] - 2} 个交易日);扣费 = 每批一进一出 0.2%")

    print("\n=== 市场状态(信号日的全市场20日涨幅中位)× 三档入选,持有20日 ===")
    med = rs.ctx["med_ret20"]
    print(f"{'状态':<22}{'日数':>5}{'段数':>5}{'市场%':>8}" + "".join(f"{label + '%':>9}{'涨率':>7}{'跌率':>7}" for _k, label, *_ in TIERS))
    for label, lo, hi in (("普跌后(中位≤-5%)", -np.inf, -5.0), ("阴跌/震荡(-5%~0%)", -5.0, 0.0), ("偏强(中位>0%)", 0.0, np.inf)):
        m = (med[days] > lo) & (med[days] <= hi)
        segs = int(np.sum(m[1:] & ~m[:-1]) + (1 if m[0] else 0))
        cells = ""
        for key, *_ in TIERS:
            pk = spec[key]["picks"] & tr
            vals = np.concatenate([target[t, pk[t]] for t in days[m] if pk[t].any()] or [np.array([np.nan])])
            daily = np.nanmean([target[t, pk[t]].mean() for t in days[m] if pk[t].any()] or [np.nan])
            cells += f"{daily:>+9.2f}{(vals >= BOOM[h]).mean() * 100:>6.2f}%{(vals <= CRASH[h]).mean() * 100:>6.2f}%"
        mk = np.mean([target[t, tr[t]].mean() for t in days[m]]) if m.any() else np.nan
        print(f"{label:<22}{int(m.sum()):>5}{segs:>5}{mk:>+8.2f}{cells}")
    print("-- 「普跌后」逐段明细(每段是一次独立的市场事件;段数少,只能当参考,不能当规则)--")
    washed = med[days] <= -5.0
    starts = [i for i in range(days.size) if washed[i] and (i == 0 or not washed[i - 1])]
    for a in starts:
        b = a
        while b + 1 < days.size and washed[b + 1]:
            b += 1
        seg = days[a:b + 1]
        cells = ""
        for key, *_ in TIERS:
            pk = spec[key]["picks"] & tr
            cells += f"{np.nanmean([target[t, pk[t]].mean() for t in seg if pk[t].any()] or [np.nan]):>+9.2f}"
        mk = np.mean([target[t, tr[t]].mean() for t in seg])
        print(f"  {p.dates[seg[0]]} ~ {p.dates[seg[-1]]}  {seg.size:>3} 日  市场 {mk:>+7.2f}  稳健/均衡/进攻 {cells}")

    print(f"\n=== 消融与对照(均衡档,持有20日)===\n{_HEAD}")
    variants = (
        ("★ 最终方案", {}),
        ("去掉「20日无涨停」", {"no_limit_up": False}),
        ("去掉「不放量」", {"energy_max": np.inf}),
        ("位置≤0.25", {"pos_max": 0.25}),
        ("位置≤0.50", {"pos_max": 0.50}),
        ("仅 dip60", {"parts": (("dip60", 1),)}),
        ("仅 frc60", {"parts": (("frc60", -1),)}),
        ("再加 skew20", {"parts": (("dip60", 1), ("frc60", -1), ("skew20", -1))}),
        ("前 10%", {"top": 0.10}),
        ("前 30%", {"top": 0.30}),
        ("前 50%", {"top": 0.50}),
    )
    for label, kw in variants:
        s = ambush(rs, **kw)[TIERS[len(TIERS) // 2][0]]
        print(_fmt_stats(label, *_eval(rs, s["picks"], s["gate"], h)))
    bal_key = TIERS[len(TIERS) // 2][0]
    bal = spec[bal_key]
    for k in (10, 30):
        picks = top_k(bal["score"], bal["gate"], range(60, len(p.dates)), k)
        print(_fmt_stats(f"对照:档内分数最高 {k} 只", *_eval(rs, picks, bal["gate"], h)))

    print("\n=== 出场规则(均衡档入选)===")
    simulate_exits(rs, bal["picks"], h)
    last = len(p.dates) - 1
    print(f"\n最新交易日 {p.dates[last]}:全市场20日涨幅中位 {med[last]:+.1f}% · "
          + " · ".join(f"{label}档入选 {int(spec[key]['picks'][last].sum())} 只(门槛内 {int(spec[key]['gate'][last].sum())})"
                       for key, label, *_ in TIERS))


# ============================== 阶段:实盘代码路径一致性 ==============================

_LEAN_COLS = ("ts_code", "name", "pct_change", "close", "net_amount", "net_amount_rate",
              "buy_elg_amount", "buy_elg_amount_rate", "buy_lg_amount", "buy_lg_amount_rate",
              "buy_md_amount", "buy_md_amount_rate", "buy_sm_amount", "buy_sm_amount_rate")


def production_picks(db: str, rs: Research) -> Dict[str, np.ndarray]:
    """走实盘代码路径逐日滚动选股:库行 → build_day_slice → assemble_panel → repair_panel → compute_features。

    返回 {策略key: (T,N) bool},列顺序对齐研究面板。
    """
    conn = _connect(db)
    sql = f"SELECT {','.join(_LEAN_COLS)} FROM moneyflow_dc WHERE top_n=0 AND trade_date=?"
    slices = [(d, bf.build_day_slice([dict(zip(_LEAN_COLS, r)) for r in conn.execute(sql, (d,))]))
              for d in rs.panel.dates]
    panel = bf.assemble_panel(slices)
    bf.repair_panel(panel)
    col = np.array([rs.panel.codes.index(c) for c in panel.codes])
    out = {key: np.zeros(rs.panel.pct.shape, dtype=bool) for key in bf.STRATEGIES}
    for t in range(bf.LOOKBACK - 1, len(panel.dates)):
        f = bf.compute_features(panel, t)
        for key, strategy in bf.STRATEGIES.items():
            out[key][t, col] = strategy.mask(f) & f["eligible"]
    return out


def stage_parity(rs: Research, db: str) -> None:
    """实盘路径(逐日 61 行窗口、float64、行级四舍五入)与研究路径(整段向量化、float32)选出的股票是否一致。"""
    prod, spec = production_picks(db, rs), ambush(rs)
    days = np.arange(bf.LOOKBACK - 1, len(rs.panel.dates))
    print(f"{'策略':<16}{'实盘路径入选(股·日)':>14}{'研究路径':>10}{'交集':>10}{'一致率(交/并)':>12}{'最新一日 实盘/研究/交集':>22}")
    for key, label, _lo, _hi in TIERS:
        a, b = prod[key][days], spec[key]["picks"][days]
        inter, union = int((a & b).sum()), int((a | b).sum())
        last = f"{int(a[-1].sum())}/{int(b[-1].sum())}/{int((a[-1] & b[-1]).sum())}"
        print(f"{label:<16}{int(a.sum()):>14}{int(b.sum()):>10}{inter:>10}{inter / max(union, 1) * 100:>11.2f}%{last:>22}")
    print(f"\n实盘路径入选的回测(与 --stage strategy 的「入选」行对照):\n{_HEAD}")
    for h in HORIZONS:
        for key, label, _lo, _hi in TIERS:
            print(_fmt_stats(f"持有{h}日 {label}档", *_eval(rs, prod[key], spec[key]["gate"], h)))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DESKTOP_DB, help="SQLite 路径(只读);默认桌面软件实库")
    p.add_argument("--cache", default=DEFAULT_CACHE, help="面板缓存目录")
    p.add_argument("--refresh", action="store_true", help="忽略缓存重新读库")
    p.add_argument("--stage", default="strategy", choices=("data", "scan", "fm", "strategy", "parity"),
                   help="data 数据质量与基础发生率 / scan 单因子扫描 / fm 多元回归 / strategy 最终方案全套验证 / "
                        "parity 实盘代码路径与研究路径的选股一致性")
    p.add_argument("--cohorts", default="all,pos35")
    p.add_argument("--horizons", default="10,20")
    p.add_argument("--top", type=int, default=None, help="每张表只打印前 N 个因子")
    args = p.parse_args()
    if not os.path.exists(args.db):
        raise SystemExit(f"库不存在: {args.db}")
    t0 = time.time()
    rs = load_research(args.db, args.cache, args.refresh)
    print(f"[载入+因子 {time.time() - t0:.0f}s]", file=sys.stderr)
    if args.stage == "data":
        stage_data(rs, args.db)
    elif args.stage == "scan":
        stage_scan(rs, [c.strip() for c in args.cohorts.split(",") if c.strip()],
                   [int(h) for h in args.horizons.split(",")], args.top)
    elif args.stage == "fm":
        stage_fm(rs, [int(h) for h in args.horizons.split(",")])
    elif args.stage == "strategy":
        stage_strategy(rs)
    elif args.stage == "parity":
        stage_parity(rs, args.db)


if __name__ == "__main__":
    main()
