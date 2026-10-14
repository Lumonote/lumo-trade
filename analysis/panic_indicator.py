"""统一恐慌强度：真实涨跌、20期回撤、上涨广度与放量卖压，纯计算、零 I/O。

自定义描述指标，并非期权隐含波动率 VIX。缺失因子不补零，按有效权重归一化。
单日涨跌的满刻度分别为指数 3%、板块 5%、个股 10%；回撤满刻度为其三倍。
"""
from __future__ import annotations

import math
from statistics import mean

METHOD = "sell_pressure_v1"
DESCRIPTION = "自定义恐慌强度（0–100，越高卖压越强）：涨跌40%、20期回撤30%、上涨广度20%、放量卖压10%；缺失项按有效权重归一化。"
SCALES = {"market": 3.0, "index": 3.0, "sector": 5.0, "stock": 10.0}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clip(value):
    return max(0.0, min(100.0, value))


def panic_indicator(*, change_pct=None, drawdown_pct=None, breadth=None,
                    volume_ratio=None, scope="stock", as_of=None,
                    breadth_label="上涨广度", provisional=False, stale=False):
    scale = SCALES.get(scope, 10.0)
    factors = []

    def add(key, label, value, score, weight):
        factors.append({"key": key, "label": label, "value": round(value, 4),
                        "score": round(_clip(score), 1), "weight": weight})

    change = number(change_pct)
    if change is not None and change >= -100:
        add("change", "当期涨跌幅%", change, 50-change/scale*50, .4)
    dd = number(drawdown_pct)
    if dd is not None and 0 <= dd <= 100:
        add("drawdown", "距20期收盘高点回撤%", dd, dd/(scale*3)*100, .3)
    ratio = number(breadth)
    if ratio is not None and 0 <= ratio <= 1:
        add("breadth", breadth_label, ratio, (1-ratio)*100, .2)
    vol = number(volume_ratio)
    # 放量上涨不增加恐慌；仅有当期方向和真实量比时启用该因子。
    if vol is not None and vol >= 0 and change is not None and change >= -100:
        pressure = max(0.0, vol-1)*50 if change < 0 else 0.0
        add("volume", "成交量/前5期均量", vol, pressure, .1)
    weight = sum(f["weight"] for f in factors)
    score = round(sum(f["score"]*f["weight"] for f in factors)/weight, 1) if weight else None
    level = ("extreme" if score >= 80 else "high" if score >= 60 else
             "neutral" if score >= 40 else "low" if score >= 20 else "calm") if score is not None else "unknown"
    labels = {"extreme": "极度恐慌", "high": "恐慌偏高", "neutral": "中性",
              "low": "恐慌偏低", "calm": "平稳", "unknown": "数据不足"}
    return {"available": score is not None, "score": score, "level": level,
            "label": labels[level], "scope": scope, "method": METHOD,
            "description": DESCRIPTION+f" 涨跌满刻度±{scale:g}%，回撤满刻度{scale*3:g}%。", "factors": factors,
            "coverage": round(weight, 2), "quality": "composite" if len(factors) > 1 else "snapshot" if factors else "unavailable",
            "as_of": as_of, "provisional": bool(provisional), "stale": bool(stale)}


def panic_from_bars(bars, *, quote=None, scope="stock", period="daily"):
    """以最后一根实际K线为准；缺口不拼接，日线以外保留当期口径。"""
    rows = sorted((dict(r) for r in bars or [] if isinstance(r, dict)),
                  key=lambda r: str(r.get("date") or r.get("day") or ""))
    quote = quote or {}
    last = rows[-1] if rows else {}
    close = number(last.get("close"))
    prev = number(rows[-2].get("close")) if len(rows) >= 2 else None
    change = ((close/prev-1)*100 if close is not None and close > 0 and prev is not None and prev > 0
              else number(last.get("pct_chg")))
    if change is None:
        change = number(quote.get("change_pct"))
    window = rows[-20:]
    closes = [number(r.get("close")) for r in window]
    dd = ((1-close/max(closes))*100 if len(closes) == 20 and all(c is not None and c > 0 for c in closes) else None)
    volumes = [number(r.get("volume")) for r in rows[-6:-1]]
    volume = number(last.get("volume"))
    vol_ratio = (volume/mean(volumes) if volume is not None and volume >= 0 and len(volumes) == 5
                 and all(v is not None and v >= 0 for v in volumes) and mean(volumes) > 0 else None)
    result = panic_indicator(change_pct=change, drawdown_pct=dd, volume_ratio=vol_ratio,
                             scope=scope, as_of=last.get("date") or last.get("day") or quote.get("updated_at"),
                             provisional=bool(last.get("realtime") or quote.get("applied")))
    result["period"] = period
    return result


def panic_from_sector_series(rows, *, stale=False):
    rows = sorted(rows or [], key=lambda r: str(r.get("trade_date") or ""))
    last = rows[-1] if rows else {}
    # 由实际等权成分收益计算回撤；不是合成板块OHLC。
    changes = [number(r.get("pct_chg_mean")) for r in rows[-20:]]
    dd = None
    if len(changes) == 20 and all(p is not None and p > -100 for p in changes):
        nav, peak = 1.0, 0.0
        for pct in changes:
            nav *= 1+pct/100
            peak = max(peak, nav)
        dd = (1-nav/peak)*100
    return panic_indicator(change_pct=last.get("pct_chg_mean"), drawdown_pct=dd,
                           breadth=last.get("breadth"), scope="sector", stale=stale,
                           as_of=last.get("trade_date"), provisional=last.get("provisional"))


def panic_from_quotes(rows, *, scope="market", breadth_label="样本上涨占比", as_of=None):
    changes = [number(r.get("change_pct")) for r in rows or [] if r.get("available") is not False]
    changes = [p for p in changes if p is not None and p >= -100]
    result = panic_indicator(change_pct=mean(changes) if changes else None,
                             breadth=sum(p > 0 for p in changes)/len(changes) if changes else None,
                             breadth_label=breadth_label, scope=scope, as_of=as_of)
    result["sample_count"] = len(changes)
    return result
