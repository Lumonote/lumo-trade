"""两融压力测算与资金归因。金额统一为元，缺失不补零，代理不代表账户身份。"""
from __future__ import annotations

from collections import defaultdict
import re
from statistics import median

from analysis.market_health import number
from analysis.market_health_fusion import iso, margin_metrics


def text(value, fallback=""):
    """分类/代码须为有效文本；旧缓存中的NaN、数值和空串不参与文本排序。"""
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return fallback if value.lower() in ("", "nan", "none", "null", "nat", "<na>", "—", "--", "未分类", "名称未取得", "行业未取得", "跟踪指数未取得") else value


def security_code(value):
    code = text(value).upper()
    if re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", code):
        code = f"{code[2:]}.{code[:2]}"
    if re.fullmatch(r"\d{6}", code):
        suffix = "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "30")) else "BJ" if code.startswith(("4", "8", "92")) else None
        if suffix:
            code += "." + suffix
    return code


def is_a_stock(code):
    return bool(re.fullmatch(r"(?:6\d{5}\.SH|(?:00|30)\d{4}\.SZ|(?:[48]\d{5}|92\d{4})\.BJ)", code))


def security_name(value, code, fallback="名称未取得"):
    name = text(value)
    return name if name and name not in (code, code.split(".")[0]) else fallback


def total(rows, key):
    values = [number(r.get(key)) for r in rows]
    return sum(values) if values and all(v is not None for v in values) else None


def ratio(a, b):
    return a / b * 100 if a is not None and b is not None and b > 0 else None


def dated(rows, date):
    return [r for r in rows if iso(r.get("trade_date")) == date]


def indexed(rows):
    return {(iso(r.get("trade_date")), text(r.get("ts_code"))): r for r in rows if iso(r.get("trade_date")) and text(r.get("ts_code"))}


def window_rows(index, code, dates, n):
    wanted = dates[-n:]
    return [index[(d, code)] for d in wanted] if len(wanted) == n and all((d, code) in index for d in wanted) else []


def stock_meta(bundle):
    meta = {security_code(r.get("ts_code")): {**r, "ts_code": security_code(r.get("ts_code")), "name": security_name(r.get("name"), security_code(r.get("ts_code"))),
                                             "industry": text(r.get("industry"), "未分类"), "market": text(r.get("market"), "未分类")}
            for r in bundle.get("stocks", []) if is_a_stock(security_code(r.get("ts_code")))}
    for row in bundle.get("quant", []):
        code = security_code(row.get("code"))
        if code in meta:
            meta[code]["name"] = text(meta[code]["name"], security_name(row.get("name"), code))
            meta[code]["industry"] = text(meta[code]["industry"], text(row.get("sector"), text(row.get("industry"), "未分类")))
    if any(not text(r["name"]) for r in meta.values()):
        for key in ("flows", "details"):
            for row in bundle.get(key, []):
                code = security_code(row.get("ts_code"))
                if code in meta and not text(meta[code]["name"]):
                    meta[code]["name"] = security_name(row.get("name"), code)
    return meta


