"""市场参与度、同组趋势广度与正估值分布；不参与原有评分。"""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import math


TREND_ROWS = (("above_ma20", "站上 MA20"), ("above_ma60", "站上 MA60"),
              ("above_ma250", "站上 MA250"), ("newhigh60", "60 日新高"), ("newlow60", "60 日新低"))


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _count(value):
    value = _number(value)
    return int(value) if value is not None and value >= 0 and value.is_integer() else None


def _date(value):
    text = str(value or "")[:10]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _percent(value):
    value = _number(value)
    return value if value is not None and 0 <= value <= 100 else None


def valuation_distribution(values, *, kind):
    """Finite positive ratios only; missing and nonpositive rows stay separate."""
    values = [_number(v) for v in values]
    positive = sorted(v for v in values if v is not None and v > 0)
    sample, total = len(positive), len(values)
    def quantile(q):
        if not sample:
            return None
        position = (sample - 1) * q
        lower, upper = math.floor(position), math.ceil(position)
        return positive[lower] + (positive[upper] - positive[lower]) * (position - lower)
    edges = (1, 2, 3, 5) if kind == "pb" else (15, 30, 50, 100)
    labels = ("0–1", "1–2", "2–3", "3–5", "5 及以上") if kind == "pb" else ("0–15", "15–30", "30–50", "50–100", "100 及以上")
    buckets = []
    for i, label in enumerate(labels):
        lower, upper = (0 if i == 0 else edges[i-1]), (edges[i] if i < len(edges) else math.inf)
        count = sum(lower <= value < upper for value in positive)
        buckets.append({"label": label, "count": count, "pct": count / sample * 100 if sample else None})
    return {"sample": sample, "total": total, "coverage_pct": sample / total * 100 if total else None,
            "missing": sum(value is None for value in values),
            "nonpositive": sum(value is not None and value <= 0 for value in values),
            "quantiles": {name: quantile(q) for name, q in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9))},
            "buckets": buckets}


def _status(date, as_of, available):
    return "missing" if not available or not date or not as_of or date > as_of else "current" if date == as_of else "stale"


def _participation(health, as_of):
    market = _dict(health.get("market"))
    values = _dict(market.get("values"))
    current = _dict(_dict(health.get("market_internals")).get("current"))
    candidates = []
    for source, row, date in (("internals", current, _date(current.get("as_of"))),
                              ("market", values, _date(market.get("as_of")))):
        counts = [_count(row.get(k)) for k in ("up", "down", "flat")]
        sample = _count(row.get("eligible" if source == "internals" else "change_sample"))
        if not date or not as_of or date > as_of or any(n is None for n in counts) or sample is None or sample <= 0 or sum(counts) != sample:
            continue
        candidates.append((date, source == "internals", source, row, sample, counts))
    chosen = max(candidates, default=None, key=lambda r: (r[0], r[1]))
    date, source, row, sample, counts = (chosen[0], chosen[2], chosen[3], chosen[4], chosen[5]) if chosen else (None, None, {}, None, [None]*3)
    up, down, flat = counts
    denominator = (up + down if source == "internals" else sample) if chosen else None
    ratio = up / denominator * 100 if denominator else None
    median = _number(row.get("median_return_pct" if source == "internals" else "chg_median"))
    weighted = _number(values.get("mv_weighted_chg")) if date and _date(market.get("as_of")) == date else None
    weighted_sample = _count(values.get("weighted_sample")) if weighted is not None else None
    gap = weighted - median if weighted is not None and median is not None else None
    status = _status(date, as_of, chosen is not None)
    label, tone = "参与度待补齐", "unavailable"
    if status == "stale":
        label = "仅历史参与度"
    elif status == "current":
        label, tone = ("上涨家数占优", "positive") if up > down else ("下跌家数占优", "negative") if down > up else ("涨跌家数均衡", "neutral")
    note = ("上涨占比 = 上涨家数 ÷（上涨 + 下跌），平盘不进入该分母；样本数含平盘，剔除无成交或价格不全的记录。"
            if source == "internals" else "回退至全市场正价格快照；上涨占比 = 上涨家数 ÷ 有效涨跌样本，分母包含平盘。" if source else "有效涨跌家数与样本分母尚未取得。")
    if weighted is not None:
        note += " 市值加权涨跌来自正市值样本，与涨跌中位数的样本及权重不同；差值仅作描述，不代表同篮子超额收益。"
        if weighted_sample is None:
            note += "旧快照未记录加权样本数。"
    if status == "stale":
        note += f" 观测日 {date}，不用于 {as_of} 的方向结论。"
    provisional = bool(row.get("provisional")) if source == "internals" else bool(market.get("provisional")) if source else False
    return {"as_of": date, "provisional": provisional, "status": status, "sample": sample,
            "captured_at": row.get("captured_at") if source == "internals" else market.get("captured_at") if source else None,
            "source": row.get("source") if source == "internals" else market.get("source") if source else None,
            "up": up, "down": down, "flat": flat, "up_ratio": ratio, "net_advances": up-down if chosen else None,
            "median_return": median, "weighted_return": weighted, "weighted_sample": weighted_sample,
            "weighted_gap_pp": gap, "label": label, "tone": tone, "note": note}


