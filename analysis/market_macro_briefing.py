"""宏观观测简报：按频率比较实际值，独立评估快照与查看日的时效。"""
from __future__ import annotations

from copy import deepcopy
import datetime as dt
import math


GROUPS = [
    {"key": "growth", "name": "增长与物价"},
    {"key": "liquidity", "name": "货币与信用"},
    {"key": "external", "name": "海外利率与汇率"},
    {"key": "fiscal", "name": "财政与地方债"},
    {"key": "valuation", "name": "股债估值"},
]
GROUP_KEYS = {
    "growth": ("pmi", "cpi", "ppi", "gdp"),
    "liquidity": ("lpr1y", "lpr5y", "shibor3m", "m1", "m2", "social", "social_stock"),
    "external": ("fed_upper", "fed_lower", "us10y", "real10y", "usd", "usdcny", "gold"),
    "fiscal": ("spending", "local_new", "local_refinance"),
    "valuation": ("equity_bond_spread",),
}
INTERPRETATIONS = {
    "pmi": "50 为制造业扩张与收缩的分界；方向与所在区间需分别观察。",
    "cpi": "居民消费价格同比；同比回升并不单独证明需求改善。",
    "ppi": "工业生产者价格同比；同时关注销量、成本与利润率。",
    "gdp": "当季同比增速，仅作季频环境观察，不参与日/月频联动。",
    "lpr1y": "贷款报价利率，实际融资成本还取决于加点和信用需求。",
    "lpr5y": "长期贷款报价；报价下调并不代表地产销售已经改善。",
    "shibor3m": "银行间资金价格；回落不等同于资金进入股票市场。",
    "m1": "货币活化线索；2025 年口径调整前后不直接比较。",
    "m2": "广义货币同比；结合社融和实体需求观察传导。",
    "social": "当月流量，存在季节性；单月增加不直接等于信用趋势改善。",
    "social_stock": "社融存量同比；较上月变化描述信用扩张速度。",
    "fed_upper": "政策目标区间上限，不代表期货隐含的降息概率。",
    "fed_lower": "政策目标区间下限，不代表市场有效利率或降息概率。",
    "us10y": "名义美债收益率；通胀、增长与期限溢价均可能影响它。",
    "real10y": "实际利率变化可影响折现与黄金持有成本，需结合增长验证。",
    "usd": "美元广义贸易加权指数，非 DXY；上升表示该口径美元走强。",
    "usdcny": "人民币/美元；上升表示人民币对美元贬值，企业影响各异。",
    "gold": "人民币/克，含汇率和境内溢价，不等同美元现货黄金。",
    "spending": "年内累计支出同比；不等于当月同比，跨年不比较增速变化。",
    "local_new": "当月新增债券发行，发行到拨付、项目和订单仍有时滞。",
    "local_refinance": "当月再融资发行，用于存量债务，不等同新增需求。",
    "equity_bond_spread": "同日盈利收益率减国债收益率；利差扩大不自动构成买入信号。",
}
WATCH = {
    "growth": ["用新订单、销量与利润验证需求，而非仅看价格。", "PMI 与物价若不在同一统计期，先核对各自日期。"],
    "liquidity": ["观察利率变化是否传导至社融存量、M1 与企业订单。", "不把货币投放或社融流量视作股市净流入。"],
    "external": ["交叉验证实际利率、美元与人民币的各自观测日。", "政策降息也可能反映增长走弱，需核对盈利预期。"],
    "fiscal": ["跟踪新增发行、拨付和项目订单，区分再融资用途。", "累计支出同比与当月债券发行不能直接相加。"],
}


