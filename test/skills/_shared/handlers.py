"""Function handlers: each maps one (skill, function) to a standard envelope.

Every handler is strictly a *data query*: it returns facts (quotes, valuations,
flows, holdings, provenance) and never emits a recommendation, score-as-advice,
target price or buy/sell signal. All local rows are historical snapshots; the
``data_nature`` and ``as_of`` fields make that explicit.
"""
from __future__ import annotations

import statistics
from typing import Any, Callable, Dict, List, Optional

from . import envelope as E
from .db_gateway import DatabaseUnavailable, Gateway


# Tables that carry per-trading-day market data (used by data_availability).
_DATE_DATA_TABLES = [
    "ohlcv", "daily_basic", "market_daily", "moneyflow_dc",
    "dragon_tiger_inst", "hsgt_individual", "jgdy_detail",
]

# Tables that are populated by the app but currently empty in the sample DB.
_EMPTY_FEATURE_TABLES = [
    "dragon_tiger_list", "market_flow_daily", "financial_statement",
    "quant_radar_stock_daily", "sector_daily_metrics", "sector_turning_signal",
    "stock_related_news", "opportunity_run", "opportunity_item",
    "hot_sector_snapshot", "hot_sector_board", "hot_sector_stock",
]


def _open(args: Dict[str, Any]):
    return Gateway(args.get("db"))


def _nearest_either(gw: Gateway, table: str, compact: str) -> Optional[str]:
    """Nearest date present in either direction (before preferred)."""
    d = gw.nearest_date(table, compact, direction="before")
    if d:
        return d
    return gw.nearest_date(table, compact, direction="after")


def _request_echo(args: Dict[str, Any]) -> Dict[str, Any]:
    """Echo only the user-facing parameters (never secrets)."""
    keys = ("date", "start", "end", "code", "top_n", "limit", "window",
            "direction", "statement_type", "namespace", "cache_type", "source",
            "frequency", "end_date")
    return {k: args[k] for k in keys if args.get(k) not in (None, "")}


# --------------------------------------------------------------------------- #
# market-calendar-query
# --------------------------------------------------------------------------- #
def h_is_trading_day(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-calendar-query", "is_trading_day"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期，请使用 YYYY-MM-DD 或 YYYYMMDD")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    flag = gw.calendar_open(compact)
    bounds = gw._date_bounds("trade_calendar")
    limitations: List[str] = []
    status = E.STATUS_OK
    if flag is None:
        status = E.STATUS_LIMITED
        # Always surface the nearest *known* date (prefer the latest date not
        # after the request; fall back to the earliest date after it).
        nearest = gw.nearest_date("trade_calendar", compact, direction="before")
        if not nearest:
            nearest = gw.nearest_date("trade_calendar", compact, direction="after")
        limitations.append(
            "本地交易日历未覆盖该日期，无法判定其是否为交易日；"
            f"日历范围 {bounds.get('min')} ~ {bounds.get('max')}，"
            f"可参考的最近已知日期 {nearest}。"
        )
        data = {"date": iso, "is_open": None}
        as_of = {"date": nearest}
        gw.close()
        return E.build_envelope(skill, fn, _request_echo(args), status=status,
                                data_nature=E.NATURE_REFERENCE, source="trade_calendar",
                                data=data, as_of=as_of, limitations=limitations,
                                coverage={"rows": 0})
    data = {"date": iso, "is_open": bool(flag)}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_REFERENCE, source="trade_calendar",
                            data=data, as_of={"date": iso}, coverage={"rows": 1})


def h_list_open_days(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-calendar-query", "list_open_days"
    s, e = E.compact_of(args.get("start")), E.compact_of(args.get("end"))
    if not s or not e:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --start 与 --end")
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.calendar_days(s, e, only_open=True)
    bounds = gw._date_bounds("trade_calendar")
    limitations: List[str] = []
    status = E.STATUS_OK
    if not rows:
        status = E.STATUS_NO_DATA
        limitations.append(
            f"区间内无交易日记录；本地日历范围 {bounds.get('min')} ~ {bounds.get('max')}。")
    elif bounds.get("max") and e > bounds["max"].replace("-", ""):
        limitations.append(
            f"区间末端超出本地日历（最新 {bounds['max']}），超出部分未返回，需联网由应用补齐日历。")
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_REFERENCE, source="trade_calendar",
                            data=rows, as_of={"date": bounds.get("max")},
                            coverage=E.rows_coverage(rows), limitations=limitations)


