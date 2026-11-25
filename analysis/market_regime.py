# -*- coding: utf-8 -*-
"""市场风格环境评估 + 动态置信度阈值 + 指数基准(2026-07 回测调查产物)。

背景: 6~7月小盘/题材段崩盘(中证1000 单月-18%)时上证/沪深300被权重股撑平,
只盯沪深300的门控全程未触发;同时 Top10 样本 91% 落在 S 级(≥85分),分级失去
区分度。本模块提供三块纯函数能力,供机会挖掘与报告生成器复用:

1. 风格门控: ``trailing_changes``/``classify_index_level``/``classify_style_regime``
   —— 把中证1000/国证2000 纳入判定,任一风格指数走坏即降级(取最差)。
2. 动态分级阈值: ``compute_dynamic_tier_thresholds``/``resolve_tier``
   —— S/A/B 阈值 = max(静态下限, 近窗口 Top10 分数分位数),对抗评分通胀。
3. 指数基准: ``forward_returns_for_dates`` —— 与回测同口径(次日开盘买、
   第 N 个交易日收盘卖)的指数前瞻收益,用于报告超额归因。

IO 侧: 新浪指数日 K(OHLC)抓取 → SQLite ``market_daily`` 表持久化(SQLite-only
约定),抓取失败时回退读库,离线可用。
"""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 严重度从高到低;merge 取最差
_SEVERITY = ("risk_off", "caution", "neutral", "risk_on")

# 新浪 symbol → (market_daily ts_code, 显示名)
INDEX_SYMBOLS: Dict[str, Tuple[str, str]] = {
    "sh000001": ("000001.SH", "上证指数"),
    "sh000300": ("000300.SH", "沪深300"),
    "sh000852": ("000852.SH", "中证1000"),
    "sz399303": ("399303.SZ", "国证2000"),
    # 总览页「指数风向」展示用(spec 2026-08-21)。399001/399006 是深市指数代码,
    # 与个股 ts_code 不冲突;000688.SH 同理(沪市个股为 6 开头)。
    "sz399001": ("399001.SZ", "深证成指"),
    "sz399006": ("399006.SZ", "创业板指"),
    "sh000688": ("000688.SH", "科创50"),
}

DEFAULT_TIER_FLOORS = {"S": 85.0, "A": 78.0, "B": 70.0}
DEFAULT_TIER_PERCENTILES = {"S": 85.0, "A": 60.0, "B": 35.0}

_POSITION_ADVICE = {
    "risk_off": "轻仓或观望(建议总仓位≤2成),等待风格企稳再介入",
    "caution": "降低仓位(建议总仓位≤4成),只参与最高置信度标的",
    "neutral": "常规仓位(建议总仓位≤6成),严格执行止损",
    "risk_on": "可正常参与,注意单票集中度",
    "unknown": "指数数据不足,请自行判断市场环境后再决定仓位",
}


