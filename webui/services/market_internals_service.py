"""Dated A-share advance/decline and Arms Index observations for market health."""
from __future__ import annotations

import time

from analysis.market_capital import is_a_stock, security_code
from analysis.market_health import market_internals
from data_store import kv_repo, tushare_client

NAMESPACE = "market_health"
CLOSE_PREFIX = "internals:close:"
HISTORY_SESSIONS = 20
TURNOVER_FLAT_PCT = 1
SINA = "新浪沪深京 A 股行情（分页快照）"
TUSHARE = "TuShare 沪深京 A 股日线"


def from_sina(rows, date, *, started, captured, provisional):
    quotes = ((r.get("trade"), r.get("settlement"), r.get("volume"), r.get("amount"), code[-2:])
              for r in rows if is_a_stock(code := security_code(r.get("symbol"))))
    return {**market_internals(quotes), "as_of": date, "provisional": provisional,
            "source": SINA, "sample_started_at": started, "captured_at": captured}


def _from_daily(rows, date):
    seen, quotes = set(), []
    for row in rows:
        code = security_code(row.get("ts_code"))
        if not is_a_stock(code):
            continue
        if tushare_client.yyyymmdd_to_iso(row.get("trade_date")) != date or code in seen:
            raise ValueError("日线包含异日或重复个股")
        seen.add(code)
        quotes.append((row.get("close"), row.get("pre_close"), row.get("vol"), row.get("amount"), code[-2:]))
    return {**market_internals(quotes, shares_per_unit=100, yuan_per_unit=1000), "as_of": date,
            "provisional": False, "source": TUSHARE}


def save_close(observation):
    if observation["provisional"]:
        return
    key = CLOSE_PREFIX + observation["as_of"]
    old = kv_repo.get(NAMESPACE, key)
    if old:
        previous = old[0]
        def quality(point):
            return (point["trin"] is not None, point.get("turnover_yuan") is not None,
                    point.get("sh_sz_turnover_yuan") is not None,
                    point["source"] == TUSHARE, point["eligible"])
        if quality(observation) < quality(previous):
            return
    kv_repo.set_(NAMESPACE, key, observation)


def intraday_history(date):
    hit = kv_repo.get(NAMESPACE, f"internals:intraday:{date}") if date else None
    return hit[0] if hit else []


def save_intraday(observation):
    date, captured = observation["as_of"], observation["captured_at"]
    if not observation["provisional"] or not captured:
        return
    points = {row["captured_at"]: row for row in intraday_history(date)}
    points[captured] = observation
    newest = sorted(points)[-100:]
    kv_repo.set_(NAMESPACE, f"internals:intraday:{date}", [points[stamp] for stamp in newest])


def limit_counts(date):
    from webui.services.market_limits_service import NAMESPACE as limits_namespace

    hit = kv_repo.get(limits_namespace, "day:" + date)
    if not hit or not hit[0].get("covered"):
        return None
    day = hit[0]
    return {"up": sum(event["kind"] == "U" for event in day["events"]),
            "down": sum(event["kind"] == "D" for event in day["events"]),
            "covered": day["covered"], "unverified": day.get("unverified"), "estimated": False}


def read_history(calendar, *, recent=True):
    # The oldest visible session also needs its immediately preceding close.
    days = sorted(set(calendar))
    if recent:
        days = days[-(HISTORY_SESSIONS + 1):]
    if not days:
        return []
    stored = dict(kv_repo.range_items(NAMESPACE, CLOSE_PREFIX + days[0], CLOSE_PREFIX + days[-1]))
    return [stored.get(CLOSE_PREFIX + day) or {"as_of": day} for day in days]


def recorded_days(start, end):
    return [key[len(CLOSE_PREFIX):] for key in kv_repo.range_keys(NAMESPACE, CLOSE_PREFIX + start, CLOSE_PREFIX + end)]


def _change_pct(value, base):
    return 100 * (value - base) / base if value is not None and base is not None and base > 0 else None


