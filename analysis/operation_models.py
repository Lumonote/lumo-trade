"""可回放的操作结构：阶段、波段、突破/回调/修复。纯函数，零网络。

这些是经典方法的明确化研究规则，不冒充完整 SEPA/CAN SLIM 或缠论。
分型在右侧两根收盘后确认；事件记录确认日，不回填到极值日。
周线不足30根时明确降级为日线阶段代理，不把周期等同于缠论级别。
"""
from __future__ import annotations

import math
from datetime import date, datetime
from statistics import mean
from zoneinfo import ZoneInfo

STAGES = {"base": "筑底/整理", "advance": "上升阶段", "top": "高位整理", "decline": "下降阶段", "unknown": "历史不足"}


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def normalize_bars(records):
    rows = {}
    for item in records or []:
        stamp = str(item.get("date") or item.get("day") or "")
        try:
            date.fromisoformat(stamp[:10])
        except ValueError:
            continue
        values = {key: numeric(item.get(key)) for key in ("open", "high", "low", "close")}
        if (any(v is None or v <= 0 for v in values.values())
                or not values["low"] <= min(values["open"], values["close"])
                or not max(values["open"], values["close"]) <= values["high"]):
            continue
        rows[stamp] = {**values, "date": stamp, "volume": numeric(item.get("volume")),
                       "provisional": bool(item.get("provisional"))}
    return [rows[key] for key in sorted(rows)]


def confirmed_fractals(rows, radius=2):
    """保留每个已确认局部极值，追加行情不会改写历史确认事件。"""
    points = []
    for i in range(radius, len(rows) - radius):
        left, right = rows[i-radius:i], rows[i+1:i+radius+1]
        r = rows[i]
        for kind, key, compare in (("high", "high", max), ("low", "low", min)):
            values = [v[key] for v in left + right]
            extreme = compare(values)
            if (r[key] > extreme if kind == "high" else r[key] < extreme):
                points.append({"kind": kind, "date": r["date"], "price": r[key],
                               "confirmed_at": rows[i+radius]["date"], "index": i})
    return points


def _weekly(rows):
    weeks = {}
    for row in rows:
        stamp = date.fromisoformat(row["date"][:10])
        key = stamp.isocalendar()[:2]
        if key not in weeks:
            weeks[key] = dict(row)
        else:
            out = weeks[key]
            out.update(high=max(out["high"], row["high"]), low=min(out["low"], row["low"]),
                       close=row["close"], date=row["date"])
    result = list(weeks.values())
    # 未到周五的最后一周不作为周线收盘确认；缺交易日时宁可保守。
    if result and date.fromisoformat(result[-1]["date"][:10]).weekday() < 4:
        result.pop()
    return result


def _stage(rows):
    if len(rows) < 60:
        return {"key": "unknown", "label": STAGES["unknown"], "basis": "至少60根完整日K"}
    weekly = _weekly(rows)
    full = len(weekly) >= 35
    source, window, shift = (weekly, 30, 5) if full else (rows, 60, 10)
    closes = [r["close"] for r in source]
    average, previous = mean(closes[-window:]), mean(closes[-window-shift:-shift])
    slope = average / previous - 1
    price = closes[-1]
    peak = max(r["high"] for r in rows[-60:])
    key = ("advance" if price > average and slope > .003
           else "decline" if price < average and slope < -.003
           else "top" if price > peak * .85 else "base")
    return {"key": key, "label": STAGES[key], "average": round(average, 4),
            "slope_pct": round(slope*100, 2), "weekly_count": len(weekly),
            "basis": "30周均线与5周斜率" if full else "日线60日代理·完整周线不足35根"}


def _check(key, label, passed, value=None):
    return {"key": key, "label": label, "passed": passed, "value": value}


def _model(key, label, theory, checks, levels, provisional):
    ready = all(c["passed"] is True for c in checks)
    state = "provisional" if ready and provisional else "triggered" if ready else "watch"
    return {"key": key, "label": label, "theory": theory, "checks": checks,
            "state": state, "state_label": {"triggered": "技术条件确认", "provisional": "盘中待收盘", "watch": "等待条件"}[state],
            "matched": sum(c["passed"] is True for c in checks), "total": len(checks), "levels": levels}