def margin_report(bundle, *, window=20, deposit=80, warning=150, closeout=130):
    """模型仅假设同一证券担保、债务固定，无补仓/利息；不是实际担保率或平仓预测。"""
    summary = margin_metrics(bundle.get("margin", []))
    as_of = bundle.get("as_of")
    dates = [d for d in bundle.get("calendar", []) if d <= (as_of or "")]
    details = indexed(bundle.get("details", []))
    prices = indexed(bundle.get("prices", []))
    basics = indexed(bundle.get("basics", []))
    factors = indexed(bundle.get("factors", []))
    meta = stock_meta(bundle)
    rows = []
    for raw in dated(list(details.values()), as_of):
        code = raw.get("ts_code")
        info = meta.get(code)
        # 股票白名单避免ETF、债券、B股混入个股风险分母。
        if not info or not str(code).endswith((".SH", ".SZ")):
            continue
        price, basic = prices.get((as_of, code), {}), basics.get((as_of, code), {})
        row = {"code": code, "name": info.get("name") or raw.get("name") or code,
               "sector": info.get("industry") or "未分类", "as_of": as_of,
               "board": info.get("market") or "未分类", "close": number(price.get("close")),
               "pct": number(price.get("pct_chg")), "turnover": number(basic.get("turnover_rate"))}
        for key in ("rzye", "rzmre", "rzche", "rqye", "rqyl", "rqmcl", "rqchl", "rzrqye"):
            value = number(raw.get(key))
            row[key] = value if value is not None and value >= 0 else None
        row["net"] = row["rzmre"] - row["rzche"] if row["rzmre"] is not None and row["rzche"] is not None else None
        row["float_ratio"] = ratio(row["rzye"], (number(basic.get("circ_mv")) or 0) * 1e4)
        amount = number(price.get("amount"))
        row["buy_ratio"] = ratio(row["rzmre"], amount * 1e3 if amount is not None else None)
        row["cover_days"] = row["rzye"] / (amount * 1e3) if row["rzye"] is not None and amount and amount > 0 else None
        for n in (5, 10, 20):
            history = window_rows(details, code, dates, n)
            buys, repays = total(history, "rzmre"), total(history, "rzche")
            row[f"net{n}"] = buys - repays if buys is not None and repays is not None else None
        previous = details.get((dates[-2], code), {}) if len(dates) >= 2 else {}
        old = number(previous.get("rzye"))
        row["balance_change"] = row["rzye"] - old if row["rzye"] is not None and old is not None else None
        row["adjustment"] = row["balance_change"] - row["net"] if row["balance_change"] is not None and row["net"] is not None else None
        samples = window_rows(details, code, dates, window)
        buy_sum, shares, valid = 0, 0, bool(samples)
        for sample in samples:
            p = prices.get((iso(sample.get("trade_date")), code), {})
            buy, volume, turnover = number(sample.get("rzmre")), number(p.get("vol")), number(p.get("amount"))
            if buy is None or buy < 0 or volume is None or turnover is None or volume <= 0 or turnover <= 0:
                valid = False
                break
            # daily: amount千元、vol手；成交均价元/股。融资买入折股是成本代理。
            vwap = turnover * 1000 / (volume * 100)
            buy_sum += buy
            shares += buy / vwap
        # 窗口内除权/拆并股时不混用不同股价尺度。
        factor_values = [number(factors.get((d, code), {}).get("adj_factor")) for d in dates[-window:]]
        adjusted = len(factor_values) == window and all(v is not None and v > 0 for v in factor_values)
        same_scale = adjusted and max(factor_values) == min(factor_values)
        cost = buy_sum / shares if valid and same_scale and shares > 0 else None
        row["cost"] = cost
        row["model_ratio"] = (1 + deposit / 100) * row["close"] / cost * 100 if cost and row["close"] and row["close"] > 0 else None
        row["model_price"] = cost * (closeout / 100) / (1 + deposit / 100) if cost else None
        row["buffer"] = (1 - closeout / row["model_ratio"]) * 100 if row["model_ratio"] else None
        r = row["model_ratio"]
        row["risk"] = "模型不可用" if r is None else "模型低于平仓线" if r < closeout else "模型预警区" if r < warning else "模型缓冲区"
        row["flags"] = [label for condition, label in (
            (row["float_ratio"] is not None and row["float_ratio"] >= 10, "融资占流通市值≥10%"),
            (row["buy_ratio"] is not None and row["buy_ratio"] >= 20, "融资买入占成交≥20%"),
            (row["net5"] is not None and row["net5"] < 0, "五日融资净偿还"),
            (row["cover_days"] is not None and row["cover_days"] >= 5, "余额/单日成交≥5倍"),
            (row["adjustment"] is not None and abs(row["adjustment"]) > max(1, (old or 0) * 1e-4), "余额存在非交易调整"),
        ) if condition]
        rows.append(row)
    valid = [r for r in rows if r["model_ratio"] is not None and r["rzye"] is not None]
    exposure = total(valid, "rzye")
    distribution = []
    for low, high, label in ((0, 110, "<110%"), (110, 130, "110–130%"), (130, 150, "130–150%"), (150, 180, "150–180%"), (180, float("inf"), "≥180%")):
        group = [r for r in valid if low <= r["model_ratio"] < high]
        balance = sum(r["rzye"] for r in group)
        distribution.append({"label": label, "count": len(group), "balance": balance, "share": ratio(balance, exposure)})
    stress = []
    for drop in (0, 3, 5, 8, 10, 15, 20):
        item = {"drop": drop}
        for key, threshold in (("closeout", closeout), ("warning", warning)):
            group = [r for r in valid if r["model_ratio"] * (1 - drop / 100) < threshold]
            item[key + "_count"] = len(group)
            item[key + "_balance"] = sum(r["rzye"] for r in group)
            item[key + "_share"] = ratio(item[key + "_balance"], exposure)
        stress.append(item)
    sectors = []
    for sector in sorted({r["sector"] for r in rows}):
        group = [r for r in rows if r["sector"] == sector]
        sectors.append({"name": sector, "count": len(group), **{k: total(group, k) for k in ("rzye", "rqye", "net", "net5", "net20")},
                        "warning_count": sum(r["model_ratio"] is not None and r["model_ratio"] < warning for r in group),
                        "model_covered": sum(r["model_ratio"] is not None for r in group)})
    latest = next((r for r in reversed(summary) if r["date"] == as_of), {})
    return {"as_of": as_of, "summary": latest, "history": summary, "rows": sorted(rows, key=lambda r: r["model_ratio"] if r["model_ratio"] is not None else float("inf")),
            "sectors": sorted(sectors, key=lambda r: r["rzye"] or 0, reverse=True), "distribution": distribution, "stress": stress,
            "coverage": {"stocks": len(rows), "model": len(valid), "model_balance": exposure, "stock_balance": total(rows, "rzye"),
                         "median_ratio": median(r["model_ratio"] for r in valid) if valid else None},
            "parameters": {"window": window, "deposit": deposit, "warning": warning, "closeout": closeout},
            "model_note": "融资买入额按每日成交均价折股，得到窗口买入加权成本；不代表存量融资持仓成本。模型担保比例=(1+保证金比例)×同日股价/代理成本。假设只持该证券、固定债务、无利息和补仓；130%/150%仅为可调情景线，并非所有券商的统一要求。缺日、停牌或窗口内复权因子变化时不测算。"}


