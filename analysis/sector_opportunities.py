"""持续的板块状态，独立于偶发拐点。聚合收益曲线不是板块K线。

相对强度=板块/同期参与聚合全市场的复合收益比；动量=最近10日
相对收益与此前10日之差。零轴四象限借鉴轮动思想，不使用RRG专有公式。
"""
from __future__ import annotations

from datetime import date
from analysis.operation_models import numeric

LABELS = {"leading": "领涨", "weakening": "转弱", "lagging": "落后", "improving": "改善", "unknown": "历史不足"}


def _return(rows, key):
    value = 1.0
    for row in rows:
        pct = numeric(row.get(key))
        if pct is None or pct <= -100:
            return None
        value *= 1+pct/100
    return (value-1)*100


def _relative(rows):
    market = []
    for row in rows:
        p, x = numeric(row.get("pct_chg_mean")), numeric(row.get("excess_vs_market"))
        if p is None or x is None:
            return None
        market.append({"return": p-x})
    sector_return, market_return = _return(rows, "pct_chg_mean"), _return(market, "return")
    if sector_return is None or market_return is None or market_return <= -100:
        return None
    return ((1+sector_return/100)/(1+market_return/100)-1)*100


def sector_landscape(series_by_sector, *, reference_date=None):
    latest = max((str(r.get("trade_date") or "") for seq in series_by_sector.values() for r in seq), default="")
    out = []
    for (name, kind), raw in series_by_sector.items():
        by_date = {str(r["trade_date"]): r for r in raw if r.get("trade_date")}
        rows = [by_date[d] for d in sorted(by_date)]
        if not rows:
            continue
        last = rows[-1]
        stamp = str(last["trade_date"])
        stale = stamp < latest
        if reference_date:
            try:
                age = (date.fromisoformat(str(reference_date)[:10])-date.fromisoformat(stamp)).days
                stale = stale or not 0 <= age <= 7
            except ValueError:
                stale = True
        sufficient = len(rows) >= 20 and not stale
        strength = _relative(rows[-20:]) if len(rows) >= 20 else None
        first = _relative(rows[-20:-10]) if len(rows) >= 20 else None
        second = _relative(rows[-10:]) if len(rows) >= 20 else None
        momentum = second-first if first is not None and second is not None else None
        state = ("leading" if strength >= 0 and momentum >= -1e-6 else "weakening" if strength >= 0
                 else "improving" if momentum > 1e-6 else "lagging") if strength is not None and momentum is not None else "unknown"
        breadth = numeric(last.get("breadth"))
        flows = [numeric(r.get("net_amount")) for r in rows[-5:]]
        flow5 = sum(flows) if len(flows) == 5 and all(v is not None for v in flows) else None
        streak = 0
        for row in reversed(rows):
            if (numeric(row.get("net_amount")) or 0) <= 0:
                break
            streak += 1
        eligible = (sufficient and state in ("leading", "improving") and not last.get("provisional")
                    and breadth is not None and breadth >= .5 and flow5 is not None and flow5 > 0)
        curve, nav, market_nav = [], 100.0, 100.0
        for row in rows:
            pct, excess = numeric(row.get("pct_chg_mean")), numeric(row.get("excess_vs_market"))
            if pct is None or excess is None or pct <= -100 or pct-excess <= -100:
                # 缺数据后不把跨缺口累计曲线伪装为连续收益。
                nav = market_nav = 100.0
                curve.append({"date": row["trade_date"], "nav": None, "relative": None})
                continue
            nav *= 1+pct/100
            market_nav *= 1+(pct-excess)/100
            curve.append({"date": row["trade_date"], "nav": round(nav, 4),
                          "relative": round((nav/market_nav-1)*100, 4),
                          "net_amount": numeric(row.get("net_amount")), "breadth": numeric(row.get("breadth"))})
        out.append({"sector": name, "sector_type": kind, "trade_date": stamp,
            "member_count": last.get("member_count"), "provisional": bool(last.get("provisional")),
            "history_count": len(rows), "stale": stale, "state": state, "state_label": LABELS[state],
            "relative20": round(strength, 3) if strength is not None else None,
            "momentum": round(momentum, 3) if momentum is not None else None,
            "return5": _return(rows[-5:], "pct_chg_mean") if len(rows) >= 5 else None,
            "return20": _return(rows[-20:], "pct_chg_mean") if len(rows) >= 20 else None,
            "breadth": breadth, "net_amount": numeric(last.get("net_amount")),
            "flow5": round(flow5, 2) if flow5 is not None else None, "inflow_streak": streak,
            "eligible": eligible, "curve": curve,
            "data_status": "数据滞后" if stale else "历史不足20日" if not sufficient else "相对强度数据缺失" if strength is None else "盘中待确认" if last.get("provisional") else "收盘数据",
            "next_condition": "保持相对强度、广度及资金共振" if eligible else "等待相对强度改善、上涨广度≥50%与5日净流入确认",
            "basis": "每日成分股等权聚合·非可交易板块指数；成员变化可能影响曲线"})
    return sorted(out, key=lambda r: (not r["stale"], r["eligible"], r["state"] in ("leading", "improving"), r["relative20"] if r["relative20"] is not None else -999), reverse=True)