def h_nearest_trading_day(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-calendar-query", "nearest_trading_day"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    direction = args.get("direction") or "next"
    if direction not in ("prev", "next"):
        direction = "next"
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    result = gw.calendar_nearest_open(compact, "before" if direction == "prev" else "after")
    status = E.STATUS_OK if result else E.STATUS_NO_DATA
    limitations = [] if result else ["本地日历在该方向上没有更多交易日。"]
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_REFERENCE, source="trade_calendar",
                            data={"date": iso, "direction": direction, "nearest": result},
                            as_of={"date": result}, coverage={"rows": 1 if result else 0},
                            limitations=limitations)


def h_data_availability(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-calendar-query", "data_availability"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    report = []
    for table in _DATE_DATA_TABLES:
        n = gw.rows_for_date(table, compact)
        bounds = gw._date_bounds(table)
        nearest = _nearest_either(gw, table, compact) if (bounds or {}).get("rows") else None
        report.append({
            "table": table, "rows_on_date": n,
            "table_min": (bounds or {}).get("min"),
            "table_max": (bounds or {}).get("max"),
            "nearest_date_before": nearest,
        })
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_OK,
                            data_nature=E.NATURE_HISTORICAL, source=",".join(_DATE_DATA_TABLES),
                            data=report, as_of={"date": iso},
                            coverage={"tables": len(report)})


