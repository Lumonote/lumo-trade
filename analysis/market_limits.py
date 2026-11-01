"""涨跌停事件与区间统计。实际限价核验、交易日连续性和板块归属均可离线重算。"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from analysis.market_health import number
from data_store.tushare_client import text_field, yyyymmdd_to_iso
from webui.services.stock_screener_service import PRESET_KEYS, preset_pass, tech_features

KINDS = {"U": "涨停", "D": "跌停", "Z": "炸板", "O": "跌停开板"}
PATTERNS = dict(zip(PRESET_KEYS, (
    "持续放量", "放量突破MA20", "超跌反弹", "创N日新高", "创N日新低", "单日放量",
    "一阳穿N线", "N线多头排列", "均线金叉", "量能日环比", "均线支撑博反弹", "均线方向",
    "量能峰值缩量", "对子底", "翻倍又腰斩", "近期20cm", "均线方向组合", "首次站上MA20",
)))


def _cents(value):
    value = number(value)
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if value is not None else None


def a_share(code):
    code = text_field(code)
    return ((code.endswith(".SH") and code.startswith(("60", "68")))
            or (code.endswith(".SZ") and code.startswith(("00", "30")))
            or (code.endswith(".BJ") and code[:6].isdigit()))


def _seal_time(value):
    text = text_field(value)
    numeric = number(text)
    if numeric is not None and numeric.is_integer() and 0 <= numeric <= 235959:
        if numeric == 0:
            return ""
        text = str(int(numeric)).zfill(6)
        return f"{text[:2]}:{text[2:4]}:{text[4:]}"
    return text


def normalize_day(date, quotes, limits, extra=(), basics=None):
    """同日未复权OHLC × stk_limit，金额统一元。未知限价不猜测。"""
    basics = basics or {}
    prices = {r.get("ts_code"): r for r in limits if yyyymmdd_to_iso(r.get("trade_date")) == date}
    supplements = {(r.get("ts_code"), r.get("limit")): r for r in extra
                   if yyyymmdd_to_iso(r.get("trade_date")) == date}
    events, covered, missing, moves, unverified_codes = {}, 0, 0, {}, []
    for raw in quotes:
        code = text_field(raw.get("ts_code"))
        if not a_share(code) or yyyymmdd_to_iso(raw.get("trade_date")) != date:
            continue
        daily_close, daily_pre = number(raw.get("close")), number(raw.get("pre_close"))
        if daily_close is not None and daily_pre:
            moves[code[:6]] = -1 if daily_close < daily_pre else 1 if daily_close > daily_pre else 0
        price = prices.get(code, {})
        up, down, pre, close = [_cents(v) for v in (
            price.get("up_limit"), price.get("down_limit"), price.get("pre_close", raw.get("pre_close")), raw.get("close"))]
        high, low = _cents(raw.get("high")), _cents(raw.get("low"))
        if (None in (up, down, pre, close) or not 0 < down < pre < up
                or not down <= close <= up or (high is not None and high > up) or (low is not None and low < down)):
            missing += 1
            unverified_codes.append(code[:6])
            continue
        covered += 1
        kinds = []
        if close == up:
            kinds.append("U")
        elif (_cents(raw.get("high")) or 0) >= up:
            kinds.append("Z")
        if close == down:
            kinds.append("D")
        elif (_cents(raw.get("low")) or close) <= down:
            kinds.append("O")
        for kind in kinds:
            ext = supplements.get((code, kind), {})
            basic = basics.get(code, {})
            row = {"date": date, "code": code[:6], "ts_code": code, "kind": kind, "label": KINDS[kind],
                   "name": text_field(ext.get("name")) or text_field(basic.get("name")) or code[:6],
                   "industry": text_field(ext.get("industry")),
                   "close": number(raw.get("close")), "pct": number(raw.get("pct_chg")),
                   "open": number(raw.get("open")), "high": number(raw.get("high")), "low": number(raw.get("low")),
                   "up_limit": up / 100, "down_limit": down / 100,
                   "amount": number(raw.get("amount")) * 1000 if number(raw.get("amount")) is not None else None,
                   "one_price": _cents(raw.get("high")) == _cents(raw.get("low")) == close,
                   "first_time": _seal_time(ext.get("first_time")), "last_time": _seal_time(ext.get("last_time")),
                   "up_stat": text_field(ext.get("up_stat")), "source_streak": number(ext.get("limit_times"))}
            for key in ("turnover_ratio", "fd_amount", "open_times", "limit_amount", "float_mv", "total_mv"):
                row[key] = number(ext.get(key))
            events[(code, kind)] = row
    return {"date": date, "events": list(events.values()), "covered": covered, "unverified": missing,
            "unverified_codes": unverified_codes, "moves": moves}


def pattern_tags(bars, code="", as_of=""):
    """与条件选股逐标签共用默认规则；所有日线先截到查询截止日。"""
    rows = [r for r in bars if str(r.get("date", "")) <= as_of]
    feat = tech_features(rows, code)
    if feat is None:
        return {"tags": [], "as_of": rows[-1]["date"] if rows else None, "status": "日K不足21根"}
    tags = []
    for key in PRESET_KEYS:
        # 20cm是个股交易制度标签，板块指数无涨跌停制度。
        if not code and key == "recent_20cm":
            continue
        ok, hits = preset_pass(feat, {"labels": [key], "mode": "any"})
        if ok:
            tags.append({"key": key, "label": PATTERNS[key], "evidence": "；".join(hits)})
    return {"tags": tags, "as_of": feat["bar_date"], "status": "已计算" if feat["bar_date"] == as_of else "最近可用交易日"}


def summarize(events):
    out = {kind: sum(r["kind"] == kind for r in events) for kind in KINDS}
    out.update(stocks=len({r["code"] for r in events}),
               up_stocks=len({r["code"] for r in events if r["kind"] == "U"}),
               down_stocks=len({r["code"] for r in events if r["kind"] == "D"}),
               max_up=max((r["streak"] for r in events if r["kind"] == "U"), default=0),
               max_down=max((r["streak"] for r in events if r["kind"] == "D"), default=0))
    out["up_dates"] = sorted({r["date"] for r in events if r["kind"] == "U"})
    out["down_dates"] = sorted({r["date"] for r in events if r["kind"] == "D"})
    return out


def build_report(days, calendar, start, end, memberships=None, patterns=None, boards=None):
    """先用区间前的事件计算连续数，再截区间；重复事件去重，缺日打断并标下界。"""
    memberships, patterns, boards = memberships or {}, patterns or {}, boards or {}
    by_date = {d["date"]: d for d in days}
    previous, falls, events, stocks, sectors = {}, {}, [], defaultdict(list), defaultdict(list)
    for index, date in enumerate(calendar):
        day = by_date.get(date)
        if day is None:
            previous = {}
            falls = {}
            continue
        prior_day = by_date.get(calendar[index - 1]) if index else None
        next_falls = {}
        for code, move in day.get("moves", {}).items():
            old = falls.get(code, (0, prior_day is None))
            next_falls[code] = (old[0] + 1, old[1]) if move < 0 else (0, False)
        falls = next_falls
        current = {}
        unique = {(r["code"], r["kind"]): r for r in day["events"]}
        for key, raw in sorted(unique.items()):
            row = dict(raw)
            prior = previous.get(key)
            row["streak"] = prior[0] + 1 if prior and row["kind"] in ("U", "D") else 1
            row["streak_lower_bound"] = (prior[1] if prior else prior_day is None or row["code"] in prior_day.get("unverified_codes", []))
            row["fall_days"], row["fall_lower_bound"] = falls.get(row["code"], (None, False))
            current[key] = (row["streak"], row["streak_lower_bound"])
            if not start <= date <= end:
                continue
            links = memberships.get(f"{date}:{row['code']}", [])
            row["sectors"] = sorted({r["code"] for r in links if r["code"] in boards})
            row["pattern"] = patterns.get("stock:" + row["code"], {"tags": [], "status": "待补日K", "as_of": None})
            events.append(row)
            stocks[row["code"]].append(row)
            for code in row["sectors"]:
                sectors[code].append(row)
        previous = current
    stock_rows = []
    for code, rows in stocks.items():
        latest = max(rows, key=lambda r: r["date"])
        stock_rows.append({"code": code, "name": latest["name"], **summarize(rows),
                           "last_date": latest["date"], "sectors": sorted({v for r in rows for v in r["sectors"]}),
                           "pattern": latest["pattern"]})
    sector_rows = []
    for code, rows in sectors.items():
        sector_rows.append({**boards[code], "code": code, **summarize(rows),
                            "stock_codes": sorted({r["code"] for r in rows}),
                            "pattern": patterns.get("sector:" + code, {"tags": [], "status": "待补板块日K", "as_of": None})})
    selected = [d for d in calendar if start <= d <= end]
    daily = [{"date": d, "available": d in by_date,
              "covered": by_date.get(d, {}).get("covered"), "unverified": by_date.get(d, {}).get("unverified"),
              **(summarize([r for r in events if r["date"] == d]) if d in by_date else {k: None for k in KINDS})}
             for d in selected]
    return {"start": start, "end": end, "as_of": max((d for d in selected if d in by_date), default=None),
            "calendar": selected, "missing_dates": [d for d in selected if d not in by_date],
            "summary": summarize(events), "events": sorted(events, key=lambda r: (r["date"], r["code"], r["kind"]), reverse=True),
            "stocks": sorted(stock_rows, key=lambda r: (-r["U"], -r["D"], r["code"])),
            "sectors": sorted(sector_rows, key=lambda r: (-r["U"], -r["D"], r["code"])),
            "daily": daily, "unmapped_events": sum(not r["sectors"] for r in events),
            "pattern_catalog": [{"key": k, "label": v} for k, v in PATTERNS.items()]}