def flow_rows(bundle):
    meta = stock_meta(bundle)
    result = []
    for raw in indexed(bundle.get("flows", [])).values():
        code, date = raw["ts_code"], iso(raw["trade_date"])
        if code not in meta:
            continue
        row = {"code": code, "date": date, "name": meta[code].get("name") or raw.get("name") or code,
               "sector": meta[code].get("industry") or "未分类", "pct": number(raw.get("pct_change"))}
        for key, field in (("main", "net_amount"), ("elg", "buy_elg_amount"), ("lg", "buy_lg_amount"), ("md", "buy_md_amount"), ("sm", "buy_sm_amount")):
            value = number(raw.get(field))
            row[key] = value * 1e4 if value is not None else None
        result.append(row)
    return result


def institution_rows(raw_rows):
    """只汇总机构专用的单日席位；去掉买卖榜重复和多日累计榜。"""
    unique = {}
    for r in raw_rows:
        seat = text(r.get("exalter"))
        reason = text(r.get("reason"))
        if "机构专用" not in seat or not reason or any(t in reason for t in ("连续", "累计", "三个", "3个", "三日", "3日")):
            continue
        buy, sell = number(r.get("buy")), number(r.get("sell"))
        if buy is None or sell is None:
            continue
        key = (iso(r.get("trade_date")), security_code(r.get("ts_code")), seat, buy, sell)
        unique[key] = {"date": key[0], "code": key[1], "seat": seat, "buy": buy, "sell": sell, "net": buy - sell, "reason": reason}
    return list(unique.values())


