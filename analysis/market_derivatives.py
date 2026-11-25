"""股指期货席位多空变化与同合约行情；不把对冲持仓解释为买卖指令。"""
from __future__ import annotations

import datetime as dt
import re

from analysis.market_health import number


PRODUCTS = (("IF", "沪深300", "sh000300"), ("IH", "上证50", "sh000016"),
            ("IC", "中证500", "sh000905"), ("IM", "中证1000", "sh000852"))


def _date(value):
    value = str(value or "")[:10]
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _status(date, as_of, calendar):
    if not date or not as_of or date > as_of:
        return "unavailable"
    if date == as_of:
        return "current"
    previous = [day for day in calendar if day < as_of]
    return "lagged" if previous and date == previous[-1] else "stale"


def _nonnegative(value):
    value = number(value)
    return value if value is not None and value >= 0 else None


def _position(data, variety, as_of):
    rows = [row for row in data.get("positions", []) if row.get("variety") == variety
            and (date := _date(row.get("date"))) and as_of and date <= as_of]
    return max(rows, key=lambda row: _date(row["date"]), default={})


def _quote(product, variety, as_of):
    def valid(row):
        return (re.fullmatch(rf"{variety}\d{{4}}", str(row.get("symbol") or ""))
                and (date := _date(row.get("tradedate"))) and as_of and date <= as_of
                and (number(row.get("price")) or 0) > 0)

    main = product.get("main") or {}
    contracts = [row for row in product.get("contracts", []) if valid(row)]
    if valid(main):
        latest_date = max((_date(row["tradedate"]) for row in contracts), default=None)
        if not latest_date or _date(main["tradedate"]) >= latest_date:
            return main
    return max(contracts, key=lambda row: (_date(row["tradedate"]), number(row.get("volume")) or 0), default={})


def _action(long_change, short_change):
    if long_change is None or short_change is None:
        return "多空变化未齐"
    return "、".join(("多单增仓" if long_change > 0 else "多单减仓" if long_change < 0 else "多单持平",
                     "空单增仓" if short_change > 0 else "空单减仓" if short_change < 0 else "空单持平"))


def _product(data, spec, as_of, calendar):
    variety, name, index_code = spec
    position = _position(data, variety, as_of)
    summary = (position.get("aggregate") or {}).get("summary") or {}
    position_date = _date(position.get("date"))
    position_status = _status(position_date, as_of, calendar)
    long_, short = _nonnegative(summary.get("long_total")), _nonnegative(summary.get("short_total"))
    long_change, short_change = number(summary.get("long_chg")), number(summary.get("short_chg"))
    totals_valid = long_ is not None and short is not None and long_ + short > 0
    net = long_ - short if long_ is not None and short is not None else None
    net_change = long_change - short_change if long_change is not None and short_change is not None else None
    direction, label = "unavailable", "证据不足"
    reason = "多空持仓及增减字段未齐，暂不判断方向"
    if position_status == "stale":
        label, reason = "历史观测", "席位数据早于上一交易日，仅展示历史值"
    elif position_status == "unavailable":
        reason = "尚无所选观察日及之前的席位数据"
    elif totals_valid and net_change is not None:
        direction = "bullish" if net_change > 0 else "bearish" if net_change < 0 else "neutral"
        label = {"bullish": "净变化偏多", "bearish": "净变化偏空", "neutral": "净变化持平"}[direction]
        reason = f"多单增减 − 空单增减 = {net_change:+,.0f} 手；反映披露席位的净变化"

    product = next((p for p in (data.get("quotes") or {}).get("products", []) if p.get("variety") == variety), {})
    main = _quote(product, variety, as_of)
    quote_date = _date(main.get("tradedate"))
    quote_status = _status(quote_date, as_of, calendar)
    price = number(main.get("price"))
    oi = _nonnegative(main.get("position"))
    basis = number(main.get("verified_basis_pct"))
    previous_date = max((day for day in calendar if quote_date and day < quote_date), default=None)
    previous_product = next((p for p in (data.get("previous_quotes") or {}).get("products", []) if p.get("variety") == variety), {})
    old_rows = [previous_product.get("main") or {}, *previous_product.get("contracts", [])]
    old = next((row for row in old_rows if main and row.get("symbol") == main.get("symbol")
                and previous_date and _date(row.get("tradedate")) == previous_date), {})
    old_price, old_oi, old_basis = number(old.get("price")), _nonnegative(old.get("position")), number(old.get("verified_basis_pct"))
    price_change = (price / old_price - 1) * 100 if price is not None and old_price and old_price > 0 else None
    oi_change = oi - old_oi if oi is not None and old_oi is not None else None
    basis_change = basis - old_basis if basis is not None and old_basis is not None else None
    if price_change is None or oi_change is None:
        price_position_label = "缺少同合约相邻日观测"
    else:
        price_position_label = ("价涨" if price_change > 0 else "价跌" if price_change < 0 else "价格持平") + " / " + ("仓增" if oi_change > 0 else "仓减" if oi_change < 0 else "持仓持平")
    tick = str(main.get("ticktime") or "")
    quote_at = main.get("quote_at") or (f"{quote_date}T{tick}" if quote_date and ":" in tick else None)
    position_source = {"cffex": "中金所持仓排名", "tushare": "TuShare / 中金所持仓排名"}.get(position.get("source"))
    if position and not position_source:
        position_source = "现有席位缓存（来源未标记）"
    quote_source = main.get("source") or ("现有期货行情" if main else None)
    source = "；".join(filter(None, (position_source, quote_source))) or "尚未取得"
    return {"variety": variety, "name": name, "index_code": index_code,
            "position_date": position_date, "position_status": position_status,
            "direction": direction, "direction_label": label, "reason": reason,
            "action_label": _action(long_change, short_change),
            "long": long_, "short": short, "long_change": long_change, "short_change": short_change,
            "net": net, "net_change": net_change,
            "ls_ratio": long_ / short if totals_valid and short else None,
            "long_share_pct": long_ / (long_ + short) * 100 if totals_valid else None,
            "quote_date": quote_date, "quote_at": quote_at, "quote_status": quote_status,
            "contract": main.get("symbol"), "price": price, "price_change_pct": price_change,
            "open_interest": oi, "oi_change": oi_change, "price_position_label": price_position_label,
            "basis_pct": basis, "basis_change": basis_change, "previous_date": previous_date if old else None,
            "source": source}