def analyze_operations(records, *, period="daily", now=None, fundamentals=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    rows = [r for r in normalize_bars(records) if r["date"][:10] <= now.date().isoformat()]
    daily = period in ("daily", "1d", "day")
    provisional = bool(rows and (rows[-1]["provisional"] or
        (rows[-1]["date"][:10] == now.date().isoformat() and (now.hour, now.minute) < (15, 5))))
    closed = rows[:-1] if provisional else rows
    points = confirmed_fractals(closed)
    result = {"version": 1, "available": False, "period": period,
              "as_of": rows[-1]["date"] if rows else None,
              "confirmed_as_of": closed[-1]["date"] if closed else None,
              "provisional": provisional, "fractals": points,
              "models": [], "events": [], "zones": [], "stage_history": [],
              "stage": _stage(closed) if daily else {"key": "unknown", "label": "分钟波段", "basis": "分钟K不用于周线阶段判定"},
              "theory_note": "经典方法的规则化技术候选；波段分型不等同于缠论笔/线段，条件数不代表胜率。"}
    if not daily or len(closed) < 60:
        result["reason"] = "需要至少60根完整日K" if daily else "分钟图仅展示已确认波段；操作模型以日K为依据"
        return result
    # 极端跳变可能来自除权/价格口径变化，先暂停模型，保留真实K线供核对。
    recent = closed[-61:]
    if any(abs(b["close"] / a["close"] - 1) > .35 for a, b in zip(recent, recent[1:])):
        result["reason"] = "价格出现超过35%的单日跳变，需核对复权/除权口径"
        return result
    close = rows[-1]["close"]
    prior = closed[:-1] if not provisional else closed
    atr = mean(max(b["high"]-b["low"], abs(b["high"]-a["close"]), abs(b["low"]-a["close"]))
               for a, b in zip(prior[-21:-1], prior[-20:]))
    if atr <= 0:
        result["reason"] = "波动距离为零，无法制定结构风险区"
        return result
    closes = [r["close"] for r in closed]
    ma20, ma60 = mean(closes[-20:]), mean(closes[-60:])
    ceiling, floor = max(r["high"] for r in prior[-20:]), min(r["low"] for r in prior[-20:])
    support = min(r["low"] for r in prior[-10:])
    high_points = [p for p in points if p["kind"] == "high"]
    low_points = [p for p in points if p["kind"] == "low"]
    dow = ("up" if len(high_points) >= 2 and len(low_points) >= 2 and
           high_points[-1]["price"] > high_points[-2]["price"] and low_points[-1]["price"] > low_points[-2]["price"]
           else "down" if len(high_points) >= 2 and len(low_points) >= 2 and
           high_points[-1]["price"] < high_points[-2]["price"] and low_points[-1]["price"] < low_points[-2]["price"] else "range")
    trend = result["stage"]["key"] == "advance" and close >= ma60
    volumes = [r["volume"] for r in prior[-20:]]
    volume_base = mean(volumes) if len(volumes) == 20 and all(v is not None and v > 0 for v in volumes) else None
    vr = rows[-1]["volume"] / volume_base if volume_base and rows[-1]["volume"] is not None else None
    recent_vol = [r["volume"] for r in prior[-5:]]
    dry = mean(recent_vol)/volume_base if volume_base and all(v is not None and v > 0 for v in recent_vol) else None
    width = max(r["high"] for r in prior[-10:])-min(r["low"] for r in prior[-10:])
    old_width = max(r["high"] for r in prior[-30:-10])-min(r["low"] for r in prior[-30:-10])
    contraction = old_width > 0 and width/old_width < .75

    def levels(entry, stop, target):
        low, high = max(.01, entry-atr*.15), entry+atr*.35
        stop = max(.01, min(stop, low-atr*.25))
        risk = high-stop
        projected = target <= high
        target1 = high+2*risk if projected else target
        return {"entry_low": round(low, 2), "entry_high": round(high, 2), "stop_loss": round(stop, 2),
                "target1": round(target1, 2), "target2": round(max(target1+risk, high+3*risk), 2),
                "support": round(support, 2), "resistance": round(ceiling, 2),
                "risk_pct": round(risk/high*100, 3), "risk_reward": round((target1-high)/risk, 2),
                "target_basis": "2R风险情景·非价格预测" if projected else "前期结构压力区"}

    break_levels = levels(ceiling, support-atr*.25, ceiling)
    pull_levels = levels(ma20, support-atr*.25, ceiling)
    base_levels = levels(ceiling, floor-atr*.25, ceiling)
    spring = None
    for i in range(max(20, len(closed)-15), len(closed)):
        base_low = min(r["low"] for r in closed[i-20:i])
        if closed[i]["low"] < base_low and closed[i]["close"] > base_low:
            spring = {"date": closed[i]["date"], "low": closed[i]["low"], "high": closed[i]["high"], "index": i}
    models = [
        _model("breakout", "主升浪突破", "温斯坦阶段＋相对强度候选＋SEPA/VCP形态思想＋唐奇安突破", [
            _check("trend", "上升阶段", trend, result["stage"]["label"]),
            _check("contraction", "波动收缩", contraction, round(width/old_width, 2) if old_width else None),
            _check("trigger", "越过前20日高点", close > ceiling, round(ceiling, 2)),
            _check("volume", "成交量≥1.3倍", vr >= 1.3 if vr is not None else None, round(vr, 2) if vr is not None else None),
            _check("extension", "未远离突破区", close <= ceiling+atr, round((close-ceiling)/atr, 2)),
        ], break_levels, provisional),
        _model("pullback", "强势回调再启动", "道氏波段＋威科夫供需测试＋趋势回调", [
            _check("trend", "上升阶段未破坏", trend, dow),
            _check("location", "回到MA20附近", abs(close-ma20) <= atr, round(ma20, 2)),
            _check("dry", "回调量能收缩", dry <= .85 if dry is not None else None, round(dry, 2) if dry is not None else None),
            _check("trigger", "收复前日高点", close > prior[-1]["high"], prior[-1]["high"]),
            _check("support", "结构支撑有效", close > support, support),
        ], pull_levels, provisional),
        _model("reversal", "底部修复观察", "威科夫Spring候选＋测试确认＋结构突破", [
            _check("base", "整理或修复阶段", result["stage"]["key"] in ("base", "advance")),
            _check("spring", "跌破后收回支撑", spring is not None, spring["date"] if spring else None),
            _check("test", "后续守住测试低点", bool(spring and spring["index"] < len(closed)-1 and
                min(r["low"] for r in closed[spring["index"]+1:]) > spring["low"])),
            _check("trigger", "突破整理上沿", close > ceiling, ceiling),
            _check("volume", "突破量能确认", vr >= 1.3 if vr is not None else None, round(vr, 2) if vr is not None else None),
        ], base_levels, provisional),
    ]
    selected = max(models, key=lambda m: (m["state"] == "triggered", m["matched"]/m["total"], m["key"] != "reversal"))
    # 历史标记仅使用当日之前的窗口；实际事件日期就是确认日期。
    for i in range(60, len(closed)):
        previous = closed[i-20:i]
        v = [r["volume"] for r in previous]
        volume_valid = all(x is not None and x > 0 for x in v) and closed[i]["volume"] is not None
        resistance = max(r["high"] for r in previous)
        stop = min(r["low"] for r in closed[i-10:i])
        prior_ma = mean(r["close"] for r in closed[i-60:i])
        if volume_valid and closed[i]["close"] > resistance and closed[i]["close"] > prior_ma and closed[i]["volume"] >= mean(v)*1.3:
            result["events"].append({"date": closed[i]["date"], "confirmed_at": closed[i]["date"],
                "price": closed[i]["low"], "kind": "breakout", "label": "放量突破", "invalid_below": stop})
        elif closed[i]["close"] < stop:
            result["events"].append({"date": closed[i]["date"], "confirmed_at": closed[i]["date"],
                "price": closed[i]["high"], "kind": "risk", "label": "支撑破坏"})
    # 收盘阶段带按当时可见数据计算，禁止使用最终阶段回涂整段历史。
    for i in range(59, len(closed)):
        stage = _stage(closed[:i+1])
        result["stage_history"].append({"date": closed[i]["date"], "key": stage["key"], "label": stage["label"]})
    result.update(available=True, models=models, selected_model=selected["key"], levels=selected["levels"],
        dow={"key": dow, "label": {"up": "高低点抬升", "down": "高低点下移", "range": "波段待确认"}[dow]},
        metrics={"ma20": round(ma20, 4), "ma60": round(ma60, 4), "atr20": round(atr, 4),
                 "volume_ratio": round(vr, 2) if vr is not None else None, "contraction": contraction},
        fundamentals={"available": bool(fundamentals), "label": "已提供基本面资料·需独立核验" if fundamentals else "成长/业绩条件待核实·不宣称完整CAN SLIM"},
        zones=[{"from": prior[-20]["date"], "to": rows[-1]["date"], "low": floor, "high": ceiling,
                "label": "20日结构区间"}])
    return result
