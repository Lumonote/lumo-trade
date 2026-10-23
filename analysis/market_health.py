"""大盘体检的可解释规则。百分比均使用百分点；未知数据不算未触发。"""
from __future__ import annotations

import math
import datetime as dt
from statistics import mean, median, stdev

INDEXES = {"sh000001": "上证指数", "sh000300": "沪深300", "sz399001": "深证成指",
           "sz399006": "创业板指", "sh000688": "科创50", "sh000852": "中证1000", "sz399303": "国证2000"}
VERSION = "market_health_v1"


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def normalize_bars(bars):
    result = {}
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        day = str(bar.get("day") or bar.get("date") or "")[:10]
        try:
            day = dt.date.fromisoformat(day).isoformat()
        except ValueError:
            continue
        close = number(bar.get("close"))
        if len(day) == 10 and close is not None and close > 0:
            result[day] = {"date": day, **{k: number(bar.get(k)) for k in ("open", "high", "low", "volume")}, "close": close}
    return sorted(result.values(), key=lambda b: b["date"])


def rsi(closes, periods=14):
    """Wilder RSI；横盘为50，单边上涨为100。"""
    if len(closes) <= periods:
        return None
    delta = [b-a for a, b in zip(closes, closes[1:])]
    gain = mean(max(0, d) for d in delta[:periods])
    loss = mean(max(0, -d) for d in delta[:periods])
    for d in delta[periods:]:
        gain = (gain*(periods-1)+max(0, d))/periods
        loss = (loss*(periods-1)+max(0, -d))/periods
    return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100-100/(1+gain/loss)


def index_metrics(symbol, bars):
    rows = normalize_bars(bars)
    if not rows:
        return {"symbol": symbol, "name": INDEXES.get(symbol, symbol), "available": False}
    c = [r["close"] for r in rows]
    last = c[-1]
    out = {"symbol": symbol, "name": INDEXES.get(symbol, symbol), "available": True,
           "as_of": rows[-1]["date"], "close": last, "bars": len(rows)}
    for n in (1, 5, 20, 60):
        out[f"ret{n}"] = (last/c[-n-1]-1)*100 if len(c) > n else None
    for n in (20, 60, 120, 250):
        ma = mean(c[-n:]) if len(c) >= n else None
        out[f"ma{n}"] = ma
        out[f"dev{n}"] = (last/ma-1)*100 if ma else None
    # 固定回看500根收盘价，避免把11年高点与本轮高点混为一谈。
    window = c[-500:]
    peak_i = max(range(len(window)), key=lambda i: window[i])
    out.update(drawdown=(last/max(window)-1)*100, down_days=len(window)-1-peak_i,
               peak=max(window), peak_date=rows[-len(window)+peak_i]["date"], rsi14=rsi(c))
    out["position_60"] = ((last-min(c[-60:]))/(max(c[-60:])-min(c[-60:]))*100
                           if len(c) >= 60 and max(c[-60:]) != min(c[-60:]) else None)
    for n, key, exclude in ((250, "volume250", False), (5, "volume5", True)):
        v = [r["volume"] for r in (rows[-n-1:-1] if exclude else rows[-n:])]
        out[key] = (rows[-1]["volume"]/mean(v) if len(v) == n and all(x is not None and x >= 0 for x in v)
                    and mean(v) > 0 and rows[-1]["volume"] is not None else None)
    returns = [math.log(b/a) for a, b in zip(c[-21:], c[-20:])] if len(c) >= 21 else []
    out["volatility20"] = stdev(returns)*math.sqrt(250)*100 if len(returns) == 20 else None
    out["new_low20"] = last <= min(c[-20:]) if len(c) >= 20 else None
    out["ma20_rising"] = out["ma20"] > mean(c[-25:-5]) if len(c) >= 25 else None
    mas = [out[f"ma{n}"] for n in (20, 60, 120, 250)]
    out["trend"] = ("数据不足" if any(v is None for v in mas) else "多头排列" if last > mas[0] > mas[1] > mas[2] > mas[3]
                    else "空头排列" if last < mas[0] < mas[1] < mas[2] < mas[3] else "均线交织")
    # 波段锚点必须遵守先低后高的时间顺序。
    anchor = min(window[:peak_i+1])
    out["levels"] = {str(p): max(window)-(max(window)-anchor)*p for p in (.382, .5, .618, .786)} if anchor < max(window) else {}
    out["swing_low"] = anchor
    out["series"] = [{"date": r["date"], "close": r["close"],
                       "ma20": mean(c[i-19:i+1]) if i >= 19 else None,
                       "ma250": mean(c[i-249:i+1]) if i >= 249 else None}
                      for i, r in enumerate(rows) if i >= len(rows)-250]
    return out