# --------------------------------------------------------------------------- #
# market-overview-query
# --------------------------------------------------------------------------- #
def h_index_daily(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-overview-query", "index_daily"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_on_date("market_daily", compact)
    limitations = [
        "market_daily 在本库仅含 4 条指数行（上证/沪深300/中证1000/国证2000），"
        "并非全市场个股日线；全市场个股日线需由应用在联网时按交易日补齐。"
    ]
    status = E.STATUS_OK
    if not rows:
        status = E.STATUS_NO_DATA
        nearest = _nearest_either(gw, "market_daily", compact)
        limitations.append(f"该日期无指数行，可参考的最近日期 {nearest}。")
        as_of = {"date": nearest}
    else:
        as_of = {"date": iso}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="market_daily",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_market_breadth(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-overview-query", "market_breadth"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    # full-market rows live under top_n=0; pct_change drives the breadth.
    rows = [r for r in gw.read_on_date("moneyflow_dc", compact) if r.get("top_n") == 0]
    pcts = [r.get("pct_change") for r in rows if r.get("pct_change") is not None]
    up = sum(1 for p in pcts if p > 0)
    down = sum(1 for p in pcts if p < 0)
    flat = len(pcts) - up - down
    limitations = [
        "宽度由 moneyflow_dc(top_n=0) 的 pct_change 逐只统计，属于历史快照口径；"
        "该表覆盖区间约 2025-11-05 ~ 2026-07-08，区间外日期无宽度数据。"
    ]
    status = E.STATUS_OK
    if not pcts:
        status = E.STATUS_NO_DATA
        nearest = _nearest_either(gw, "moneyflow_dc", compact)
        limitations.append(f"该日期无全市场行，可参考的最近日期 {nearest}。")
        data = {"date": iso, "up": None, "down": None, "flat": None}
        as_of = {"date": nearest}
    else:
        data = {
            "date": iso, "universe": len(pcts), "up": up, "down": down, "flat": flat,
            "up_ratio": round(up / len(pcts), 4),
            "mean_pct_change": round(statistics.fmean(pcts), 3),
        }
        as_of = {"date": iso}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="moneyflow_dc(top_n=0)",
                            data=data, as_of=as_of,
                            coverage={"rows": len(pcts)}, limitations=limitations)


def h_daily_basic_snapshot(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "market-overview-query", "daily_basic_snapshot"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_on_date("daily_basic", compact)
    limitations = [
        "daily_basic 为估值/换手历史快照（全市场约 5400 行/日），"
        "本库覆盖约 2025-12-26 ~ 2026-02-09；返回汇总统计与少量样本行。"
    ]
    status = E.STATUS_OK
    if not rows:
        status = E.STATUS_NO_DATA
        nearest = _nearest_either(gw, "daily_basic", compact)
        limitations.append(f"该日期无估值快照，可参考的最近日期 {nearest}。")
        data = {"date": iso}
        as_of = {"date": nearest}
    else:
        def med(col):
            vals = [r.get(col) for r in rows if r.get(col) is not None]
            return round(statistics.median(vals), 4) if vals else None
        data = {
            "date": iso, "universe": len(rows),
            "median_pe_ttm": med("pe_ttm"), "median_pb": med("pb"),
            "median_turnover_rate": med("turnover_rate"),
            "median_total_mv": med("total_mv"),
            "sample": rows[:5],
        }
        as_of = {"date": iso}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="daily_basic",
                            data=data, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_market_daily_snapshot(args: Dict[str, Any]) -> Dict[str, Any]:
    # Same underlying table as index_daily; expose it explicitly for completeness.
    return h_index_daily(args)


# --------------------------------------------------------------------------- #
# stock-research-query
# --------------------------------------------------------------------------- #
def h_daily_kline(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "stock-research-query", "daily_kline"
    code = E.normalize_code(args.get("code"))
    nd = E.normalize_date(args.get("date"))
    if not code or not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE,
                                error="需要有效的 --code 与 --date")
    iso, compact = nd
    window = int(args.get("window") or 30)
    start = E.add_days(iso, -window)
    end = E.add_days(iso, min(window, 0)) or iso
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.ohlcv_window(code, start, iso, freq=args.get("frequency") or "1d")
    bounds = gw._date_bounds("ohlcv")
    have_codes = gw.read_for_code("ohlcv", code, limit=1)
    limitations = [
        "ohlcv 为前复权日K历史快照，仅覆盖被单独分析过的个股（本库约 202 只），"
        "并非全市场；缺失个股需由应用联网补齐。"
    ]
    status = E.STATUS_OK
    if not rows:
        status = E.STATUS_NO_DATA
        nearest = _nearest_either(gw, "ohlcv", compact)
        if not have_codes:
            limitations.append(f"本地 ohlcv 未覆盖个股 {code}（不在已分析个股集合）。")
        else:
            limitations.append(f"该窗口内无K线，可参考的最近日期 {nearest}。")
        as_of = {"date": nearest}
    else:
        as_of = {"date": rows[-1].get("ts", "")[:10]}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="ohlcv(1d,qfq)",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_valuation(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "stock-research-query", "valuation"
    code = E.normalize_code(args.get("code"))
    nd = E.normalize_date(args.get("date"))
    if not code or not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code 与 --date")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_for_code("daily_basic", code, start=iso, end=iso)
    limitations = ["daily_basic 为估值历史快照，覆盖约 2025-12-26 ~ 2026-02-09。"]
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    if not rows:
        nearest = _nearest_either(gw, "daily_basic", compact)
        limitations.append(f"该日期无该个股估值，可参考的最近日期 {nearest}。")
        as_of = {"date": nearest}
    else:
        as_of = {"date": iso}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="daily_basic",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_financial_statements(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "stock-research-query", "financial_statements"
    code = E.normalize_code(args.get("code"))
    if not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code")
    stype = args.get("statement_type") or "income"
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = [r for r in gw.read_for_code("financial_statement", code)
            if r.get("statement_type") == stype]
    limitations = [
        "financial_statement 表在本库为空（财报需由应用在联网时按报告期缓存）；"
        "当前无法返回任何财报行项目，未构造任何数据。"
    ]
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL if rows else E.NATURE_NOT_AVAILABLE,
                            source="financial_statement", data=rows,
                            as_of={}, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_stock_context(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "stock-research-query", "stock_context"
    code = E.normalize_code(args.get("code"))
    nd = E.normalize_date(args.get("date"))
    if not code or not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code 与 --date")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    kline = gw.ohlcv_window(code, iso, iso)
    valuation = gw.read_for_code("daily_basic", code, start=iso, end=iso)
    flow = [r for r in gw.read_for_code("moneyflow_dc", code, start=iso, end=iso) if r.get("top_n") == 0]
    lhb = gw.read_for_code("dragon_tiger_inst", code, start=iso, end=iso)
    sections = {
        "date": iso, "code": code,
        "daily_bar": kline, "valuation": valuation,
        "moneyflow": flow, "dragon_tiger_inst": lhb,
    }
    present = sum(1 for v in (kline, valuation, flow, lhb) if v)
    limitations = [
        "事实卡片仅汇总当日各表已存在的事实数据，不做评分、不做买卖建议；"
        "任一来源在该日期无数据时对应字段为空数组。"
    ]
    status = E.STATUS_OK if present else E.STATUS_NO_DATA
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL,
                            source="ohlcv,daily_basic,moneyflow_dc,dragon_tiger_inst",
                            data=sections, as_of={"date": iso},
                            coverage={"sections_with_data": present, "sections_total": 4},
                            limitations=limitations)


# --------------------------------------------------------------------------- #
# capital-flow-query
# --------------------------------------------------------------------------- #
def h_moneyflow_ranking(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "capital-flow-query", "moneyflow_ranking"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    limit = int(args.get("limit") or 32)
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    # The ranked snapshot is top_n=32 (the 32-row list); fall back to 0.
    rows = [r for r in gw.read_on_date("moneyflow_dc", compact, order="net_amount DESC")
            if r.get("top_n") == 32]
    if not rows:
        rows = [r for r in gw.read_on_date("moneyflow_dc", compact, order="net_amount DESC")
                if r.get("top_n") == 0][:limit]
    rows = rows[:limit]
    limitations = [
        "这是按 net_amount（主力净额）字段的事实排序，仅用于研究复核，"
        "不是推荐、不是买入信号；数据为历史快照，覆盖约 2025-11-05 ~ 2026-07-08。"
    ]
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    if not rows:
        nearest = _nearest_either(gw, "moneyflow_dc", compact)
        limitations.append(f"该日期无资金流，可参考的最近日期 {nearest}。")
        as_of = {"date": nearest}
    else:
        as_of = {"date": iso}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="moneyflow_dc",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_moneyflow_stock(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "capital-flow-query", "moneyflow_stock"
    code = E.normalize_code(args.get("code"))
    nd = E.normalize_date(args.get("date"))
    if not code or not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code 与 --date")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_for_code("moneyflow_dc", code, start=iso, end=iso)
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    limitations = ["资金流为历史快照，覆盖约 2025-11-05 ~ 2026-07-08。"]
    if not rows:
        nearest = _nearest_either(gw, "moneyflow_dc", compact)
        limitations.append(f"该日期无该个股资金流，可参考的最近日期 {nearest}。")
        as_of = {"date": nearest}
    else:
        as_of = {"date": iso}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="moneyflow_dc",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_dragon_tiger(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "capital-flow-query", "dragon_tiger"
    nd = E.normalize_date(args.get("date"))
    code = E.normalize_code(args.get("code"))
    if not nd and not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE,
                                error="需要 --date（可叠加 --code）")
    iso, compact = nd if nd else (None, None)
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    if code:
        rows = gw.read_for_code("dragon_tiger_inst", code,
                                start=iso, end=iso)
    else:
        rows = gw.read_on_date("dragon_tiger_inst", compact)
    limitations = [
        "龙虎榜机构席位为历史事实数据，不代表机构当前观点，不构成建议；"
        "dragon_tiger_list（游资净额口径）在本库为空。"
    ]
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    if not rows:
        nearest = _nearest_either(gw, "dragon_tiger_inst", compact) if compact else None
        limitations.append(f"该日期无龙虎榜记录，可参考的最近日期 {nearest}。")
        as_of = {"date": nearest}
    else:
        as_of = {"date": iso or rows[0].get("trade_date")}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="dragon_tiger_inst",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_hsgt_hold(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "capital-flow-query", "hsgt_hold"
    code = E.normalize_code(args.get("code"))
    if not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code")
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_for_code("hsgt_individual", code)
    limitations = [
        "沪深港通个股持股为历史快照，本库该表最新仅到 2024-08-16，"
        "明显过期，不能表述为当前北向持股；需由应用联网更新。"
    ]
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    as_of = {"date": rows[0].get("trade_date") if rows else None}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="hsgt_individual",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


# --------------------------------------------------------------------------- #
# holders-survey-query
# --------------------------------------------------------------------------- #
def h_top_floatholders(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "holders-survey-query", "top_floatholders"
    code = E.normalize_code(args.get("code"))
    if not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code")
    end = E.normalize_date(args.get("end_date"))
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    if end:
        rows = [r for r in gw.read_for_code("top10_floatholders", code)
                if E.normalize_date(r.get("end_date")) == end]
    else:
        rows = gw.read_for_code("top10_floatholders", code)
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    limitations = ["十大流通股东为报告期（end_date）历史快照，低频更新。"]
    if not rows:
        limitations.append("该个股无十大流通股东记录（或指定 end_date 无数据）。")
        as_of = {}
    else:
        as_of = {"date": E.iso_of(rows[0].get("end_date"))}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="top10_floatholders",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_holder_number(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "holders-survey-query", "holder_number"
    code = E.normalize_code(args.get("code"))
    if not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code")
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_for_code("stk_holdernumber", code)
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    limitations = ["股东户数为报告期历史快照，低频更新。"]
    as_of = {"date": E.iso_of(rows[0].get("end_date")) if rows else None}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="stk_holdernumber",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_institution_survey(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "holders-survey-query", "institution_survey"
    code = E.normalize_code(args.get("code"))
    if not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code")
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.read_for_code("jgdy_detail", code,
                            start=args.get("start"), end=args.get("end"),
                            limit=int(args.get("limit") or 100))
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    limitations = ["机构调研记录为历史事实，不代表机构当前观点。"]
    as_of = {"date": rows[0].get("survey_date") if rows else None}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="jgdy_detail",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


def h_fund_holdings(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "holders-survey-query", "fund_holdings"
    code = E.normalize_code(args.get("code"))
    if not code:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="需要 --code")
    end = E.normalize_date(args.get("end_date"))
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    all_rows = gw.read_for_code("fund_hold_detail", code)
    if end:
        rows = [r for r in all_rows if E.normalize_date(r.get("end_date")) == end]
    else:
        rows = all_rows
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    limitations = ["基金持仓为报告期（end_date）历史快照，低频更新。"]
    if not rows:
        limitations.append("该个股无基金持仓记录（或指定 end_date 无数据）。")
        as_of = {}
    else:
        as_of = {"date": E.iso_of(rows[0].get("end_date"))}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="fund_hold_detail",
                            data=rows, as_of=as_of, coverage=E.rows_coverage(rows),
                            limitations=limitations)


# --------------------------------------------------------------------------- #
# data-provenance-query
# --------------------------------------------------------------------------- #
def h_sync_log(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "data-provenance-query", "sync_log"
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    rows = gw.sync_log_rows(args.get("source"), args.get("start"),
                            args.get("end"), int(args.get("limit") or 100))
    status = E.STATUS_OK if rows else E.STATUS_NO_DATA
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=status,
                            data_nature=E.NATURE_HISTORICAL, source="sync_log",
                            data=rows, as_of={}, coverage=E.rows_coverage(rows),
                            limitations=["sync_log 记录各数据源历史抓取结果（含失败原因）。"])


def h_cache_status(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "data-provenance-query", "cache_status"
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    kv = gw.kv_get(args.get("namespace") or "")
    sent = gw.sentiment_rows(args.get("cache_type") or None)
    # Summarize rather than dumping potentially large payloads.
    kv_summary = [{"namespace": r["namespace"], "key": r["key"],
                   "updated_at": r["updated_at"], "ttl_seconds": r["ttl_seconds"]} for r in kv]
    sent_summary = [{"cache_type": r["cache_type"], "identifier": r["identifier"],
                     "updated_at": r["updated_at"], "ttl_seconds": r["ttl_seconds"]} for r in sent]
    data = {"kv_cache": kv_summary, "sentiment_cache": sent_summary}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_OK,
                            data_nature=E.NATURE_HISTORICAL, source="kv_cache,sentiment_cache",
                            data=data, as_of={},
                            coverage={"kv_rows": len(kv_summary), "sentiment_rows": len(sent_summary)},
                            limitations=["缓存时效以 updated_at/ttl_seconds 为准；过期缓存不代表当前值。"])


def h_data_freshness(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "data-provenance-query", "data_freshness"
    nd = E.normalize_date(args.get("date"))
    if not nd:
        return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                                data_nature=E.NATURE_NOT_AVAILABLE, error="无法解析日期")
    iso, compact = nd
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    report = []
    for table in _DATE_DATA_TABLES:
        b = gw._date_bounds(table) or {}
        latest = b.get("max")
        lag = E.calendar_days_between(latest, iso) if latest else None
        report.append({
            "table": table, "rows": b.get("rows", 0),
            "latest_date": latest, "lag_days_vs_requested": lag,
            "is_empty": b.get("rows", 0) == 0,
        })
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_OK,
                            data_nature=E.NATURE_HISTORICAL, source="inventory",
                            data=report, as_of={"date": iso},
                            coverage={"tables": len(report)},
                            limitations=["lag_days 为各表最新日期相对请求日期的自然日差；负值表示表内最新早于请求日期。"])


def h_source_diagnostics(args: Dict[str, Any]) -> Dict[str, Any]:
    skill, fn = "data-provenance-query", "source_diagnostics"
    try:
        gw = _open(args)
    except DatabaseUnavailable as exc:
        return _db_error(skill, fn, args, exc)
    inv = gw.inventory()
    empty = [r["table"] for r in inv if r["rows"] == 0]
    # failure counts from sync_log
    failures = {}
    for r in gw.sync_log_rows(None, limit=10000):
        if r.get("status") not in ("ok",):
            failures[r["source"]] = failures.get(r["source"], 0) + 1
    data = {"empty_tables": empty, "recent_sync_failures": failures, "inventory": inv}
    gw.close()
    return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_OK,
                            data_nature=E.NATURE_HISTORICAL, source="inventory,sync_log",
                            data=data, as_of={},
                            coverage={"empty_tables": len(empty)},
                            limitations=["空表表示对应功能在本库尚未产生数据，不代表功能不存在。"])


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
REGISTRY: Dict[str, Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = {
    "market-calendar-query": {
        "is_trading_day": h_is_trading_day,
        "list_open_days": h_list_open_days,
        "nearest_trading_day": h_nearest_trading_day,
        "data_availability": h_data_availability,
    },
    "market-overview-query": {
        "index_daily": h_index_daily,
        "market_breadth": h_market_breadth,
        "daily_basic_snapshot": h_daily_basic_snapshot,
        "market_daily_snapshot": h_market_daily_snapshot,
    },
    "stock-research-query": {
        "daily_kline": h_daily_kline,
        "valuation": h_valuation,
        "financial_statements": h_financial_statements,
        "stock_context": h_stock_context,
    },
    "capital-flow-query": {
        "moneyflow_ranking": h_moneyflow_ranking,
        "moneyflow_stock": h_moneyflow_stock,
        "dragon_tiger": h_dragon_tiger,
        "hsgt_hold": h_hsgt_hold,
    },
    "holders-survey-query": {
        "top_floatholders": h_top_floatholders,
        "holder_number": h_holder_number,
        "institution_survey": h_institution_survey,
        "fund_holdings": h_fund_holdings,
    },
    "data-provenance-query": {
        "sync_log": h_sync_log,
        "cache_status": h_cache_status,
        "data_freshness": h_data_freshness,
        "source_diagnostics": h_source_diagnostics,
    },
}


def _db_error(skill: str, fn: str, args: Dict[str, Any], exc: DatabaseUnavailable) -> Dict[str, Any]:
    return E.build_envelope(skill, fn, _request_echo(args), status=E.STATUS_ERROR,
                            data_nature=E.NATURE_NOT_AVAILABLE, source=None,
                            error=str(exc),
                            limitations=["数据源不可用：数据库缺失或无法只读打开，未返回任何数据。"])
