"""牛股挖掘 · 底部埋伏 —— 条件选股「牛股挖掘」快速选择的唯一口径(实盘筛选与回测共用)。

要解决的问题:在股票还趴在底部、没有启动的时候把它挑出来,而不是等涨起来再追。

数据面:本地 ``moneyflow_dc`` 全市场日频面板(涨跌幅 / 收盘价 / 主力净额 / 由净额÷净占比反推的成交额),
零新抓取、全市场可算、可回测。没有开高低与换手,所以全部因子都是「收盘 + 成交额 + 主力资金」口径:

- 价格用涨跌幅链乘出的复权指数(不受除权影响),不直接比收盘价;收盘价只用于数据校验(repair_panel);
- 涨停按板块阈值(主板 9.8 / 创业·科创 19.8 / 北交所 29.8 / 主板 ST 4.8)以收盘涨幅判定;
- 成交额 = |分档净额| ÷ |分档净占比|,取净占比绝对值最大的一档以压低舍入误差。

规则(证据见 ``scripts/research_bottom_factors.py``,只读桌面库、可成交口径、不重叠持有期块检验):

1. 底部门槛:60 日区间位置 ≤ POS_MAX、近 20 日无涨停、5 日/60 日均额 < ENERGY_MAX(没放量)。
   底部放量与近期涨停在回测里都只抬高大跌概率,所以「还没动」是硬条件。
2. 风险档:按全市场 60 日波动分位切成 稳健 / 均衡 / 进攻。波动是风险旋钮而不是收益来源 ——
   波动越大的底部股,大涨和大跌的比例一起升,且大跌升得更快;所以分档如实标注,不加分。
3. 排序:逆势承接分 = 「下跌日主力净占比」与「主力净占比和涨跌幅的相关性(越低越好)」两项的档内分位均值,
   取档内前 TOP_FRAC。主力在下跌日买、不追涨的股票,之后相对同档其它底部股有稳定超额;
   这是全部候选因子里唯一在各时段、各持有期、控制规模/价位/波动后都同号的一类。

被检验后放弃的:位置深度、距新低天数、超跌反弹、放量突破、连板/首板、路径效率、波动收缩、市场状态择时
(要么只是行情风格的函数,要么只抬高尾部不改变方向)。规则只做筛选与排序,不改机会评分,不构成投资建议。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

LOOKBACK = 61          # 因子窗口行数:60 个日收益 + 1 行基准
MIN_VALID_DAYS = 55    # 窗口内有效交易日下限(剔次新 / 长期停牌)
MIN_AMOUNT_WAN = 3000  # 5日均成交额下限(万元):流动性地板,剔僵尸股

# ---- 底部埋伏参数(改动必须重跑 scripts/research_bottom_factors.py --stage strategy) ----
POS_MAX = 0.35         # 60日区间位置上限(0 = 60日最低,1 = 60日最高)
ENERGY_MAX = 1.0       # 5日均额 / 60日均额 上限:没有放量
TOP_FRAC = 0.20        # 档内按逆势承接分取前 20%
FLOW_WINDOW = 60       # 资金因子窗口(交易日)
MIN_SIDE_DAYS = 12     # 窗口内上涨日、下跌日各自的下限:少于它「下跌日净占比」没有意义
MIN_RANK_N = 50        # 截面排名的最小样本数;不足则该档当日不出结果
# (key, 名称, 风险档, 全市场波动分位下限, 上限)
TIERS: Tuple[Tuple[str, str, str, float, float], ...] = (
    ("ambush_steady", "底部埋伏·稳健", "low", 0.0, 0.5),
    ("ambush_balanced", "底部埋伏·均衡", "mid", 0.5, 0.8),
    ("ambush_aggressive", "底部埋伏·进攻", "high", 0.8, 1.01),
)
# 逆势承接分的构成:(因子, 方向);方向 +1 = 越大越好
SCORE_PARTS: Tuple[Tuple[str, int], ...] = (("dip60", 1), ("frc60", -1))


# ----------------------------- 日切片 / 面板 -----------------------------

def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _code6(ts_code: Any) -> str:
    s = str(ts_code or "").split(".")[0].strip()
    return s if len(s) == 6 and s.isdigit() else ""


def is_st_name(name: Any) -> bool:
    text = str(name or "")
    return "ST" in text.upper() or "退" in text


def _row_amount_wan(row: Dict[str, Any]) -> Optional[float]:
    """由 净额÷净占比 反推当日成交额(万元);取净占比绝对值最大的一档。无成交 → None。"""
    best_rate, best_amt = 0.0, None
    for amount_key, rate_key in (("net_amount", "net_amount_rate"),
                                 ("buy_elg_amount", "buy_elg_amount_rate"),
                                 ("buy_lg_amount", "buy_lg_amount_rate"),
                                 ("buy_md_amount", "buy_md_amount_rate"),
                                 ("buy_sm_amount", "buy_sm_amount_rate")):
        amount, rate = _f(row.get(amount_key)), _f(row.get(rate_key))
        if amount is None or rate is None or abs(rate) < 0.05:
            continue
        if abs(rate) > best_rate:
            best_rate, best_amt = abs(rate), abs(amount) / abs(rate) * 100.0
    return best_amt


def build_day_slice(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """一个交易日的 moneyflow 行 → 紧凑切片 {codes,names,st,pct,close,amt,net}(缺值为 None)。"""
    codes: List[str] = []
    names: List[str] = []
    st: List[bool] = []
    pct: List[Optional[float]] = []
    close: List[Optional[float]] = []
    amt: List[Optional[float]] = []
    net: List[Optional[float]] = []
    seen = set()
    for row in rows or []:
        code = _code6(row.get("ts_code") or row.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        amount = _row_amount_wan(row)
        change = _f(row.get("pct_change"))
        price = _f(row.get("close"))
        name = str(row.get("name") or "")
        codes.append(code)
        names.append(name)
        st.append(is_st_name(name))
        pct.append(round(change, 2) if change is not None else None)
        close.append(price if price is not None and price > 0 else None)
        amt.append(round(amount, 0) if amount is not None else None)
        flow = _f(row.get("net_amount"))
        net.append(round(flow, 1) if flow is not None else None)
    return {"codes": codes, "names": names, "st": st, "pct": pct, "close": close, "amt": amt, "net": net}


@dataclass
class Panel:
    dates: List[str]      # 升序交易日
    codes: List[str]
    names: List[str]      # 最新名称(展示用)
    pct: np.ndarray       # (T, N) 日涨跌幅 %,缺失 NaN
    amt: np.ndarray       # (T, N) 成交额 万元,缺失 NaN
    net: np.ndarray       # (T, N) 主力净额 万元,缺失 NaN
    close: Optional[np.ndarray] = None    # (T, N) 未复权收盘价,只用于 repair_panel 的前收盘校验
    st: Optional[np.ndarray] = None       # (T, N) bool 当日名称带 ST/退;None = 按最新名称
    traded: Optional[np.ndarray] = None   # (T, N) bool 有效交易日;None = 「有涨跌幅且有成交额」


def assemble_panel(slices: List[Tuple[str, Dict[str, Any]]]) -> Optional[Panel]:
    """[(date, slice)](任意顺序) → 对齐面板;代码取全部切片并集,名称取最新一次出现。"""
    slices = sorted((s for s in slices or [] if s and s[1] and s[1].get("codes")),
                    key=lambda s: str(s[0]))
    if not slices:
        return None
    index: Dict[str, int] = {}
    names: List[str] = []
    for _date, sl in slices:
        for code, name in zip(sl["codes"], sl.get("names") or [""] * len(sl["codes"])):
            pos = index.get(code)
            if pos is None:
                index[code] = len(names)
                names.append(name or "")
            elif name:
                names[pos] = name
    t_count, n_count = len(slices), len(index)
    pct = np.full((t_count, n_count), np.nan)
    amt = np.full((t_count, n_count), np.nan)
    net = np.full((t_count, n_count), np.nan)
    close = np.full((t_count, n_count), np.nan)
    st = np.zeros((t_count, n_count), dtype=bool)
    for t, (_date, sl) in enumerate(slices):
        cols = np.fromiter((index[c] for c in sl["codes"]), dtype=np.int64, count=len(sl["codes"]))
        for target, key in ((pct, "pct"), (amt, "amt"), (net, "net"), (close, "close")):
            values = sl.get(key)
            if values is None:      # 旧切片没有这一列:留空
                continue
            target[t, cols] = np.array([np.nan if v is None else v for v in values], dtype=float)
        flags = sl.get("st")
        if flags is None:
            flags = [is_st_name(n) for n in sl.get("names") or [""] * len(sl["codes"])]
        st[t, cols] = np.array(flags, dtype=bool)
    return Panel(dates=[str(d) for d, _ in slices], codes=list(index), names=names,
                 pct=pct, amt=amt, net=net, close=close, st=st)


# ----------------------------- 数据缺陷修复 -----------------------------

STALE_DAY_SHARE = 0.20     # 当日收盘与「次日隐含前收盘」对不上的股票占比超过它 → 当日是未更新完的盘中快照
_PRECLOSE_TOL = 0.005      # 对不上的判据:相对偏差 > 0.5%(除权日个别股票会超,但全市场占比远低于阈值)
_MIN_COMPARABLE = 50       # 可比股票太少不判(小面板 / 单测)
INCOMPLETE_DAY_RATIO = 0.98  # 最新一日行数不足前一日的 98% → 数据未到齐


def repair_panel(panel: Optional[Panel], flow_fields: Tuple[str, ...] = ("amt", "net")) -> Dict[str, Any]:
    """就地修复面板里的两类数据缺陷(只改内存面板,不动原表),返回 {stale_days:[日期], gap_fills:int}。

    依据:每行自带「收盘价 + 涨跌幅」,所以次日那一行隐含了当日的真实收盘(次日收盘 ÷ (1+次日涨跌幅))。

    - 盘中快照日:数据源收盘后未更新完就被抓取入库(行不全、涨跌幅与成交额都是盘中值),此后不再重抓。
      判据 = 当日大面积股票的收盘价与次日隐含的前收盘对不上。修复 = 当日涨跌幅按隐含收盘重算;
      当日成交额与资金只有半天,作废为缺失(``flow_fields``)。
    - 单股缺行:某只股票前后都有行、中间缺行 → 用隐含收盘补出缺口期间的累计涨跌幅(记在缺口最后一日),
      否则复权价格指数会漏掉这段涨跌。幅度超过涨跌停上限 × 缺口天数的视为除权 / 重组复牌,不补。

    最新一日没有「次日」,无法校验 —— 用 :func:`latest_day_coverage` 给出提示。面板没有收盘价时什么都不做。
    """
    out: Dict[str, Any] = {"stale_days": [], "gap_fills": 0}
    if panel is None or panel.close is None or len(panel.dates) < 2:
        return out
    pct, close = panel.pct, panel.close
    t_count, n_count = pct.shape
    has = np.isfinite(pct) & np.isfinite(close) & (close > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        pre = np.where(has, close / (1.0 + pct / 100.0), np.nan)       # 每行隐含的前收盘
    traded = np.isfinite(pct) & np.isfinite(panel.amt)
    for t in range(t_count - 1):
        both = has[t] & has[t + 1]
        if int(both.sum()) < _MIN_COMPARABLE:
            continue
        off = np.abs(pre[t + 1, both] / close[t, both] - 1.0) > _PRECLOSE_TOL
        if float(off.mean()) <= STALE_DAY_SHARE:
            continue
        fix = both & np.isfinite(pre[t + 1]) & (pre[t + 1] > 0)
        pct[t, fix] = ((1.0 + pct[t, fix] / 100.0) * pre[t + 1, fix] / close[t, fix] - 1.0) * 100.0
        close[t, fix] = pre[t + 1, fix]
        for name in flow_fields:
            getattr(panel, name)[t] = np.nan
        traded[t] = np.isfinite(pct[t])
        has[t] = np.isfinite(pct[t]) & np.isfinite(close[t]) & (close[t] > 0)
        out["stale_days"].append(panel.dates[t])
    cap = limit_thresholds(panel.codes, panel.names) + 0.5
    last_close = np.full(n_count, np.nan)
    last_t = np.full(n_count, -1)
    for t in range(t_count):
        row = has[t]
        gap = row & (last_t >= 0) & (last_t < t - 1)
        if gap.any():
            idx = np.nonzero(gap)[0]
            move = (pre[t, idx] / last_close[idx] - 1.0) * 100.0
            ok = np.isfinite(move) & (np.abs(move) > 1e-6) & (np.abs(move) <= cap[idx] * (t - 1 - last_t[idx]))
            idx, move = idx[ok], move[ok]
            pct[t - 1, idx], close[t - 1, idx], traded[t - 1, idx] = move, pre[t, idx], True
            out["gap_fills"] += int(idx.size)
        last_close[row] = close[t, row]
        last_t[row] = t
    panel.traded = traded
    return out


def latest_day_coverage(panel: Optional[Panel]) -> Optional[float]:
    """最新一日行数 ÷ 前一日行数;明显小于 1(见 INCOMPLETE_DAY_RATIO)说明最新一日数据还没到齐。"""
    if panel is None or len(panel.dates) < 2:
        return None
    previous = int(np.isfinite(panel.pct[-2]).sum())
    return float(np.isfinite(panel.pct[-1]).sum()) / previous if previous else None


# ----------------------------- 因子 -----------------------------

def _board_limits(codes: List[str]) -> Tuple[np.ndarray, np.ndarray]:
    """(逐股非 ST 涨停阈值 %, 是否主板)。"""
    base = np.empty(len(codes))
    main = np.zeros(len(codes), dtype=bool)
    for i, code in enumerate(codes):
        if code[:1] in ("4", "8", "9"):
            base[i] = 29.8
        elif code.startswith(("30", "688", "689")):
            base[i] = 19.8
        else:
            base[i], main[i] = 9.8, True
    return base, main


def limit_thresholds(codes: List[str], names: List[str]) -> np.ndarray:
    """逐股涨停阈值(%):北交所 29.8 / 创业·科创 19.8 / 主板 9.8,主板 ST 4.8。"""
    base, main = _board_limits(codes)
    st = np.array([is_st_name(n) for n in names], dtype=bool)
    return np.where(main & st, 4.8, base)


def _div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    out = np.asarray(out, dtype=float)
    out[~np.isfinite(out)] = np.nan
    return out


def _rankdata(x: np.ndarray) -> np.ndarray:
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


def rank01(values: np.ndarray, mask: np.ndarray, min_n: int = MIN_RANK_N) -> np.ndarray:
    """``mask`` 内的截面分位 (0,1);mask 外 / 缺值 / 样本不足 ``min_n`` → NaN。"""
    out = np.full(values.shape, np.nan)
    ok = mask & np.isfinite(values)
    n = int(ok.sum())
    if n >= min_n:
        out[ok] = (_rankdata(values[ok].astype(float)) - 0.5) / n
    return out


def score_ambush(f: Dict[str, np.ndarray]) -> None:
    """截面步骤:波动分位 → 风险档 → 档内逆势承接分。写入 vol_rank / tier / ambush_score。

    tier = TIERS 的下标(不在任何档的门槛内为 -1);ambush_score = 档内门槛股票里的分位 (0,1)。
    """
    eligible = f["eligible"]
    vol_rank = rank01(f["vol60"], eligible)
    with np.errstate(invalid="ignore"):
        base = eligible & (f["pos60"] <= POS_MAX) & (f["lu20"] == 0) & (f["energy"] < ENERGY_MAX)
    tier = np.full(eligible.shape, -1, dtype=int)
    score = np.full(eligible.shape, np.nan)
    for k, (_key, _label, _risk, lo, hi) in enumerate(TIERS):
        with np.errstate(invalid="ignore"):
            gate = base & (vol_rank >= lo) & (vol_rank < hi)
        raw = np.mean(np.stack([rank01(f[name] * sign, gate) for name, sign in SCORE_PARTS]), axis=0)
        score[gate] = rank01(raw, gate, min_n=10)[gate]
        tier[gate] = k
    f["vol_rank"], f["tier"], f["ambush_score"] = vol_rank, tier, score


def compute_features(panel: Panel, t: Optional[int] = None) -> Optional[Dict[str, np.ndarray]]:
    """截至第 ``t`` 行(默认最后一行)的全市场因子向量;历史不足 LOOKBACK 行 → None。

    只使用 ``[t-60, t]`` 窗口内的数据,回测按日滚动调用即无未来函数(ST 状态也取当日名称)。
    """
    t = len(panel.dates) - 1 if t is None else int(t)
    if t < LOOKBACK - 1 or t >= len(panel.dates):
        return None
    win = slice(t - LOOKBACK + 1, t + 1)
    ret = panel.pct[win]            # (61, N)
    amt = panel.amt[win]
    net = panel.net[win]
    # 有成交才算有效交易日:停牌/退市行在表里是整行 0(涨跌幅 0.0 而非空),不能当成「平盘」;
    # repair_panel 修过的面板自带 traded(补回的缺行 / 盘中快照日没有成交额,但确实交易过)
    valid = panel.traded[win] if panel.traded is not None else np.isfinite(ret) & np.isfinite(amt)
    r0 = np.where(valid, ret, 0.0)
    price = np.vstack([np.ones((1, r0.shape[1])), np.cumprod(1.0 + r0[1:] / 100.0, axis=0)])
    last = price[-1]
    f: Dict[str, np.ndarray] = {}

    def trailing_return(days: int) -> np.ndarray:
        return (last / price[-days - 1] - 1.0) * 100.0

    # ---- 位置 ----
    f["ret5"], f["ret20"], f["ret60"] = trailing_return(5), trailing_return(20), trailing_return(60)
    hi60, lo60 = price[-60:].max(0), price[-60:].min(0)
    f["pos60"] = _div(last - lo60, hi60 - lo60)                 # 60日区间位置 0~1
    f["dd60"] = (last / hi60 - 1.0) * 100.0                     # 距60日高点回撤(≤0)

    # ---- 涨停(当日 ST 状态决定主板阈值) ----
    base, main = _board_limits(panel.codes)
    if panel.st is not None:
        st = panel.st[win]
    else:
        st = np.broadcast_to(np.array([is_st_name(n) for n in panel.names], dtype=bool), ret.shape)
    limits = np.where(st & main[None, :], 4.8, base[None, :])
    limit_up = valid & (ret >= limits)
    f["lu20"] = limit_up[-20:].sum(0)

    # ---- 波动 / 量能 ----
    w = FLOW_WINDOW
    v, r = valid[-w:], r0[-w:]
    cnt = v.sum(0).astype(float)
    mean_r = _div(r.sum(0), cnt)
    f["vol60"] = np.sqrt(np.clip(_div((r * r).sum(0), cnt) - mean_r * mean_r, 0.0, None))   # 有效交易日的日波动 %
    a0 = np.where(np.isfinite(amt), amt, 0.0)
    have = np.isfinite(amt)
    amt5 = _div(a0[-5:].sum(0), have[-5:].sum(0).astype(float))
    f["amt5"] = amt5
    f["energy"] = _div(amt5, _div(a0[-w:].sum(0), have[-w:].sum(0).astype(float)))          # 5日/60日均额

    # ---- 逆势资金:下跌日谁在接、资金是否追涨 ----
    a, flow = a0[-w:], np.where(np.isfinite(net), net, 0.0)[-w:]
    is_dn, is_up = v & (r < 0), v & (r > 0)
    enough = (is_dn.sum(0) >= MIN_SIDE_DAYS) & (is_up.sum(0) >= MIN_SIDE_DAYS)
    f["dip60"] = np.where(enough, _div((flow * is_dn).sum(0), (a * is_dn).sum(0)) * 100.0, np.nan)   # 下跌日主力净占比 %
    rate = np.where(v & (a > 0), flow / np.where(a > 0, a, 1.0) * 100.0, 0.0)                     # 日度主力净占比
    mean_x = _div(rate.sum(0), cnt)
    cov = _div((rate * r).sum(0), cnt) - mean_x * mean_r
    var_x = _div((rate * rate).sum(0), cnt) - mean_x * mean_x
    var_y = _div((r * r).sum(0), cnt) - mean_r * mean_r
    corr = _div(cov, np.sqrt(np.clip(var_x * var_y, 0.0, None)))
    f["frc60"] = np.where(cnt >= w * 0.9, corr, np.nan)          # 主力净占比与涨跌幅的相关:越低越不追涨

    # ---- 准入 ----
    f["valid_days"] = valid.sum(0)
    f["is_st"] = st[-1].copy()
    f["eligible"] = ((f["valid_days"] >= MIN_VALID_DAYS) & valid[-1]
                     & (np.nan_to_num(amt5) >= MIN_AMOUNT_WAN) & ~f["is_st"])
    score_ambush(f)
    return f


# ----------------------------- 策略 -----------------------------

@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    summary: str      # 一句话口径,前端 title 展示(不含回测数字)
    risk: str         # low / mid / high —— 即波动档
    rank: str         # 命中后默认排序用的因子(降序)
    mask: Callable[[Dict[str, np.ndarray]], np.ndarray]
    detail: Callable[[Dict[str, float]], str]


_GATE_TEXT = "低位未启动(60日区间下部、近20日无涨停、近5日未放量)"
_SCORE_TEXT = "档内按逆势承接分(主力在下跌日净买入、资金不追涨)取前两成"
_VOL_TEXT = {"low": "波动处于全市场较低的一半", "mid": "波动处于全市场中上(50%~80%分位)",
             "high": "波动处于全市场最高的两成"}


def _strategy(index: int, key: str, label: str, risk: str) -> Strategy:
    def mask(f: Dict[str, np.ndarray]) -> np.ndarray:
        with np.errstate(invalid="ignore"):
            return (f["tier"] == index) & (f["ambush_score"] >= 1.0 - TOP_FRAC)

    def detail(m: Dict[str, float]) -> str:
        return f"{label}·位置{m['pos60'] * 100:.0f}%·承接分位{m['ambush_score'] * 100:.0f}%"

    return Strategy(key, label, f"{_GATE_TEXT},{_VOL_TEXT[risk]};{_SCORE_TEXT}", risk, "ambush_score", mask, detail)


_STRATEGY_LIST = tuple(_strategy(i, key, label, risk) for i, (key, label, risk, _lo, _hi) in enumerate(TIERS))
STRATEGIES: Dict[str, Strategy] = {s.key: s for s in _STRATEGY_LIST}

# 随选股结果下发的因子列(全市场都有,不依赖逐股日K)
METRIC_KEYS = ("ret5", "ret20", "ret60", "pos60", "dd60", "vol60", "vol_rank", "energy", "lu20",
               "dip60", "frc60", "ambush_score")


def catalog() -> List[Dict[str, str]]:
    """策略清单(key/label/summary/risk),供接口与模板一致性校验。"""
    return [{"key": s.key, "label": s.label, "summary": s.summary, "risk": s.risk}
            for s in _STRATEGY_LIST]


def evaluate(panel: Optional[Panel]) -> Dict[str, Any]:
    """最新交易日的全市场评估:{date, days, stocks:{code:{metrics…, tags:[key…], hits:[文案…]}}}。

    历史不足或面板为空 → stocks 为空并带 reason。不合格股票(ST/次新/停牌/流动性不足)
    仍下发因子列,但不会命中任何策略(``eligible`` False)。评估前先做 :func:`repair_panel`,
    修复过的交易日与最新一日的数据到齐程度随结果返回(repaired_days / coverage / incomplete)。
    """
    if panel is None or not panel.dates:
        return {"date": "", "days": 0, "stocks": {}, "reason": "本地资金流全市场数据为空"}
    out: Dict[str, Any] = {"date": panel.dates[-1], "days": len(panel.dates), "stocks": {}}
    out["repaired_days"] = repair_panel(panel)["stale_days"]
    out["coverage"] = latest_day_coverage(panel)
    out["incomplete"] = out["coverage"] is not None and out["coverage"] < INCOMPLETE_DAY_RATIO
    f = compute_features(panel)
    if f is None:
        out["reason"] = f"本地资金流历史仅 {len(panel.dates)} 个交易日,牛股挖掘需要至少 {LOOKBACK} 个"
        return out
    eligible = f["eligible"]
    masks = {key: s.mask(f) & eligible for key, s in STRATEGIES.items()}
    has_data = np.isfinite(panel.pct[-1]) & np.isfinite(panel.amt[-1])   # 最新日有成交
    stocks: Dict[str, Dict[str, Any]] = {}
    for i, code in enumerate(panel.codes):
        if not has_data[i]:
            continue
        metrics: Dict[str, Any] = {}
        for key in METRIC_KEYS:
            value = float(f[key][i])
            metrics[key] = round(value, 4 if key in ("ambush_score", "vol_rank", "pos60") else 2) \
                if np.isfinite(value) else None
        tags = [key for key, mask in masks.items() if mask[i]]
        metrics["eligible"] = bool(eligible[i])
        metrics["tags"] = tags
        metrics["hits"] = [STRATEGIES[key].detail(metrics) for key in tags]
        stocks[code] = metrics
    out["stocks"] = stocks
    out["counts"] = {key: int(mask.sum()) for key, mask in masks.items()}
    return out