# ============================================================ 纯函数: 趋势
def _num(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def trailing_changes(closes: Sequence[Any], horizons: Sequence[int] = (5, 20)) -> Dict[str, Optional[float]]:
    """收盘价序列 → 近 N 日涨跌幅(%)。历史不足或基准价非正 → None。"""
    values = [v for v in (_num(c) for c in closes) if v is not None]
    result: Dict[str, Optional[float]] = {}
    for n in horizons:
        key = f"chg_{n}d"
        if len(values) < n + 1 or values[-(n + 1)] <= 0:
            result[key] = None
        else:
            result[key] = (values[-1] - values[-(n + 1)]) / values[-(n + 1)] * 100
    return result


def classify_index_level(metrics: Optional[Dict[str, Any]]) -> str:
    """单指数环境分级。规则与既有沪深300门控同源,补充 caution 层。"""
    chg5 = _num((metrics or {}).get("chg_5d"))
    chg20 = _num((metrics or {}).get("chg_20d"))
    if chg5 is None:
        return "unknown"
    if chg5 <= -3 or (chg5 <= -1 and chg20 is not None and chg20 <= -5):
        return "risk_off"
    if chg5 <= -1.5 or (chg20 is not None and chg20 <= -3):
        return "caution"
    if chg5 >= 3 and chg20 is not None and chg20 >= 3:
        return "risk_on"
    return "neutral"


def merge_regime_levels(levels: Iterable[str]) -> str:
    """多指数分级合并: 取最差;unknown 不拖累已知结论,全 unknown 才 unknown。"""
    known = [lv for lv in levels if lv in _SEVERITY]
    if not known:
        return "unknown"
    return _SEVERITY[min(_SEVERITY.index(lv) for lv in known)]


def classify_style_regime(metrics_by_index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """多指数(含小盘风格)环境判定。

    返回 {style_regime, levels: {指数名: level}, reasons: [触发说明]}。
    """
    levels: Dict[str, str] = {}
    reasons: List[str] = []
    for name, metrics in (metrics_by_index or {}).items():
        level = classify_index_level(metrics)
        levels[name] = level
        if level in ("risk_off", "caution"):
            chg5 = _num(metrics.get("chg_5d"))
            chg20 = _num(metrics.get("chg_20d"))
            chg5_txt = f"{chg5:+.1f}%" if chg5 is not None else "—"
            chg20_txt = f"{chg20:+.1f}%" if chg20 is not None else "—"
            reasons.append(f"{name} 近5日{chg5_txt}/近20日{chg20_txt} → {level}")
    style = merge_regime_levels(levels.values())
    return {"style_regime": style, "levels": levels, "reasons": reasons}


def position_advice(level: str) -> str:
    return _POSITION_ADVICE.get(level) or _POSITION_ADVICE["unknown"]


# ============================================================ 纯函数: 动态阈值
def _percentile(sorted_values: List[float], pct: float) -> float:
    """线性插值分位数(sorted_values 升序,pct ∈ [0,100])。"""
    if not sorted_values:
        raise ValueError("empty values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = max(0.0, min(100.0, pct)) / 100.0 * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def compute_dynamic_tier_thresholds(
    scores: Sequence[Any],
    floors: Optional[Dict[str, float]] = None,
    percentiles: Optional[Dict[str, float]] = None,
    min_samples: int = 60,
    ceiling_band: float = 10.0,
) -> Dict[str, Any]:
    """S/A/B 动态阈值 = clamp(滚动窗口分数分位数, 下限, 下限+ceiling_band)。

    评分通胀(样本 91% ≥85)时静态 85 分毫无区分度;按分位数抬升后 S 恢复
    「近窗口前 15%」语义。上限带宽防退化: v24 时代 52% 样本顶格 100 分,
    纯分位数会得 S=100,评分版本切换后当日推荐将被全判 C。
    样本不足(< min_samples)回退静态下限,dynamic=False。
    """
    floors = dict(floors or DEFAULT_TIER_FLOORS)
    percentiles = dict(percentiles or DEFAULT_TIER_PERCENTILES)
    values = sorted(v for v in (_num(s) for s in scores) if v is not None)
    result: Dict[str, Any] = {"sample_size": len(values)}
    if len(values) < max(1, min_samples):
        result.update({k: float(floors[k]) for k in ("S", "A", "B")})
        result["dynamic"] = False
        return result

    def _clamped(tier: str) -> float:
        raw = _percentile(values, percentiles[tier])
        return min(max(floors[tier], raw), floors[tier] + ceiling_band)

    s_thr = _clamped("S")
    a_thr = min(_clamped("A"), s_thr - 0.5)
    b_thr = min(_clamped("B"), a_thr - 0.5)
    result.update({
        "S": round(s_thr, 2),
        "A": round(max(a_thr, floors["A"]), 2),
        "B": round(max(b_thr, floors["B"]), 2),
        "dynamic": True,
    })
    return result


def resolve_tier(score: Any, thresholds: Dict[str, Any]) -> str:
    value = _num(score)
    if value is None:
        return "C"
    for tier in ("S", "A", "B"):
        thr = _num(thresholds.get(tier))
        if thr is not None and value >= thr:
            return tier
    return "C"


# ============================================================ 纯函数: 指数基准
def _parse_bars(bars: Iterable[Dict[str, Any]]) -> List[Tuple[str, float, float]]:
    parsed: List[Tuple[str, float, float]] = []
    for bar in bars or []:
        day = str(bar.get("day") or bar.get("date") or "")[:10]
        open_ = _num(bar.get("open"))
        close = _num(bar.get("close"))
        if day and open_ is not None and close is not None:
            parsed.append((day, open_, close))
    parsed.sort(key=lambda x: x[0])
    return parsed


def forward_returns_for_dates(
    bars: Iterable[Dict[str, Any]],
    report_dates: Iterable[Any],
    horizon: int = 5,
) -> Dict[str, float]:
    """与回测同口径的指数前瞻收益: 报告日次一交易日开盘买入,
    第 ``horizon`` 个交易日收盘卖出。前向数据不足或开盘价非正的日期不返回。
    """
    parsed = _parse_bars(bars)
    days = [p[0] for p in parsed]
    result: Dict[str, float] = {}
    for raw in report_dates or []:
        date = str(raw or "")[:10]
        if not date:
            continue
        entry_idx = next((i for i, d in enumerate(days) if d > date), None)
        if entry_idx is None or entry_idx + horizon - 1 >= len(parsed):
            continue
        entry_open = parsed[entry_idx][1]
        exit_close = parsed[entry_idx + horizon - 1][2]
        if entry_open <= 0:
            continue
        result[date] = (exit_close - entry_open) / entry_open * 100
    return result


# ============================================================ IO: 抓取与入库
_SINA_KLINE_URL = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   "CN_MarketData.getKLineData")
_SINA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://finance.sina.com.cn/",
}