def _turnover_comparison(amount, prior_amount, provisional):
    if provisional:
        progress = 100 * amount / prior_amount if amount is not None and prior_amount else None
        return {"previous_yuan": prior_amount, "progress_pct": progress}
    change = _change_pct(amount, prior_amount)
    state = None
    if change is not None:
        state = "expansion" if change >= TURNOVER_FLAT_PCT else "contraction" if change <= -TURNOVER_FLAT_PCT else "flat"
    return {"previous_yuan": prior_amount, "change_pct": change, "state": state}


def _vs_previous(observation, previous):
    if not observation.get("source"):
        return observation
    comparison = {"as_of": previous.get("as_of"), "source": previous.get("source")}
    for side in ("up", "down", "flat"):
        comparison[f"{side}_pct"] = (_change_pct(observation.get(side), previous.get(side))
                                     if observation.get("eligible") and previous.get("eligible") else None)
    for field in ("turnover", "sh_sz_turnover"):
        key = field + "_yuan"
        comparison[field] = _turnover_comparison(observation.get(key), previous.get(key), observation["provisional"])
    return {**observation, "vs_previous": comparison}


def compared_history(history, *, calendar_complete=True):
    return [_vs_previous(row, history[i - 1] if i and calendar_complete else {}) for i, row in enumerate(history)]


def view(history, market, as_of, *, provisional=False):
    history = [row for row in history if row["as_of"] <= (as_of or "")]
    compared = compared_history(history)[-HISTORY_SESSIONS:]
    live = market.get("internals") or {}
    if provisional and live.get("provisional") and live.get("as_of") == as_of:
        latest = _vs_previous(live, history[-1] if history and history[-1]["as_of"] < as_of else {})
    else:
        latest = next((row for row in reversed(compared) if row.get("source")), None)
    if latest and not latest["provisional"]:
        limits = limit_counts(latest["as_of"])
        if not limits and market.get("as_of") == latest["as_of"]:
            values = market.get("values") or {}
            if values.get("limits_estimated"):
                limits = {"up": values.get("limit_up"), "down": values.get("limit_down"), "estimated": True}
        if limits:
            latest = {**latest, "limits": limits}
    return {"current": latest, "history": compared,
            "intraday_history": intraday_history(as_of),
            "covered": sum(row.get("breadth_pct") is not None or row.get("trin") is not None or
                           row.get("turnover_yuan") is not None for row in compared)}


def backfill(calendar, expected_count, *, recent=True, progress=None):
    """Fill missing closes using whole daily partitions; keep completed dates on failures."""
    try:
        history = read_history(calendar, recent=recent)
        missing = [row["as_of"] for row in reversed(history)
                   if row.get("source") != TUSHARE or row.get("turnover_yuan") is None or row.get("sh_sz_turnover_yuan") is None]
        if not missing:
            return None
        if tushare_client.get_pro() is None:
            return "市场广度 / TRIN 历史待补：需要现有 TuShare 日线配置。"

        # Reuse the app's paginated daily feed. Import lazily to avoid a service import cycle.
        from webui.services.sector_depth_service import _records

        minimum = max(3000, int((expected_count or 0) * .9)) if recent else 1
        incomplete = 0
        started = 0.0
        for i, date in enumerate(missing, 1):
            if progress:
                progress(i, len(missing), date)
                if len(missing) > HISTORY_SESSIONS:
                    time.sleep(max(0, started + 1.25 - time.monotonic()))
                    started = time.monotonic()
            rows = _records("daily", cap=6000, trade_date=date.replace("-", ""),
                            fields="ts_code,trade_date,close,pre_close,vol,amount")
            observation = _from_daily(rows, date)
            if observation["eligible"] < minimum or observation["missing_volume"]:
                incomplete += 1
                continue
            save_close(observation)
            if observation["turnover_yuan"] is None or observation["sh_sz_turnover_yuan"] is None:
                incomplete += 1
    except Exception as exc:
        return f"市场广度 / TRIN 历史回补中断（{type(exc).__name__}），保留已采集的行情。"
    return f"市场广度 / TRIN 有 {incomplete} 个交易日的日线尚不完整。" if incomplete else None