def market_snapshot(rows):
    rows = [r for r in rows if number(r.get("trade")) is not None and number(r.get("trade")) > 0]
    changes = [number(r.get("changepercent")) for r in rows]
    changes = [v for v in changes if v is not None and v >= -100]
    pbs = [number(r.get("pb")) for r in rows if number(r.get("pb")) is not None and number(r.get("pb")) > 0]
    pes = [number(r.get("per")) for r in rows if number(r.get("per")) is not None and number(r.get("per")) > 0]
    turns = [number(r.get("turnoverratio")) for r in rows if number(r.get("turnoverratio")) is not None]
    up, down = sum(v > 0 for v in changes), sum(v < 0 for v in changes)
    weights = [(number(r.get("mktcap")), number(r.get("changepercent"))) for r in rows]
    weights = [(w, v) for w, v in weights if w is not None and w > 0 and v is not None]
    limits = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        pct = number(row.get("changepercent"))
        threshold = 29.5 if symbol.startswith("bj") else 19.8 if symbol.startswith(("sz30", "sh68")) else 4.8 if "ST" in str(row.get("name", "")).upper() else 9.8
        if pct is not None:
            limits.append((pct, threshold))
    return {"total": len(rows), "change_sample": len(changes), "up": up, "down": down, "flat": len(changes)-up-down,
            "up_ratio": up/len(changes)*100 if changes else None,
            "limit_up": sum(v >= t for v, t in limits), "limit_down": sum(v <= -t for v, t in limits),
            "limits_estimated": True, "pb_median": median(pbs) if pbs else None, "pb_sample": len(pbs),
            "pe_median": median(pes) if pes else None, "pe_sample": len(pes),
            "below_pb1_ratio": sum(v < 1 for v in pbs)/len(pbs)*100 if pbs else None,
            "turnover_median": median(turns) if turns else None, "chg_median": median(changes) if changes else None,
            "mv_weighted_chg": sum(w*v for w, v in weights)/sum(w for w, _ in weights) if weights else None}


def breadth_snapshot(histories, as_of):
    """仅同交易日且历史完整的样本；每项分别记录真实分母。"""
    counts, hits, dd = {}, {}, []
    for bars in histories.values():
        rows = normalize_bars(bars)
        if not rows or rows[-1]["date"] != as_of:
            continue
        c = [r["close"] for r in rows]
        for n in (20, 60, 250):
            if len(c) >= n:
                key = f"above_ma{n}"
                counts[key] = counts.get(key, 0)+1
                hits[key] = hits.get(key, 0)+int(c[-1] > mean(c[-n:]))
        if len(c) >= 60:
            for key, hit in (("newlow60", c[-1] <= min(c[-60:])), ("newhigh60", c[-1] >= max(c[-60:]))):
                counts[key] = counts.get(key, 0)+1
                hits[key] = hits.get(key, 0)+int(hit)
        if len(c) >= 250:
            dd.append((c[-1]/max(c[-250:])-1)*100)
    return {"as_of": as_of, "sample": counts.get("above_ma20", 0), "denominators": counts,
            **{k: hits[k]/v*100 for k, v in counts.items()}, "dd52_median": median(dd) if dd else None,
            "method": "sina_unadjusted_stratified400"}