def _date(value):
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _period_serial(date, frequency):
    day = dt.date.fromisoformat(date)
    return day.year * (4 if frequency == "quarterly" else 12) + ((day.month - 1) // 3 if frequency == "quarterly" else day.month - 1)


def _rows(raw, spec, cutoff):
    rows = {}
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        date, value = _date(row.get("date")), _number(row.get("value"))
        published = _date(row.get("published_on"))
        if not date or date > cutoff or value is None:
            continue
        if row.get("published_on") and (not published or published > cutoff):
            continue
        if spec["kind"] == "price" and value <= 0:
            continue
        frequency = spec["frequency"]
        period = date[:7] if frequency == "monthly" else f"{date[:4]}Q{(int(date[5:7])-1)//3+1}" if frequency == "quarterly" else None
        item = {**row, "date": date, "period": period, "value": value, "published_on": published}
        key = date if spec["frequency"] == "daily" else _period_serial(date, spec["frequency"])
        previous = rows.get(key)
        if not previous or (published or "", date) >= (previous.get("published_on") or "", previous["date"]):
            rows[key] = item
    return sorted(rows.values(), key=lambda r: r["date"])


def _comparison(key, spec, rows):
    frequency, kind = spec["frequency"], spec["kind"]
    unit = "%" if kind == "price" else "bp" if kind == "rate" and frequency == "daily" else "百分点" if kind == "rate" or key == "pmi" else spec["unit"]
    if len(rows) < 2:
        return None, unit, "缺少可比前值"
    last, previous = rows[-1], rows[-2]
    if frequency != "daily" and _period_serial(last["date"], frequency) - _period_serial(previous["date"], frequency) != 1:
        return None, unit, "缺少相邻统计期"
    if frequency == "daily" and (dt.date.fromisoformat(last["date"]) - dt.date.fromisoformat(previous["date"])).days > (45 if key.startswith("lpr") else 10):
        return None, unit, "前次观测间隔过长"
    if key == "m1" and previous["date"] < "2025-01-01" <= last["date"]:
        return None, unit, "跨 M1 统计口径，不比较"
    if key == "spending" and previous["date"][:4] != last["date"][:4]:
        return None, unit, "累计口径跨年，不比较"
    change = ((last["value"] / previous["value"] - 1) * 100 if kind == "price" else
              (last["value"] - previous["value"]) * (100 if kind == "rate" and frequency == "daily" else 1))
    if not math.isfinite(change):
        return None, unit, "变化值不可计算"
    return round(change, 8), unit, "较前次观测" if frequency == "daily" else "较上季" if frequency == "quarterly" else "较上月"


def _evidence(row):
    stamp = row["period"] or row["date"]
    change = row["change"]
    digits = 4 if row["key"] == "usdcny" else 2
    def display(value, signed=False):
        rounded = round(value, digits)
        rounded = 0 if rounded == 0 else rounded
        return format(rounded, f"{'+' if signed else ''}.{digits}f").rstrip("0").rstrip(".")
    detail = f"；{row['change_label']} {display(change, True)}{row['change_unit']}" if change is not None else f"；{row['change_label']}"
    return f"{row['name']} {display(row['value'])}{row['unit']}（{stamp}）{detail}"


def _dimensions(indicators):
    lookup = {r["key"]: r for r in indicators}
    result = []
    primary = {"growth": "pmi", "liquidity": "social_stock", "external": "real10y", "fiscal": "spending"}
    for group in GROUPS[:4]:
        key = group["key"]
        rows = [lookup[k] for k in GROUP_KEYS[key] if k in lookup]
        fresh = [r for r in rows if r["status"] == "fresh"]
        point = lookup.get(primary[key])
        if point and point["status"] == "fresh":
            fresh = [point, *(r for r in fresh if r["key"] != point["key"])]
        label, tone = ("关键证据待齐", "neutral") if fresh else ("暂无时效内证据", "unavailable")
        summary = "补齐关键指标后再判断该方向；其他指标见下方观测。" if fresh else "已有历史值保留在指标库，缺失或超过观察阈值的数据不用于本栏判断。"
        if point and point["status"] == "fresh":
            value, delta = point["value"], point["change"]
            if key == "growth":
                label = "制造业扩张区间" if value > 50 else "制造业收缩区间" if value < 50 else "制造业临界点"
                tone = "positive" if value > 50 else "negative" if value < 50 else "neutral"
                summary = "PMI 的 50 分界描述制造业景气区间；改善或走弱需同时看较上月变化，不能由物价单独确认。"
            elif delta is not None:
                if key == "liquidity":
                    label = "信用增速回升" if delta > 0 else "信用增速放缓" if delta < 0 else "信用增速持平"
                    tone = "positive" if delta > 0 else "negative" if delta < 0 else "neutral"
                    summary = "社融存量同比的相邻月变化描述信用扩张速度，资金是否传导至需求仍待订单与盈利验证。"
                elif key == "external":
                    label = "实际利率上行" if delta > 0 else "实际利率回落" if delta < 0 else "实际利率持平"
                    tone = "neutral"
                    summary = "只描述最近两次实际利率观测；美元、人民币与黄金各自保留日期，不把不同日期合成为风险偏好总分。"
                else:
                    label = "累计支出增速回升" if delta > 0 else "累计支出增速放缓" if delta < 0 else "累计支出增速持平"
                    tone = "positive" if delta > 0 else "negative" if delta < 0 else "neutral"
                    summary = "同年相邻统计期累计同比的变化，不代表当月支出增速；需跟踪拨付与项目执行。"
            else:
                label, tone = "仅有水平观测", "neutral"
                summary = f"{point['name']}有观测，但{point['change_label']}，暂不判断变化方向。"
        result.append({**group, "label": label, "tone": tone, "summary": summary,
                       "evidence": [_evidence(r) for r in fresh[:4]], "watch": list(WATCH[key]),
                       "factor_keys": [r["key"] for r in rows]})
    return result


def reassess_briefing(briefing, assessed_on):
    """Re-evaluate freshness outside the snapshot cache without touching its values."""
    result = deepcopy(briefing)
    assessed_on = _date(assessed_on) or result["as_of"]
    result["assessed_on"] = assessed_on
    coverage = {"total": len(result["indicators"]), "available": 0, "fresh": 0, "stale": 0, "missing": 0}
    for row in result["indicators"]:
        date = row["date"]
        # A caller replaying an earlier date cannot make a future observation current.
        if date and (date > assessed_on or (row["published_on"] and row["published_on"] > assessed_on)):
            row.update(value=None, date=None, period=None, published_on=None, previous_value=None,
                       previous_date=None, change=None, change_label="观测超出查看日期")
            date = None
        age = (dt.date.fromisoformat(assessed_on) - dt.date.fromisoformat(date)).days if date else None
        status = "missing" if row["value"] is None else "stale" if age > row["max_age_days"] else "fresh"
        row.update(age_days=age, status=status, status_label={"fresh": "观察窗口内", "stale": "仅历史观测", "missing": "暂无观测"}[status])
        coverage[status] += 1
        coverage["available"] += status != "missing"
    result["coverage"] = coverage
    result["dimensions"] = _dimensions(result["indicators"])
    watchlist = []
    for status in ("stale", "missing"):
        rows = [r for r in result["indicators"] if r["status"] == status]
        if rows:
            watchlist.append({"key": status, "title": f"{len(rows)} 项{'历史观测待更新' if status == 'stale' else '指标待补齐'}",
                              "reason": "、".join(r["name"] for r in rows), "factor_key": rows[0]["key"]})
    fresh = {r["key"]: r for r in result["indicators"] if r["status"] == "fresh"}
    for key, row in fresh.items():
        if row["change"] is None and row["previous_date"]:
            watchlist.append({"key": f"comparison:{key}", "title": f"{row['name']}暂不比较变化",
                              "reason": row["change_label"], "factor_key": key})
    for group in ("growth", "liquidity", "fiscal"):
        rows = [fresh[k] for k in GROUP_KEYS[group] if k in fresh and fresh[k]["frequency"] == "monthly"]
        if len({_period_serial(r["date"], "monthly") for r in rows}) > 1:
            watchlist.append({"key": f"period:{group}", "title": f"{next(g['name'] for g in GROUPS if g['key'] == group)}统计期未齐",
                              "reason": "各项保留原统计期，不将不同月份视为同月共振。", "factor_key": rows[0]["key"]})
    unknown = [r for r in fresh.values() if r["frequency"] != "daily" and not r["published_on"]]
    if unknown:
        watchlist.append({"key": "publication", "title": "部分原始发布日期未知",
                          "reason": "统计期不代表当时已披露；这些观测不能用于还原可交易时点。", "factor_key": unknown[0]["key"]})
    result["watchlist"] = watchlist
    title = "宏观证据待更新" if not coverage["fresh"] else "宏观证据部分可用" if coverage["missing"] or coverage["stale"] else "宏观观测已覆盖"
    result["summary"] = {"title": title, "text": f"快照 {result['as_of']}；按 {assessed_on} 评估，{coverage['fresh']}/{coverage['total']} 项处于观察窗口内。分栏描述增长、信用、海外与财政，不合成买卖评分。"}
    return result


def build_briefing(series, specs, as_of, *, assessed_on=None):
    as_of = _date(as_of)
    if not as_of:
        raise ValueError("宏观简报缺少有效快照日期")
    assessed_on = _date(assessed_on) or as_of
    cutoff = min(as_of, assessed_on)
    indicators = []
    for key, spec in specs.items():
        rows = _rows(series.get(key), spec, cutoff)
        last, previous = (rows[-1] if rows else {}), (rows[-2] if len(rows) > 1 else {})
        change, unit, label = _comparison(key, spec, rows)
        frequency = spec["frequency"]
        max_age = 45 if key.startswith("lpr") else 90 if key in GROUP_KEYS["fiscal"] else {"daily": 10, "monthly": 75, "quarterly": 150}[frequency]
        indicators.append({"key": key, "name": spec["name"], "unit": spec["unit"], "kind": spec["kind"],
                           "frequency": frequency, "group": next((g for g, keys in GROUP_KEYS.items() if key in keys), "growth"),
                           "value": last.get("value"), "date": last.get("date"), "period": last.get("period"),
                           "published_on": last.get("published_on"), "previous_value": previous.get("value"), "previous_date": previous.get("date"),
                           "change": change, "change_unit": unit, "change_label": label, "age_days": None,
                           "max_age_days": max_age, "source": spec.get("source"), "url": last.get("url") or spec.get("url"),
                           "interpretation": INTERPRETATIONS.get(key, "保留实际观测与统计期，需结合其他证据验证。")})
    return reassess_briefing({"as_of": as_of, "indicators": indicators, "groups": deepcopy(GROUPS), "notes": [
        "观察窗口是产品的时效提示阈值，不是法定披露截止日：日频 10 天、LPR 45 天、月频 75 天、财政 90 天、季频 150 天，均为自然日。",
        "时效按查看日与观测日的距离评估；历史值不改日期，缺失不补零。未知原始发布日期不表示当时已知。",
        "变化仅比较相邻统计期或间隔合理的两次实际观测；月/季同比的差为百分点，日频利率差为 bp，价格差为普通涨跌幅。",
        "不同统计期与来源的数据不合成共振评分；颜色描述经济方向，不代表股票买卖建议。",
    ]}, assessed_on)