def etf_rows(bundle):
    raw = indexed(bundle.get("etf", []))
    meta = {text(r.get("ts_code")): r for r in bundle.get("etf_meta", []) if text(r.get("ts_code"))}
    index_names = {text(r.get("index_code")): text(r.get("index_name")) for r in meta.values() if text(r.get("index_code")) and text(r.get("index_name"))}
    dates = bundle.get("calendar", [])
    result = []
    for (date, code), r in sorted(raw.items()):
        if date not in dates or dates.index(date) == 0:
            continue
        previous = dates[dates.index(date) - 1]
        old = raw.get((previous, code), {})
        current, before, nav = number(r.get("total_share")), number(old.get("total_share")), number(r.get("nav"))
        delta = (current - before) * 1e4 if current is not None and before is not None else None
        info = meta.get(code, {})
        index_code = text(info.get("index_code"))
        index_name = text(info.get("index_name"), index_names.get(index_code, "跟踪指数未取得"))
        result.append({"date": date, "previous_date": previous, "code": code, "name": security_name(r.get("etf_name"), code, security_name(info.get("extname"), code)),
                       "sector": index_name, "index_code": index_code or None, "shares": current * 1e4 if current is not None else None,
                       "share_change": delta, "net": delta * nav if delta is not None and nav is not None and nav > 0 else None,
                       "nav": nav, "size": number(r.get("total_size")) * 1e4 if number(r.get("total_size")) is not None else None})
    return result


