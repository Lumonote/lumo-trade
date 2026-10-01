"""可解释的研究操作计划。纯函数；价格、仓位均来自调用方提供的数据。

仓位阈值是研究默认值，不是经过回测验证的收益预测。买入必须通过
大盘、板块、趋势、价格和组合额度门控；退出条件始终优先于买入。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta
from statistics import mean

from analysis.market_regime import position_advice
from analysis.risk_opportunity_engine import OPP_BANDS, score_stock_risk

# 与 market_regime 的 2/4/6 成建议保持一致；risk_on 补充明确上限。
POLICIES = {
    "risk_on": (80, 15, 30), "neutral": (60, 10, 25),
    "caution": (40, 5, 15), "risk_off": (20, 0, 10),
    "unknown": (0, 0, 0),
}
LEVEL_LABELS = {"risk_on": "趋势向上", "neutral": "震荡", "caution": "回调防守",
                "risk_off": "风险收缩", "unknown": "数据待补齐"}
MAX_DATA_AGE_DAYS = 7
PLAN_VALID_DAYS = 7
RISK_BUDGET_PCT = 0.5
MIN_RISK_REWARD = 1.5


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def day(value):
    text = str(value or "")[:10]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def fresh(value, today):
    stamp = day(value)
    return stamp is not None and 0 <= (today - stamp).days <= MAX_DATA_AGE_DAYS


def market_plan(pulse, account=None, positions=None, *, today):
    pulse, account = pulse or {}, account or {}
    stamp = (pulse.get("as_of") or {}).get("index")
    level = pulse.get("level") or "unknown"
    cards = pulse.get("indices") or []
    usable = bool(cards) and fresh(stamp, today) and not (pulse.get("degraded") or {}).get("index")
    if not usable:
        level = "unknown"
    # 部分指数缺失时不得沿用激进档位。
    partial = len(cards) < 4 or any(c.get("pulse") == "unknown" or not fresh(c.get("as_of") or stamp, today) for c in cards)
    if partial and level in ("risk_on", "neutral"):
        level = "caution"
    if level not in POLICIES:
        level = "unknown"
    total, single, sector = POLICIES[level]
    equity = number(account.get("total_equity"))
    invested = sum(number(p.get("market_value")) or 0 for p in positions or [])
    used = invested / equity * 100 if equity and equity > 0 else None
    cash = number(account.get("cash"))
    headroom = max(0, min(total - (used or 0), (cash / equity * 100) if equity and cash is not None else 0))
    return {
        "level": level, "label": LEVEL_LABELS[level], "as_of": stamp,
        "partial": partial, "indices": cards, "style": pulse.get("style") or {},
        "advice": position_advice(level), "total_cap_pct": total,
        "single_cap_pct": single, "sector_cap_pct": sector,
        "risk_budget_pct": RISK_BUDGET_PCT, "used_position_pct": round(used, 2) if used is not None else None,
        "new_budget_pct": round(headroom, 2),
        "reduce_pct": round(max(0, (used or 0) - total), 2) if level != "unknown" else 0,
        "account_available": bool(equity and equity > 0 and cash is not None),
        "entry_allowed": usable and single > 0 and bool(equity and equity > 0 and cash is not None),
        "rules_note": "仓位与 ATR 阈值为研究默认值，未代表历史胜率；额度基于模拟账户。",
    }


def sector_plan(meta, pulse, *, today):
    names = {str(meta.get("sector") or "")} | {str(b) for b in meta.get("boards") or []}
    names.discard("")
    sectors = (pulse or {}).get("sectors") or {}
    matches = [(state, row) for state in ("fired", "watch") for row in sectors.get(state) or []
               if row.get("sector") in names]
    confirmed = [r for state, r in matches if state == "fired" and not r.get("provisional")
                 and fresh(r.get("trade_date"), today)]
    ready = bool(confirmed) and not ((pulse or {}).get("degraded") or {}).get("sector")
    landscape = [r for r in sectors.get("landscape") or [] if r.get("sector") in names]
    leaders = [r for r in landscape if r.get("eligible") and not r.get("stale")
               and not r.get("provisional") and fresh(r.get("trade_date"), today)]
    strength_ready = bool(leaders) and not ((pulse or {}).get("degraded") or {}).get("sector")
    return {
        "name": meta.get("sector") or "未识别板块", "boards": sorted(names),
        "state": "confirmed" if ready else "leading" if strength_ready else "watch" if matches or landscape else "unknown",
        "label": "板块拐点已确认" if ready else "板块强度/广度/资金共振" if strength_ready else "板块待确认" if matches or landscape else "板块数据待补齐",
        "entry_allowed": ready or strength_ready, "evidence": [r for _, r in matches],
        "landscape": landscape,
        "condition": "有效期内的已验证拐点，或收盘相对强度改善且上涨广度≥50%、5日净流入为正；后者为状态规则，不代表历史胜率",
        "exit_condition": "板块信号失效或转为临界观察时暂停加仓，复核持仓",
    }


def stock_plan(code, meta, kline, opportunity=None, *, today):
    rows = sorted((kline or {}).get("records") or [], key=lambda r: str(r.get("date") or ""))
    by_day = {}
    for r in rows:
        values = {k: number(r.get(k)) for k in ("close", "high", "low")}
        if (all(v is not None and v > 0 for v in values.values())
                and values["low"] <= values["close"] <= values["high"] and day(r.get("date"))):
            by_day[day(r.get("date"))] = {**r, **values}
    valid = [by_day[d] for d in sorted(by_day)]
    stamp = valid[-1].get("date") if valid else None
    result = {"code": code, "name": meta.get("name") or code, "sector": meta.get("sector") or "",
              "available": False, "data_as_of": stamp, "source": (kline or {}).get("source"),
              "opportunity_score": (opportunity or {}).get("score"), "levels": {},
              "valid_until": (today + timedelta(days=PLAN_VALID_DAYS)).isoformat()}
    if len(valid) < 60 or not fresh(stamp, today):
        result["reason"] = "需要至少60根有效日K且最新数据距今不超过7个自然日"
        return result
    closes = [r["close"] for r in valid]
    current, ma20, ma60 = closes[-1], mean(closes[-20:]), mean(closes[-60:])
    slope = ma20 - mean(closes[-25:-5])
    atr = mean(max(r["high"] - r["low"], abs(r["high"] - prev["close"]), abs(r["low"] - prev["close"]))
               for prev, r in zip(valid[-21:-1], valid[-20:]))
    if atr <= 0:
        result["reason"] = "波动数据不足，不能制定有效止损距离"
        return result
    support = min(r["low"] for r in valid[-11:-1])
    resistance = max(r["high"] for r in valid[-21:-1])
    center = min(current, max(ma20, support))
    low, high = round(center - atr * .25, 2), round(center + atr * .25, 2)
    stop = round(min(support, low) - atr * .5, 2)
    risk = high - stop
    if not (0 < stop < low <= high and risk > 0):
        result["reason"] = "价格过低或止损距离无效"
        return result
    target1 = round(resistance if resistance > high else high + 2 * risk, 2)
    target2 = round(max(target1 + risk, high + 3 * risk), 2)
    trend = "up" if current >= ma20 >= ma60 and slope > 0 else "down" if current < ma20 and slope < 0 else "range"
    vol = [number(r.get("volume")) for r in valid[-21:-1]]
    last_vol = number(valid[-1].get("volume"))
    vol_ratio = last_vol / mean(vol) if all(v is not None and v > 0 for v in vol) and last_vol is not None else None
    signals = dict((opportunity or {}).get("signals") or {})
    # 自选股未进入挖掘榜时仍可由日K复核风险，避免把“未知”当作低风险。
    changes = [b - a for a, b in zip(closes[-15:-1], closes[-14:])]
    gain = mean(max(0, v) for v in changes)
    loss = mean(max(0, -v) for v in changes)
    for key in ("rsi", "chase", "change_3d", "sell_signals", "quant_score", "limit_up_streak"):
        if number(signals.get(key)) is None:
            signals.pop(key, None)
    signals.setdefault("rsi", 100 if loss == 0 and gain > 0 else 50 if loss == 0 else 100 - 100 / (1 + gain / loss))
    signals.setdefault("change_3d", (current / closes[-4] - 1) * 100)
    signals["is_st"] = bool(signals.get("is_st")) or "ST" in str(meta.get("name") or "").upper()
    stock_risk = score_stock_risk(signals)
    risk_pct = risk / high * 100
    result.update({
        "available": True, "reference_price": current,
        "trend": trend, "trend_label": {"up": "上升趋势", "down": "下降趋势", "range": "震荡待确认"}[trend],
        "ma20": round(ma20, 2), "ma60": round(ma60, 2), "atr20": round(atr, 4),
        "volume_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "risk": stock_risk, "signals": signals,
        "opportunity_degraded": bool((opportunity or {}).get("degraded")),
        "levels": {"entry_low": low, "entry_high": high, "stop_loss": stop,
                   "target1": target1, "target2": target2, "support": round(support, 2),
                   "resistance": round(resistance, 2), "risk_pct": round(risk_pct, 3),
                   "risk_reward": round((target1 - high) / risk, 2),
                   "target_basis": "近20日压力位" if resistance > high else "2R波动投影（非预测收益）"},
        "stages": ["先试仓：不超过可新增额度的40%", "确认后加仓：站稳MA20且放量≥1.2倍，使用余下60%额度",
                   "跌破止损或趋势转弱时停止加仓；不向下摊平成本"],
        "exits": ["触及止损：退出剩余仓位", "第一目标：减持原计划仓位30%",
                  "第二目标：再减持原计划仓位40%", "余下30%：跌破锁定止损与MA20中的较高者时退出"],
    })
    operation = (kline or {}).get("operation")
    if operation is not None:
        result["operation"] = operation
        if not operation.get("available"):
            result.update(available=False, reason=operation.get("reason") or "操作结构数据不足")
            return result
        if day(operation.get("as_of")) != day(stamp):
            result.update(available=False, reason="最新日K的OHLC结构无效，请复核行情后制定计划")
            return result
        levels = dict(operation.get("levels") or {})
        if not (0 < levels.get("stop_loss", 0) < levels.get("entry_low", 0) <= levels.get("entry_high", 0)):
            result.update(available=False, reason="结构价位精度不足，无法制定有效风险距离")
            return result
        stage = operation["stage"]["key"]
        result.update(levels=levels, trend="up" if stage == "advance" else "down" if stage == "decline" else "range",
                      trend_label=operation["stage"]["label"])
        selected = next(m for m in operation["models"] if m["key"] == operation["selected_model"])
        result["model"] = {k: selected[k] for k in ("key", "label", "state", "state_label", "theory")}
        result["stages"] = ["试仓：模型收盘确认且大盘、板块和风险预算均通过",
                            "加仓：突破/回调确认持续有效，已有仓位盈利且预算仍有余量",
                            "结构支撑失效时取消计划；不向下摊平"]
        result["exits"] = ["跌破锁定结构失效线：退出复核", "进入第一压力/风险情景区：分批减仓复核",
                           "余仓按趋势保护线跟踪；目标区不代表必达价格"]
    basis = {k: result[k] for k in ("code", "data_as_of", "levels")}
    if result.get("model"):
        basis["model"] = result["model"]["key"]
    result["basis_key"] = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:20]
    return result


def evaluate_plan(plan, candidate, market, sector, quote=None, position=None, *, today):
    """保留已锁定价位；新数据只影响门控与状态，不重算原止损。"""
    result = dict(plan)
    quote, position = quote or {}, position or {}
    live = number(quote.get("price"))
    quote_ok = (live is not None and live > 0 and fresh(quote.get("quote_date"), today)
                and day(quote.get("quote_date")) >= (day(candidate.get("data_as_of")) or today))
    current = live if quote_ok else candidate.get("reference_price")
    held = (number(position.get("qty")) or 0) > 0
    levels = plan.get("levels") or {}
    blockers = []
    operation = candidate.get("operation") or {}
    if operation.get("available"):
        model = next((m for m in operation.get("models") or [] if m["key"] == operation.get("selected_model")), {})
        if model.get("state") != "triggered":
            pending = [c["label"] for c in model.get("checks") or [] if c["passed"] is not True]
            blockers.append("盘中模型待收盘确认" if model.get("state") == "provisional" else "模型待确认：" + "、".join(pending))
    if not plan.get("available") or not candidate.get("available"):
        blockers.append(candidate.get("reason") or plan.get("reason") or "个股数据不足")
    if not market.get("entry_allowed"):
        blockers.append("大盘或模拟账户门控未通过，暂停新增仓位")
    if not sector.get("entry_allowed"):
        blockers.append(sector["label"])
    if candidate.get("trend") != "up":
        blockers.append("个股尚未确认上升趋势")
    if current is not None and candidate.get("ma20") and current < candidate["ma20"]:
        blockers.append("价格仍在MA20下方，等待站稳")
    score = number(candidate.get("opportunity_score"))
    if score is not None and score < OPP_BANDS["mid"]:
        blockers.append(f"机会评分未达观察门槛{OPP_BANDS['mid']:.0f}分")
    if candidate.get("opportunity_degraded"):
        blockers.append("机会评分数据降级，需重新分析复核")
    if (levels.get("risk_reward") or 0) < MIN_RISK_REWARD:
        blockers.append(f"首目标盈亏比不足{MIN_RISK_REWARD}:1")
    signals = candidate.get("signals") or {}
    if ((candidate.get("risk") or {}).get("risk") or 0) >= 60 or signals.get("is_st") or signals.get("halt"):
        blockers.append("个股高风险/ST/停牌，暂停买入")
    flow = number(quote.get("main_net_inflow"))
    if flow is not None and flow < 0:
        blockers.append("当日主力净流出，等待资金确认")
    if not quote_ok:
        blockers.append("实时报价缺失或日期无效，仅显示日K参考计划")
    reference = number(candidate.get("reference_price"))
    if quote_ok and reference and abs(live / reference - 1) > .3:
        blockers.append("报价与日K价格口径差异超过30%，需复核复权或除权数据")
    expiry = day(plan.get("valid_until"))
    expired = not expiry or today > expiry
    if expired:
        blockers.append("计划已过7个自然日复核期，请重新制定")
    status, label = "blocked", "等待条件"
    if held and current is not None and levels and current <= levels["stop_loss"]:
        status, label = "exit", "止损退出"
    elif not held and current is not None and levels and current <= levels["stop_loss"]:
        status, label = "invalidated", "计划失效"
    elif held and current is not None and levels and current >= levels["target2"]:
        status, label = "take_profit", "第二目标止盈"
    elif held and current is not None and levels and current >= levels["target1"]:
        status, label = "take_profit", "第一目标止盈"
    elif held and candidate.get("trend") == "down":
        status, label = "reduce", "趋势转弱·减仓复核"
    elif held and plan.get("locked") and current is not None and candidate.get("ma20") and current < candidate["ma20"]:
        status, label = "reduce", "跌破趋势保护线·减仓复核"
    elif held and market.get("reduce_pct", 0) > 0:
        status, label = "reduce", "组合超限·减仓复核"
    elif not blockers and current is not None:
        if current < levels["stop_loss"]:
            status, label = "invalidated", "计划失效"
        elif current > levels["entry_high"]:
            status, label = ("hold", "持有·等待确认") if held else ("wait_pullback", "等待回踩·不追高")
        elif current < levels["entry_low"]:
            status, label = "wait_reclaim", "等待收复买入区"
        elif held:
            cost = number(position.get("avg_cost"))
            if cost and current >= cost and (candidate.get("volume_ratio") or 0) >= 1.2:
                status, label = "add", "确认加仓条件"
            else:
                status, label = "hold", "持有·暂停加仓"
        else:
            status, label = "entry", "试仓条件满足"
    elif held:
        status, label = "hold", "持有·暂停加仓"
    risk_pct = number(levels.get("risk_pct")) or 0
    max_position = min(market["single_cap_pct"], RISK_BUDGET_PCT / risk_pct * 100) if risk_pct > 0 else 0
    result.update({"status": status, "status_label": label, "blockers": blockers,
                   "current_price": current, "quote_date": quote.get("quote_date") if quote_ok else None,
                   "price_source": "quote" if quote_ok else "daily_close",
                   "held": held, "market": market, "sector_plan": sector,
                   "max_position_pct": round(max_position, 2), "new_position_pct": 0,
                   "stop_locked": bool(plan.get("locked")),
                   "review_note": "每个止盈档位只执行一次，需人工复盘确认；复核成交能力、可卖数量及费用，不自动下单。"})
    return result
