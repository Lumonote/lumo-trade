# -*- coding: utf-8 -*-
"""四大指数走势状态分级 + 大小盘风格轴(纯函数,零 I/O)。

指数层**只做状态描述与分级,不报拐点断言**(spec §2 决策 D2):218 个交易日里
真实趋势拐点仅 3-5 次,统计上无法证伪。拐点断言只在板块层给出
(见 ``analysis/sector_turning.py``)。

⚠️ 新浪日 K 对指数不返回 ``amount``(实测为 None),量能一律用 ``volume``。
"""
from __future__ import annotations

from statistics import mean
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from analysis.market_regime import trailing_changes

# (新浪 symbol, market_daily ts_code, 显示名)
DISPLAY_INDEXES: Tuple[Tuple[str, str, str], ...] = (
    ("sh000001", "000001.SH", "上证指数"),
    ("sz399001", "399001.SZ", "深证成指"),
    ("sz399006", "399006.SZ", "创业板指"),
    ("sh000688", "000688.SH", "科创50"),
)

# 风格轴(不展示为卡片,只用于判断大小盘谁占优)
STYLE_INDEXES: Tuple[Tuple[str, str, str], ...] = (
    ("sh000300", "000300.SH", "沪深300"),
    ("sh000852", "000852.SH", "中证1000"),
)

PULSE_LABELS: Dict[str, str] = {
    "strong": "强势",
    "mild_up": "温和上行",
    "range": "震荡",
    "pullback": "回调",
    "risk": "风险",
    "unknown": "数据不足",
}

STYLE_LABELS: Dict[str, str] = {
    "large": "大盘占优",
    "small": "小盘占优",
    "balanced": "风格均衡",
    "unknown": "数据不足",
}

STYLE_BAND = 2.0        # 20 日涨跌幅差异小于该值视为均衡(百分点)
_VOL_SHORT, _VOL_LONG = 5, 20
_PCT_WINDOW = 60


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _avg_tail(values: Sequence[float], n: int) -> Optional[float]:
    tail = [v for v in values[-n:] if v is not None]
    return mean(tail) if len(tail) == n else None


def index_metrics(bars: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """指数日 K 序列 → 指标字典。历史不足的项为 None,不抛。"""
    rows = sorted((dict(b) for b in bars or [] if b.get("day")),
                  key=lambda b: str(b["day"]))
    closes = [_num(b.get("close")) for b in rows]
    closes = [c for c in closes if c is not None]
    volumes = [_num(b.get("volume")) for b in rows]
    volumes = [v for v in volumes if v is not None]

    changes = trailing_changes(closes, horizons=(1, 5, 20))
    ma20 = _avg_tail(closes, 20)
    ma60 = _avg_tail(closes, 60)
    ma20_prev = _avg_tail(closes[:-5], 20) if len(closes) >= 25 else None
    slope = None
    if ma20 is not None and ma20_prev not in (None, 0):
        slope = (ma20 - ma20_prev) / ma20_prev * 100

    vol_short = _avg_tail(volumes, _VOL_SHORT)
    vol_long = _avg_tail(volumes, _VOL_LONG)
    vol_ratio = (vol_short / vol_long) if (vol_short and vol_long) else None

    window = closes[-_PCT_WINDOW:]
    percentile = None
    if len(window) >= 20:
        low, high = min(window), max(window)
        percentile = 0.5 if high == low else (window[-1] - low) / (high - low)

    return {
        "close": closes[-1] if closes else None,
        "chg_1d": changes.get("chg_1d"),
        "chg_5d": changes.get("chg_5d"),
        "chg_20d": changes.get("chg_20d"),
        "ma20": ma20,
        "ma60": ma60,
        "ma20_slope": None if slope is None else round(slope, 3),
        "above_ma20": None if (ma20 is None or not closes) else closes[-1] > ma20,
        "above_ma60": None if (ma60 is None or not closes) else closes[-1] > ma60,
        "vol_ratio": None if vol_ratio is None else round(vol_ratio, 3),
        "percentile_60d": None if percentile is None else round(percentile, 4),
        "bars": len(rows),
    }


def classify_pulse(metrics: Optional[Mapping[str, Any]]) -> str:
    """按20日累计涨跌与均线分级。仅描述当前状态,不含拐点断言。"""
    m = metrics or {}
    chg20 = _num(m.get("chg_20d"))
    if chg20 is None:
        return "unknown"
    above = m.get("above_ma20")
    slope = _num(m.get("ma20_slope")) or 0.0

    if chg20 <= -6:
        return "risk"
    if chg20 <= -2 or (above is False and slope < 0):
        return "pullback"
    if chg20 >= 5 and above is not False:
        return "strong"
    if chg20 >= 2 and above is not False:
        return "mild_up"
    return "range"


def style_axis(large_metrics: Optional[Mapping[str, Any]],
               small_metrics: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """沪深300 vs 中证1000 的 20 日相对强弱 → 风格轴。"""
    large = _num((large_metrics or {}).get("chg_20d"))
    small = _num((small_metrics or {}).get("chg_20d"))
    if large is None or small is None:
        return {"axis": "unknown", "label": STYLE_LABELS["unknown"],
                "spread": None, "large": large, "small": small}
    spread = small - large
    if abs(spread) < STYLE_BAND:
        axis = "balanced"
    else:
        axis = "small" if spread > 0 else "large"
    return {"axis": axis, "label": STYLE_LABELS[axis], "spread": round(spread, 2),
            "large": round(large, 2), "small": round(small, 2)}


def overlay_realtime(bars: Sequence[Mapping[str, Any]],
                     quote: Optional[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """把实时报价叠加到当日 bar。返回 (新序列, 是否叠加成功)。

    当日 bar 已存在 → 覆盖 close;不存在 → 追加一根只有 close 的 bar,
    **不伪造 open/high/low**(与 K 线实时化同口径)。入参不被修改。
    """
    rows = [dict(b) for b in bars or []]
    close = _num((quote or {}).get("close"))
    if close is None:
        return rows, False
    day = str((quote or {}).get("date") or "")[:10]
    if not day:
        return rows, False
    if rows and str(rows[-1].get("day") or "")[:10] == day:
        rows[-1]["close"] = close
        rows[-1]["realtime"] = True
    else:
        rows.append({"day": day, "open": None, "high": None, "low": None,
                     "close": close, "volume": None, "amount": None,
                     "realtime": True})
    return rows, True