def breadth_history(histories, dates):
    """回算固定成分组的已收盘广度，不用今天的值填充过去。

    每个成分在窗口每天均需有真实收盘价且最早一天已有250根历史；
    停牌/短历史成分整个窗口排除，避免五日确认的分母漂移。
    """
    import hashlib
    days = sorted(set(dates))
    if not days:
        return []
    stable = {}
    for symbol, bars in histories.items():
        rows = normalize_bars(bars)
        positions = {r["date"]:i for i,r in enumerate(rows)}
        if all(d in positions and positions[d] >= 249 for d in days):
            stable[symbol] = rows
    cohort = hashlib.sha256(",".join(sorted(stable)).encode()).hexdigest()[:16]
    return [{**breadth_snapshot({s:[r for r in rows if r["date"] <= day]
                                for s,rows in stable.items()}, day),
             "cohort":cohort, "backfilled":True} for day in days]


def _rule(key, label, value, op, threshold, group, note, unit="%"):
    hit = None if value is None else value <= threshold if op == "≤" else value >= threshold
    return {"key": key, "label": label, "value": value, "op": op, "threshold": threshold,
            "hit": hit, "group": group, "note": note, "unit": unit}


def signal_cards(indices, market, breadth):
    sh, cy = indices.get("sh000001", {}), indices.get("sz399006", {})
    rows = [r for r in indices.values() if r.get("available")]
    volumes = [r["volume250"] for r in rows if r.get("volume250") is not None]
    vol = median(volumes) if volumes else None
    bottom = [
        ("cy_dev", "创业板年线偏离", cy.get("dev250"), "≤", -25, "趋势", "严格使用未四舍五入值"),
        ("cy_dd", "创业板距500日高点回撤", cy.get("drawdown"), "≤", -40, "趋势", "回看近500根收盘价"),
        ("breadth250", "站上MA250个股占比", breadth.get("above_ma250"), "≤", 20, "广度", "分层抽样，不代表全量统计"),
        ("dd52", "个股250日高点回撤中位", breadth.get("dd52_median"), "≤", -35, "广度", "需250根完整个股日线"),
        ("volume", "七指数年量比中位", vol, "≤", .75, "量能", "当日成交量/250日均量；盘中不可与全天比较", "x"),
        ("up", "当日上涨家数占比", market.get("up_ratio"), "≤", 10, "情绪", "有效涨跌幅样本为分母"),
        ("sh_dev", "上证年线偏离", sh.get("dev250"), "≤", -12, "趋势", "价格偏离，不等于估值便宜"),
        ("sh_dd", "上证距500日高点回撤", sh.get("drawdown"), "≤", -18, "趋势", "权重指数的调整深度"),
        ("pb1", "正PB样本破净率", market.get("below_pb1_ratio"), "≥", 10, "估值", "剔除负净资产及缺失PB样本"),
        ("pb", "正PB样本中位数", market.get("pb_median"), "≤", 2.2, "估值", "经验阈值，未进行行业中性化", "倍"),
        ("days", "上证距500日高点交易日", sh.get("down_days"), "≥", 120, "周期", "交易日时长不是预测见底日期", "日"),
        ("rsi", "上证RSI14", sh.get("rsi14"), "≤", 30, "动量", "新增Wilder平滑RSI，与反转确认独立"),
    ]
    top = [
        ("sh_dev", "上证年线正偏离", sh.get("dev250"), "≥", 15, "趋势", "强趋势可长期保持正偏离"),
        ("cy_dev", "创业板年线正偏离", cy.get("dev250"), "≥", 30, "趋势", "识别成长风格过热"),
        ("breadth250", "站上MA250个股占比", breadth.get("above_ma250"), "≥", 85, "广度", "极端普涨状态"),
        ("breadth20", "站上MA20个股占比", breadth.get("above_ma20"), "≥", 90, "广度", "短期拥挤度"),
        ("dd52", "个股250日高点回撤中位", breadth.get("dd52_median"), "≥", -5, "广度", "多数个股接近阶段高点"),
        ("volume", "七指数年量比中位", vol, "≥", 1.8, "量能", "放量需结合价格方向", "x"),
        ("up", "当日上涨家数占比", market.get("up_ratio"), "≥", 90, "情绪", "单日普涨不等于顶部"),
        ("sh_ret", "上证20日涨幅", sh.get("ret20"), "≥", 12, "动量", "识别短期加速"),
        ("cy_ret", "创业板20日涨幅", cy.get("ret20"), "≥", 20, "动量", "识别成长股加速"),
        ("pb", "正PB样本中位数", market.get("pb_median"), "≥", 4, "估值", "经验阈值，未进行行业中性化", "倍"),
        ("pb1", "正PB样本破净率", market.get("below_pb1_ratio"), "≤", 2, "估值", "普遍估值扩张"),
        ("rsi", "上证RSI14", sh.get("rsi14"), "≥", 80, "动量", "超买可持续，不等于立即下跌"),
    ]
    def card(specs):
        rules = [_rule(*r) for r in specs]
        known = sum(r["hit"] is not None for r in rules)
        hit = sum(r["hit"] is True for r in rules)
        return {"rules": rules, "hit": hit, "known": known, "total": len(rules),
                "score": round(hit/known*100, 1) if known >= 8 else None,
                "groups": sorted({r["group"] for r in rules if r["hit"]})}
    return {"bottom": card(bottom), "top": card(top)}