def fetch_index_daily(symbol: str, datalen: int = 160, timeout: float = 8.0) -> List[Dict[str, Any]]:
    """新浪指数日 K(含 OHLC)。先绕系统代理直连,失败再走默认路由;全失败 → []。"""
    import requests

    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(int(datalen))}
    for trust_env in (False, True):
        try:
            with requests.Session() as session:
                session.trust_env = trust_env
                resp = session.get(_SINA_KLINE_URL, params=params, headers=_SINA_HEADERS,
                                   timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — 双路由逐个尝试
            logger.debug("fetch_index_daily(%s, trust_env=%s) 失败: %s", symbol, trust_env, exc)
            continue
        if isinstance(data, list) and data:
            bars = []
            for row in data:
                if isinstance(row, dict) and row.get("day"):
                    bars.append({
                        "day": str(row.get("day"))[:10],
                        "open": _num(row.get("open")),
                        "high": _num(row.get("high")),
                        "low": _num(row.get("low")),
                        "close": _num(row.get("close")),
                        "volume": _num(row.get("volume")),
                        "amount": _num(row.get("amount")),
                    })
            if bars:
                return bars
    return []


def persist_index_bars(symbol: str, bars: List[Dict[str, Any]]) -> int:
    """指数日 K 落库 market_daily(ts_code 走后缀命名,与个股不冲突)。"""
    mapping = INDEX_SYMBOLS.get(symbol)
    if not mapping or not bars:
        return 0
    try:
        import pandas as pd

        from data_store import market_snapshot_repo

        ts_code = mapping[0]
        rows = []
        prev_close: Optional[float] = None
        for bar in sorted(bars, key=lambda b: str(b.get("day") or "")):
            close = _num(bar.get("close"))
            change = pct = None
            if close is not None and prev_close not in (None, 0):
                change = close - prev_close
                pct = change / prev_close * 100
            rows.append({
                "ts_code": ts_code,
                "trade_date": str(bar.get("day") or "").replace("-", ""),
                "open": _num(bar.get("open")), "high": _num(bar.get("high")),
                "low": _num(bar.get("low")), "close": close,
                "pre_close": prev_close, "change": change, "pct_chg": pct,
                "vol": _num(bar.get("volume")), "amount": _num(bar.get("amount")),
            })
            if close is not None:
                prev_close = close
        return market_snapshot_repo.upsert_daily_df(pd.DataFrame(rows))
    except Exception as exc:  # noqa: BLE001 — 落库失败不阻塞主流程
        logger.debug("persist_index_bars(%s) 失败: %s", symbol, exc)
        return 0


def load_index_bars_from_db(symbol: str, limit: int = 160) -> List[Dict[str, Any]]:
    """从 market_daily 读指数日 K(抓取失败时的离线回退)。"""
    mapping = INDEX_SYMBOLS.get(symbol)
    if not mapping:
        return []
    try:
        from data_store.connection import get_conn

        rows = get_conn().execute(
            "SELECT trade_date, open, high, low, close, vol, amount FROM market_daily "
            "WHERE ts_code=? ORDER BY trade_date DESC LIMIT ?",
            (mapping[0], int(limit)),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — 无库上下文时静默
        logger.debug("load_index_bars_from_db(%s) 失败: %s", symbol, exc)
        return []
    bars = []
    for trade_date, open_, high, low, close, vol, amount in reversed(rows):
        raw = str(trade_date or "")
        day = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw[:10]
        bars.append({"day": day, "open": open_, "high": high, "low": low,
                     "close": close, "volume": vol, "amount": amount})
    return bars


def get_index_bars(symbols: Iterable[str], datalen: int = 160, *,
                   allow_current_fallback: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """批量取指数日 K: 新浪抓取 → 落库;收盘统计可排除未确认的今日缓存。"""
    result: Dict[str, List[Dict[str, Any]]] = {}
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    for symbol in symbols:
        bars = fetch_index_daily(symbol, datalen=datalen)
        if bars:
            persist_index_bars(symbol, bars)
        else:
            bars = load_index_bars_from_db(symbol, limit=datalen)
            if not allow_current_fallback:
                bars = [bar for bar in bars if str(bar.get("day") or "")[:10] < today]
        if bars:
            result[symbol] = bars
    return result


def assess_style_regime(datalen: int = 30) -> Dict[str, Any]:
    """风格环境一站式评估(沪深300 + 中证1000 + 国证2000)。

    返回 {style_regime, indices: {指数名: {chg_5d, chg_20d, level}}, reasons,
    position_advice};取数全失败 → style_regime='unknown'。
    """
    bars_by_symbol = get_index_bars(("sh000300", "sh000852", "sz399303"), datalen=datalen)
    metrics_by_name: Dict[str, Dict[str, Any]] = {}
    for symbol, bars in bars_by_symbol.items():
        name = INDEX_SYMBOLS[symbol][1]
        metrics_by_name[name] = trailing_changes([bar.get("close") for bar in bars])
    verdict = classify_style_regime(metrics_by_name)
    indices = {
        name: {
            "chg_5d": (None if metrics.get("chg_5d") is None else round(metrics["chg_5d"], 2)),
            "chg_20d": (None if metrics.get("chg_20d") is None else round(metrics["chg_20d"], 2)),
            "level": verdict["levels"].get(name, "unknown"),
        }
        for name, metrics in metrics_by_name.items()
    }
    return {
        "style_regime": verdict["style_regime"],
        "indices": indices,
        "reasons": verdict["reasons"],
        "position_advice": position_advice(verdict["style_regime"]),
    }
