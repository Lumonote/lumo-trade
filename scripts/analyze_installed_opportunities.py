"""Offline audit of installed opportunity history; the source database stays read-only.

Example:
    python scripts/analyze_installed_opportunities.py --db /path/to/kronos_data.sqlite

Outputs preserve both archived recommendation returns and independently reconstructed
candidate returns. These are deliberately separate populations. Calendar dates with
the same next trading day are one decision, not multiple independent observations.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd


def ts_code(code: str) -> str:
    code = str(code).split(".")[0].zfill(6)
    if code.startswith(("4", "8", "92")):
        return code + ".BJ"
    if code.startswith(("6", "9")):
        return code + ".SH"
    return code + ".SZ"


def next_day(calendar: list[str], report_date: str) -> str | None:
    index = bisect.bisect_right(calendar, report_date.replace("-", ""))
    return calendar[index] if index < len(calendar) else None


def decode(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def stats(frame: pd.DataFrame, column: str, cost_pct: float = 0.2) -> dict:
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    wins = values[values > 0]
    losses = values[values < 0]
    return {
        "n": int(len(values)),
        "days": int(frame.loc[values.index, "entry_date"].nunique()),
        "win_pct": float((values > 0).mean() * 100) if len(values) else None,
        "net_win_pct": float((values > cost_pct).mean() * 100) if len(values) else None,
        "mean_pct": float(values.mean()) if len(values) else None,
        "median_pct": float(values.median()) if len(values) else None,
        "net_mean_pct": float(values.mean() - cost_pct) if len(values) else None,
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) else None,
        "mean_win_pct": float(wins.mean()) if len(wins) else None,
        "mean_loss_pct": float(losses.mean()) if len(losses) else None,
    }


def exit_return(bars: list[dict] | None, take_profit: float, stop_loss: float) -> float:
    """Conservative daily-bar exit with A-share T+1 and gap fills.

    Entry-day sales are forbidden. If both barriers hit on the same later bar,
    stop-loss fills first. Obvious locked-down bars defer the sale. No intraday
    path or guaranteed limit-order fill is inferred from daily OHLC.
    """
    if not bars or len(bars) < 2:
        return float("nan")
    buy = float(bars[0]["open"])
    upper, lower = buy * (1 + take_profit / 100), buy * (1 - stop_loss / 100)
    for bar in bars[1:]:
        op, hi, lo = float(bar["open"]), float(bar["high"]), float(bar["low"])
        pre_close = float(bar.get("pre_close") or op)
        if hi == lo and op < pre_close:
            continue
        if op <= lower:
            return (op / buy - 1) * 100
        if op >= upper:
            return (op / buy - 1) * 100
        if lo <= lower:
            return -stop_loss
        if hi >= upper:
            return take_profit
    final = bars[-1]
    if final["high"] == final["low"] and final["open"] < (final.get("pre_close") or final["open"]):
        return float("nan")
    return (float(final["close"]) / buy - 1) * 100


def extract(db: Path, start: str, end: str, output: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    # A single transaction gives a consistent view even while the desktop app runs.
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.execute("BEGIN")
    recommendations = pd.read_sql_query(
        "SELECT * FROM backtest_recommendation WHERE report_date BETWEEN ? AND ?",
        connection, params=(start, end),
    )
    runs = pd.read_sql_query(
        "SELECT * FROM opportunity_run WHERE run_date BETWEEN ? AND ? AND mode='market_scan' ORDER BY run_at",
        connection, params=(start, end),
    )
    items = pd.read_sql_query("SELECT * FROM opportunity_item", connection)
    market = pd.read_sql_query(
        "SELECT * FROM market_daily WHERE trade_date <= ?", connection, params=(end.replace("-", ""),)
    )
    cached = pd.read_sql_query(
        "SELECT code,substr(ts,1,10) AS day,open,high,low,close,volume,amount FROM ohlcv WHERE frequency='1d' AND ts<=?",
        connection, params=(end + " 23:59:59",),
    )
    cal = pd.read_sql_query("SELECT cal_date FROM trade_calendar WHERE is_open=1 ORDER BY cal_date", connection)
    connection.close()
    calendar = sorted(set(cal.cal_date.astype(str)).union(market.trade_date.astype(str)))
    recommendations["code"] = recommendations.code.astype(str).str.zfill(6)
    recommendations["entry_date"] = recommendations.report_date.map(lambda x: next_day(calendar, x))
    rec_before = len(recommendations)
    recommendations = recommendations.sort_values("report_date").drop_duplicates(["entry_date", "code"], keep="last")
    recommendations["month"] = recommendations.report_date.str[:7]

    runs["entry_date"] = runs.run_date.map(lambda x: next_day(calendar, x))
    runs_before = len(runs)
    runs = runs.dropna(subset=["entry_date"]).drop_duplicates("entry_date", keep="last")
    candidates = items.merge(runs, left_on="run_id", right_on="id", suffixes=("", "_run"))
    candidates["code"] = candidates.code.astype(str).str.zfill(6)
    candidates["month"] = candidates.run_date.str[:7]
    signal_dicts = candidates.signals_json.map(decode)
    score_dicts = candidates.scores_json.map(decode)
    for key in ("rsi", "chase", "day_change", "change_3d", "change_5d", "sell_signals", "quant_score"):
        candidates[key] = signal_dicts.map(lambda x, k=key: x.get(k))
    for key in ("quantitative", "technical", "momentum", "volume_health", "liquidity", "sector", "fundamental", "dragon_tiger"):
        candidates["s_" + key] = score_dicts.map(lambda x, k=key: x.get(k))

    prices = {(row.ts_code, str(row.trade_date)): row._asdict() for row in market.itertuples(index=False)}
    # Cache fills only gaps in market_daily; all-candidate tests use market_daily only.
    merged = dict(prices)
    for code, group in cached.groupby("code"):
        previous = None
        for row in group.sort_values("day").itertuples(index=False):
            bar = row._asdict()
            bar["pre_close"] = previous
            previous = row.close
            merged.setdefault((ts_code(code), row.day.replace("-", "")), bar)

    def bars_for(code: str, entry: str | None, count: int, source: dict) -> list[dict] | None:
        if entry is None:
            return None
        index = bisect.bisect_left(calendar, entry)
        days = calendar[index:index + count]
        if len(days) < count or days[-1] > end.replace("-", ""):
            return None
        bars = [source.get((ts_code(code), day)) for day in days]
        if any(bar is None for bar in bars):
            return None
        if any(not all(pd.notna(bar.get(k)) and bar[k] > 0 for k in ("open", "high", "low", "close")) for bar in bars):
            return None
        return bars

    paths: dict[tuple[str, str, int], list[dict]] = {}
    for label, frame, source in (("pool", candidates, prices), ("rec", recommendations, merged)):
        for horizon in (2, 3, 5, 10):
            values = []
            exit_dates = []
            exit_values = {(tp, sl): [] for tp in (1, 2, 3, 5) for sl in (3, 5, 8, 12)} if horizon in (3, 5) else {}
            for row in frame.itertuples(index=False):
                bars = bars_for(row.code, row.entry_date, horizon, source)
                index = bisect.bisect_left(calendar, row.entry_date) if row.entry_date else len(calendar)
                exit_dates.append(calendar[index + horizon - 1] if index + horizon - 1 < len(calendar) else None)
                if bars:
                    paths[(label + ":" + row.code, row.entry_date, horizon)] = bars
                values.append((bars[-1]["close"] / bars[0]["open"] - 1) * 100 if bars else np.nan)
                for (tp, sl), results in exit_values.items():
                    results.append(exit_return(bars, tp, sl))
            frame[f"recomputed_{horizon}d"] = values
            frame[f"exit_date_{horizon}d"] = exit_dates
            for (tp, sl), results in exit_values.items():
                frame[f"exit_h{horizon}_tp{tp}_sl{sl}"] = results
        flags = []
        for row in frame.itertuples(index=False):
            bars = paths.get((label + ":" + row.code, row.entry_date, 2))
            bar = bars[0] if bars else None
            locked_up = bool(bar and bar["high"] == bar["low"] and bar["open"] > (bar.get("pre_close") or bar["open"]))
            suspended = bool(bar and (bar.get("vol", bar.get("volume", 0)) or 0) <= 0)
            flags.append(locked_up or suspended)
        frame["entry_untradeable"] = flags

    index_bars = market[market.ts_code == "000300.SH"].sort_values("trade_date")
    index_days, closes = index_bars.trade_date.tolist(), index_bars.close.to_numpy()
    for horizon in (5, 20):
        trends = {}
        for day in candidates.run_date.unique():
            index = bisect.bisect_right(index_days, day.replace("-", "")) - 1
            trends[day] = (closes[index] / closes[index - horizon] - 1) * 100 if index >= horizon else np.nan
        candidates[f"market_{horizon}d"] = candidates.run_date.map(trends)

    output.mkdir(parents=True, exist_ok=True)
    coverage = {
        "source_db": str(db), "start": start, "end": end,
        "recommendations_before_entry_dedup": rec_before,
        "recommendations_after_entry_dedup": len(recommendations),
        "market_scan_runs_before_entry_dedup": runs_before,
        "market_scan_runs_after_entry_dedup": len(runs),
        "candidate_rows": len(candidates),
        "candidate_5d_complete": int(candidates.recomputed_5d.notna().sum()),
        "candidate_5d_entry_days": int(candidates.loc[candidates.recomputed_5d.notna(), "entry_date"].nunique()),
        "missing_recommendation_months": [m for m in pd.period_range(start[:7], end[:7], freq="M").astype(str) if m not in set(recommendations.month)],
        "cost_assumption_pct": 0.2,
        "note": "Raw daily prices; no adjustment factors were available. Gross returns do not include fees. 0.2% round-trip cost is sensitivity only. T+1 entry, earliest sale on second holding day.",
    }
    (output / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for month, group in recommendations.groupby("month"):
        for horizon in (1, 3, 5, 10):
            rows.append({"month": month, "horizon": horizon, "population": "archived_returns", **stats(group, f"return_{horizon}d")})
    monthly_recommendations = pd.DataFrame(rows)
    rows = []
    for month, group in candidates[candidates.recomputed_5d.notna()].groupby("month"):
        for name, subset in (("pool", group), ("current_top10", group[group.item_rank <= 10])):
            rows.append({"month": month, "population": name, **stats(subset, "recomputed_5d")})
    with sqlite3.connect(output / "audit_snapshot.sqlite") as audit:
        candidates.to_sql("candidate_dataset", audit, if_exists="replace", index=False)
        recommendations.to_sql("recommendation_dataset", audit, if_exists="replace", index=False)
        monthly_recommendations.to_sql("monthly_recommendations", audit, if_exists="replace", index=False)
        pd.DataFrame(rows).to_sql("monthly_candidates", audit, if_exists="replace", index=False)
        pd.DataFrame([{"key": key, "value": json.dumps(value, ensure_ascii=False)} for key, value in coverage.items()]).to_sql(
            "audit_metadata", audit, if_exists="replace", index=False,
        )
    return candidates, recommendations, coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-09-29")
    parser.add_argument("--output-dir", type=Path, default=Path("results/installed_audit_20260929"))
    args = parser.parse_args()
    candidates, recommendations, coverage = extract(args.db, args.start, args.end, args.output_dir)
    print(json.dumps(coverage, ensure_ascii=False, indent=2))
    with sqlite3.connect(args.output_dir / "audit_snapshot.sqlite") as audit:
        print(pd.read_sql_query("SELECT * FROM monthly_candidates", audit).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