def _trend(health, as_of, calendar):
    breadth = _dict(health.get("breadth"))
    date = _date(breadth.get("as_of"))
    if not date or not as_of or date > as_of:
        breadth, date = {}, None
    denominators = _dict(breadth.get("denominators"))
    cohort, method = breadth.get("cohort"), breadth.get("method")
    available = any(_percent(breadth.get(key)) is not None and (_count(denominators.get(key)) or 0) > 0 for key, _ in TREND_ROWS)
    status = _status(date, as_of, available)
    sessions = sorted({day for raw in calendar or [] if (day := _date(raw.get("date") if isinstance(raw, dict) else raw))})
    prior = [day for day in sessions if date and day < date]
    previous_date = prior[-1] if prior and date in sessions else None
    history = [r for r in health.get("observations", []) if isinstance(r, dict) and _date(r.get("as_of")) == previous_date] if previous_date else []
    previous = None
    if status != "current":
        reason = "当前趋势观测缺失或日期较旧，不计算本日变化。"
    elif not previous_date:
        reason = "缺少交易日历中的上一交易日，无法确认相邻比较。"
    elif not cohort or not method:
        reason = "当前成分组或计算口径未记录，无法确认可比性。"
    elif not history:
        reason = f"缺少上一实际交易日 {previous_date} 的观测，不跨缺口比较。"
    else:
        closed = [r for r in history if not r.get("provisional")]
        matched = [r for r in closed if r.get("cohort") == cohort and r.get("method") == method]
        previous = matched[-1] if matched else None
        reason = ("上一交易日只有盘中观测，不作为收盘比较基准。" if not closed else
                  "上一交易日成分组或计算口径不同，不拼接比较。" if not matched else
                  "仅比较同成分组、同口径、分母相同的上一实际交易日收盘。")
    previous_denominators = _dict(previous.get("denominators")) if previous else {}
    rows = []
    for key, label in TREND_ROWS:
        denominator = _count(denominators.get(key))
        value = _percent(breadth.get(key)) if denominator and denominator > 0 else None
        previous_value = _percent(previous.get(key)) if previous else None
        comparable = bool(value is not None and previous_value is not None and denominator == _count(previous_denominators.get(key)))
        rows.append({"key": key, "label": label, "value": value, "denominator": denominator,
                     "previous_value": previous_value if comparable else None,
                     "change_pp": value - previous_value if comparable else None, "comparable": comparable})
    if previous and any(not row["comparable"] for row in rows):
        reason += " 部分项目缺值或分母不同，相应变化留空。"
    short, long = rows[0]["value"], rows[2]["value"]
    label, tone = "趋势广度待补齐", "unavailable"
    if status == "stale":
        label = "仅历史趋势广度"
    elif status == "current":
        label, tone = "趋势证据部分可用", "neutral"
        if short is not None and long is not None:
            label, tone = (("短长趋势同步占优", "positive") if short > 50 and long > 50 else
                           ("短长趋势同步承压", "negative") if short < 50 and long < 50 else ("短长趋势分化", "neutral"))
    provisional = bool(breadth.get("provisional"))
    if provisional:
        reason += " 该观测为盘中抽样，尚非收盘确认。"
        if any(row["comparable"] for row in rows):
            reason += "变化对比上一交易日收盘，收盘前仍会变化。"
    return {"as_of": date, "provisional": provisional, "status": status, "sample": _count(breadth.get("sample")),
            "captured_at": breadth.get("captured_at"), "source": breadth.get("source") or method,
            "cohort": cohort, "method": method, "label": label, "tone": tone,
            "note": "趋势广度为抽样股票的各项比例，每项使用自己的有效历史分母；成分组相同仍需核对分母。",
            "previous_date": previous_date, "comparison_note": reason, "rows": rows}