def temperature(indices, market, breadth):
    sh = indices.get("sh000001", {})
    specs = [("短期广度", breadth.get("above_ma20"), .2), ("长期广度", breadth.get("above_ma250"), .15),
             ("当日上涨占比", market.get("up_ratio"), .15), ("RSI14", sh.get("rsi14"), .15),
             ("年线偏离", None if sh.get("dev250") is None else 50+sh["dev250"]*2.5, .15),
             ("新低压力", None if breadth.get("newlow60") is None else 100-breadth["newlow60"]*2, .1),
             ("PB中位数", None if market.get("pb_median") is None else (market["pb_median"]-1.5)/3*100, .1)]
    factors = [{"label": label, "value": max(0, min(100, v)), "weight": w} for label, v, w in specs if v is not None]
    coverage = sum(f["weight"] for f in factors)
    score = round(sum(f["value"]*f["weight"] for f in factors)/coverage, 1) if coverage >= .6 else None
    label = ("数据不足" if score is None else "极度悲观" if score <= 20 else "偏悲观" if score <= 40
             else "中性" if score < 60 else "偏热" if score < 80 else "过热")
    return {"score": score, "label": label, "coverage": round(coverage, 2), "factors": factors}


def confirmation(indices, bars_by_symbol, market, observations, as_of):
    sh, cy = indices.get("sh000001", {}), indices.get("sz399006", {})
    bars = normalize_bars(bars_by_symbol.get("sh000001", []))
    def reversal(up):
        if len(bars) < 7:
            return None
        event, prev, last = bars[-2], bars[-3], bars[-1]
        volumes = [r["volume"] for r in bars[-7:-2]]
        if event["volume"] is None or not all(v is not None for v in volumes) or mean(volumes) <= 0:
            return None
        change = (event["close"]/prev["close"]-1)*100
        return (change >= 2 if up else change <= -2) and event["volume"] >= mean(volumes)*1.3 and (last["close"] >= event["close"] if up else last["close"] <= event["close"])
    def breadth_days(up):
        days = [r["date"] for r in bars[-5:]]
        observed = {r["as_of"]: r for r in observations}
        if len(days) < 5 or any(d not in observed or observed[d].get("above_ma250") is None for d in days):
            return None
        if len({observed[d].get("method") for d in days}) != 1:
            return None
        if len({observed[d].get("cohort") for d in days}) != 1 or any(not observed[d].get("cohort") for d in days):
            return None
        return all(observed[d]["above_ma250"] >= 25 if up else observed[d]["above_ma250"] < 50 for d in days)
    sh_known = sh.get("dev20") is not None and sh.get("ma20_rising") is not None
    cy_known = cy.get("dev250") is not None and cy.get("new_low20") is not None
    limits_known = market.get("limit_down") is not None and market.get("limit_up") is not None
    bottom = [("权重趋势修复", sh["dev20"] > 0 and sh["ma20_rising"] if sh_known else None, "上证重回MA20且MA20高于5日前"),
              ("放量反转并守住", reversal(True), "前一交易日涨≥2%、量较前5日≥1.3倍，最新收盘不回吐"),
              ("跌停压力缓和", market["limit_down"] < 20 and market["limit_up"] >= 50 if limits_known else None, "近跌停<20且近涨停≥50（按板块涨跌幅估计）"),
              ("广度连续修复", breadth_days(True), "同口径MA250广度连续5个交易日≥25%，缺观测为待补数据"),
              ("成长止跌", cy["dev250"] > -20 and not cy["new_low20"] if cy_known else None, "创业板年线偏离>−20%且不创20日收盘新低")]
    top = [("权重趋势转弱", sh["dev20"] < 0 and not sh["ma20_rising"] if sh_known else None, "上证跌破MA20且MA20低于5日前"),
           ("放量下跌未收复", reversal(False), "前一交易日跌≥2%、量较前5日≥1.3倍，最新收盘未收复"),
           ("跌停压力扩散", market["limit_down"] >= 50 and market["limit_up"] < 20 if limits_known else None, "近跌停≥50且近涨停<20（按涨跌幅估计）"),
           ("广度连续转弱", breadth_days(False), "同口径MA250广度连续5个交易日<50%"),
           ("成长破位", cy["dev250"] < 0 and cy["new_low20"] if cy_known else None, "创业板在年线下方且创20日收盘新低")]
    return {name: {"items": [{"label": label, "hit": hit, "note": note} for label, hit, note in items],
                   "hit": sum(hit is True for _, hit, _ in items), "known": sum(hit is not None for _, hit, _ in items)}
            for name, items in (("bottom", bottom), ("top", top))}