def capital_report(bundle, *, start=None, end=None, participant="main", keyword="", sector=""):
    dates = bundle.get("calendar", [])
    end = end or bundle.get("flow_as_of") or bundle.get("as_of")
    start = start or end
    meta = stock_meta(bundle)
    flows = flow_rows(bundle)
    daily = [r for r in flows if start and start <= r["date"] <= end]
    group = defaultdict(list)
    for r in daily:
        group[r["code"]].append(r)
    rows = []
    for code, items in group.items():
        row = {k: items[-1][k] for k in ("code", "name", "sector")}
        row.update({k: total(items, k) for k in ("main", "elg", "lg", "md", "sm")})
        row.update(days=len(items), positive_days=sum((r.get("main") or 0) > 0 for r in items), as_of=max(r["date"] for r in items))
        row["net"] = row.get(participant)
        rows.append(row)
    if participant == "institution":
        group = defaultdict(list)
        for r in institution_rows(bundle.get("institutions", [])):
            if start and start <= (r["date"] or "") <= end:
                group[r["code"]].append(r)
        rows = [{"code": code, "name": meta.get(code, {}).get("name") or code, "sector": meta.get(code, {}).get("industry") or "未分类",
                 "buy": total(items, "buy"), "sell": total(items, "sell"), "net": total(items, "net"), "days": len({r["date"] for r in items}),
                 "as_of": max(r["date"] for r in items), "evidence": items} for code, items in group.items()]
    elif participant == "etf":
        group = defaultdict(list)
        for r in etf_rows(bundle):
            if start and start <= r["date"] <= end:
                group[r["code"]].append(r)
        rows = [{**items[-1], "net": total(items, "net"), "share_change": total(items, "share_change"), "days": len(items), "as_of": items[-1]["date"]} for items in group.values()]
    elif participant == "national":
        rows = [dict(r) for r in bundle.get("national", [])]
    elif participant == "quant":
        rows = [dict(r) for r in bundle.get("quant", [])]
    excluded_nonstock = sum(not is_a_stock(security_code(r.get("code"))) for r in rows) if participant != "etf" else 0
    if participant != "etf":
        rows = [r for r in rows if is_a_stock(security_code(r.get("code")))]
    for row in rows:
        row["code"] = security_code(row.get("code"))
        info = meta.get(row["code"], {}) if participant != "etf" else {}
        row["name"] = security_name(row.get("name"), row["code"], text(info.get("name"), "名称未取得"))
        row["sector"] = text(info.get("industry"), text(row.get("sector"), "跟踪指数未取得" if participant == "etf" else "未分类"))
        row["net"] = number(row.get("net"))
    rows = [r for r in rows if (not keyword or keyword.casefold() in " ".join(str(r.get(k) or "") for k in ("code", "name", "sector", "holder", "seat")).casefold())
            and (not sector or r.get("sector") == sector)
            and (participant not in ("quant", "national") or start and start <= (r.get("as_of") or "") <= end)]
    expected = len([d for d in dates if start and start <= d <= end])
    outside_window = bool(dates and start and start < dates[0])
    for row in rows:
        if participant not in ("national", "quant"):
            row["expected_days"] = expected
            row["complete"] = row.get("days", 0) == expected and expected > 0 and not outside_window
    selected_codes = {r["code"] for r in rows}
    by_bare = {code.split(".")[0]: code for code in meta}
    canonical_codes = selected_codes | {by_bare.get(str(code).split(".")[0], code) for code in selected_codes}
    matrix = []
    if participant != "etf":
        inst = [r for r in institution_rows(bundle.get("institutions", [])) if start and start <= (r["date"] or "") <= end and r["code"] in canonical_codes]
        for name in sorted({r.get("sector") or "未分类" for r in rows}):
            codes = {r["code"] for r in rows if (r.get("sector") or "未分类") == name}
            codes |= {by_bare.get(str(code).split(".")[0], code) for code in codes}
            items = [r for r in daily if r["code"] in codes]
            large, small = total(items, "main"), total(items, "sm")
            matrix.append({"name": name, "main": large, "elg": total(items, "elg"), "lg": total(items, "lg"), "sm": small,
                           "institution": total([r for r in inst if r["code"] in codes], "net"),
                           "quant_count": len({r["code"] for r in bundle.get("quant", []) if (r["code"] in codes or by_bare.get(str(r["code"]).split(".")[0]) in codes) and start and start <= (r.get("as_of") or "") <= end}),
                           "national_count": len({r["code"] for r in bundle.get("national", []) if r["code"] in codes and r.get("as_of") and r["as_of"] <= end}),
                           "relation": "缺少可比资金" if large is None or small is None else "大单承接 / 小单流出" if large > 0 > small else "大单流出 / 小单承接" if large < 0 < small else "所列净额同向或为零"})
        matrix.sort(key=lambda r: abs(r["main"] or 0), reverse=True)
    sectors = []
    for name in sorted({r.get("sector") or "未分类" for r in rows}):
        items = [r for r in rows if (r.get("sector") or "未分类") == name]
        sectors.append({"name": name, "count": len(items), "net": total(items, "net"), "positive": sum(r.get("net") is not None and r["net"] > 0 for r in items),
                        "observed_net": sum(r["net"] for r in items if r.get("net") is not None) if any(r.get("net") is not None for r in items) else None,
                        "amount_covered": sum(r.get("net") is not None for r in items),
                        "negative": sum(r.get("net") is not None and r["net"] < 0 for r in items)})
    history = []
    if participant in ("main", "elg", "lg", "md", "sm"):
        for date in sorted({r["date"] for r in daily}):
            items = [r for r in daily if r["date"] == date and r["code"] in selected_codes]
            history.append({"date": date, "net": total(items, participant), "count": len(items)})
    return {"as_of": end, "start": start, "participant": participant, "rows": sorted(rows, key=lambda r: r.get("net") if r.get("net") is not None else float("-inf"), reverse=True),
            "sectors": sorted(sectors, key=lambda r: r["net"] if r["net"] is not None else float("-inf"), reverse=True), "history": history,
            "totals": {"net": total(rows, "net"), "count": len(rows), "complete": sum(r.get("complete", False) for r in rows),
                       "observed_net": sum(r["net"] for r in rows if r.get("net") is not None) if any(r.get("net") is not None for r in rows) else None,
                       "amount_covered": sum(r.get("net") is not None for r in rows),
                       "buying": sum(r.get("net") is not None and r["net"] > 0 for r in rows), "selling": sum(r.get("net") is not None and r["net"] < 0 for r in rows)},
            "matrix": matrix, "range_note": "查询起点早于已采集交易日窗口，仅展示已取得数据，不宣称区间完整" if outside_window else "",
            "excluded_nonstock": excluded_nonstock,
            "metadata_coverage": {"missing_names": sum(not text(r["name"]) for r in rows), "missing_sectors": sum(not text(r["sector"]) for r in rows)},
            "calendar": dates, "sources": bundle.get("sources", []), "flow_as_of": bundle.get("flow_as_of")}