def build_derivatives(data, as_of, calendar):
    """独立于七维压力分，只比较同日可用席位，不跨指数加总手数。"""
    data = data or {}
    as_of = _date(as_of)
    calendar = sorted({day for value in calendar or [] if (day := _date(value)) and as_of and day <= as_of})
    products = [_product(data, spec, as_of, calendar) for spec in PRODUCTS]
    available = [p for p in products if p["direction"] != "unavailable"]
    date = max((p["position_date"] for p in available), default=None)
    common = [p for p in available if p["position_date"] == date]
    counts = {key: sum(p["direction"] == key for p in common) for key in ("bullish", "bearish", "neutral")}
    eligible = len(common)
    if eligible < 2:
        label, tone = "同日证据不足", "unavailable"
    elif counts["bullish"] == eligible:
        label, tone = "席位净变化一致偏多", "bullish"
    elif counts["bearish"] == eligible:
        label, tone = "席位净变化一致偏空", "bearish"
    elif counts["neutral"] == eligible:
        label, tone = "席位净变化持平", "neutral"
    else:
        label, tone = "品种间多空分歧", "neutral"
    reason = (f"{date} 同日有效 {eligible}/4 个品种；偏多 {counts['bullish']}、偏空 {counts['bearish']}、持平 {counts['neutral']}"
              if date else "等待至少两个品种的同日多空持仓与增减数据")
    notes = ["持仓为各合约前20名多头榜、空头榜的合计，两个榜单会员可能不同；不是全市场净持仓或多空资金金额。",
             "净持仓变化 = 多单增减 − 空单增减；正负只描述席位变化，不证明主动做多、裸空或未来涨跌。空头可能用于套期保值。",
             "升贴水 = 期货/同日已核验现货 − 1；未做分红和期限调整，不将贴水直接判为看空。价仓变化使用同合约相邻交易日观测，盘中值不作收盘确认。",
             "当前覆盖 IF / IH / IC / IM 股指期货。期权 PCR、隐含波动率与期限结构尚未接入，不参与判断。"]
    if len(common) < len(available):
        notes.append("品种席位日期不一致：总览仅比较最新有效席位日，较早日期单列展示，不合并计数。")
    if any(p["position_status"] == "lagged" for p in common):
        notes.append("总览使用上一交易日已披露席位；盘中行情与盘后持仓的日期分别标记。")
    errors = [str(error) for error in data.get("errors", []) if error]
    if data.get("error"):
        errors.append(str(data["error"]))
    for row in [*data.get("positions", []), *(data.get("quotes") or {}).get("products", [])]:
        if row.get("error"):
            errors.append(f"{row.get('variety') or '期货'}：{row['error']}")
    return {"as_of": as_of,
            "summary": {"label": label, "tone": tone, "reason": reason, "date": date,
                        **counts, "eligible": eligible, "missing": len(PRODUCTS) - eligible, "total": len(PRODUCTS)},
            "products": products, "notes": notes, "errors": list(dict.fromkeys(errors))}