def build_health(state):
    indices = {s: index_metrics(s, state.get("bars", {}).get(s, [])) for s in INDEXES}
    dates = [r["as_of"] for r in indices.values() if r.get("available")]
    as_of = indices["sh000001"].get("as_of") or max(dates, default=None)
    same_day = {k: v for k, v in indices.items() if v.get("as_of") == as_of}
    m = state.get("market", {})
    b = state.get("breadth", {})
    market = m.get("values", {}) if m.get("as_of") == as_of else {}
    breadth = b if b.get("as_of") == as_of else {}
    signals = signal_cards(same_day, market, breadth)
    temp = temperature(same_day, market, breadth)
    confirms = confirmation(same_day, state.get("bars", {}), market, state.get("observations", []), as_of)
    bottom, top = signals["bottom"], signals["top"]
    stage = "数据不足"
    if temp["score"] is not None:
        if bottom["score"] is not None and bottom["score"] >= 40:
            stage = "底部特征增多 · 等待反转"
            if confirms["bottom"]["hit"] >= 3:
                stage = "底部反转条件满足 · 继续跟踪"
        elif top["score"] is not None and top["score"] >= 40:
            stage = "顶部特征增多 · 留意转弱"
            if confirms["top"]["hit"] >= 3:
                stage = "高位转弱条件满足 · 继续跟踪"
        else:
            stage = "弱势运行" if temp["score"] <= 40 else "强势运行" if temp["score"] >= 60 else "中性震荡"
    spread = (same_day["sh000852"].get("ret20")-same_day["sh000300"].get("ret20")
              if all(s in same_day and same_day[s].get("ret20") is not None for s in ("sh000852", "sh000300")) else None)
    return {"version": VERSION, "as_of": as_of, "stage": stage, "temperature": temp, "signals": signals,
            "confirmation": confirms, "indices": list(indices.values()), "market": m, "breadth": b,
            "style_spread": spread, "observations": state.get("observations", []), "notes": state.get("notes", []),
            "source": state.get("source", "baseline"), "updated_at": state.get("updated_at"),
            "historical": state.get("historical", {}), "historical_as_of": "2026-10-22",
            "report_url": "/api/market-health/report"}