def _valuation(health, as_of):
    market = _dict(health.get("market"))
    date = _date(market.get("as_of"))
    if not date or not as_of or date > as_of:
        market, date = {}, None
    values = _dict(market.get("values"))
    distributions = _dict(values.get("valuation_distribution"))
    total = _count(values.get("total"))
    result = {}
    for key in ("pb", "pe"):
        median = _number(values.get(f"{key}_median"))
        median = median if median is not None and median > 0 else None
        sample = _count(values.get(f"{key}_sample"))
        distribution = deepcopy(distributions.get(key)) if isinstance(distributions.get(key), dict) else None
        result[key] = {"median": median, "sample": sample, "coverage_pct": sample / total * 100 if sample is not None and total else None,
                       "distribution": distribution}
    available = any(row["median"] is not None for row in result.values())
    status = _status(date, as_of, available)
    note = "仅统计正 PB/PE；缺失和非正值不纳入估值分布。分桶占比以该估值有效样本为分母，覆盖率以正价格股票总数为分母；破净率以正 PB 样本为分母。"
    if any(row["distribution"] is None for row in result.values()):
        note += "旧快照未记录估值分布，保留已有中位数；更新全市场数据后可补充分布。"
    if status == "stale":
        note += f" 观测日 {date}，不将旧估值当作 {as_of} 的当前水平。"
    return {"as_of": date, "provisional": bool(market.get("provisional")), "status": status, "total": total,
            "captured_at": market.get("captured_at"), "source": market.get("source"),
            **result, "below_pb1_ratio": _percent(values.get("below_pb1_ratio")), "note": note}


def build_breadth_report(health, calendar=None):
    health = _dict(health)
    as_of = _date(health.get("as_of"))
    participation = _participation(health, as_of)
    trend = _trend(health, as_of, calendar)
    valuation = _valuation(health, as_of)
    current = sum(row["status"] == "current" for row in (participation, trend, valuation))
    title = "广度与估值证据待更新" if not current else "广度与估值部分可用" if current < 3 else "参与度、趋势与估值交叉观察"
    provisional = bool(health.get("provisional"))
    return {"as_of": as_of, "provisional": provisional,
            "summary": {"title": title, "text": f"{'盘中' if provisional else '收盘'}观察 {as_of or '未取得'}；{current}/3 个模块日期与观察日一致。上涨家数、趋势抽样和估值覆盖分别展示，不混用分母。"},
            "participation": participation, "trend": trend, "valuation": valuation,
            "notes": ["每个模块保留自己的观测日期；较旧数据只作历史展示，不生成当前方向结论。",
                      "趋势变化只比较上一实际交易日的同组、同口径、同分母收盘观测；不跨缺口或混入历史盘中抽样。",
                      "估值分布描述已采集股票的横截面，不代表历史低估概率；本报告不修改原市场温度或顶底信号。"]}
