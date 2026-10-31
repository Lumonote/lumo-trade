#!/usr/bin/env python3
"""Independent ground-truth investigation (read-only).

Opens a SEPARATE sqlite3 connection (not the skill gateway) and answers the
questions the skill's metadata assumes. Everything here is a different query
path than the handler under audit.
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DB = ROOT / "data" / "lumo_data.sqlite"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
out = {}


def q(sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def one(sql, params=()):
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


# 1) Code shapes per table: how many rows have a '.' suffix vs bare 6-digit.
for t, col in [
    ("dragon_tiger_inst", "ts_code"),
    ("jgdy_detail", "ts_code"),
    ("fund_hold_detail", "ts_code"),
    ("daily_basic", "ts_code"),
    ("moneyflow_dc", "ts_code"),
    ("top10_floatholders", "ts_code"),
    ("stk_holdernumber", "ts_code"),
    ("hsgt_individual", "ts_code"),
]:
    rows = q(
        f"SELECT "
        f"SUM(CASE WHEN {col} LIKE '%.%' THEN 1 ELSE 0 END) suffixed, "
        f"SUM(CASE WHEN {col} NOT LIKE '%.%' THEN 1 ELSE 0 END) bare, "
        f"COUNT(*) total FROM {t}"
    )[0]
    out[f"codeshape:{t}"] = rows

# 2) Date shapes per table: ISO (contains '-') vs compact (8 digits).
for t, col in [
    ("daily_basic", "trade_date"),
    ("market_daily", "trade_date"),
    ("moneyflow_dc", "trade_date"),
    ("dragon_tiger_inst", "trade_date"),
    ("jgdy_detail", "survey_date"),
    ("hsgt_individual", "trade_date"),
    ("top10_floatholders", "end_date"),
    ("stk_holdernumber", "end_date"),
    ("fund_hold_detail", "end_date"),
]:
    rows = q(
        f"SELECT "
        f"SUM(CASE WHEN {col} LIKE '%-%' THEN 1 ELSE 0 END) iso, "
        f"SUM(CASE WHEN {col} NOT LIKE '%-%' THEN 1 ELSE 0 END) compact, "
        f"COUNT(*) total FROM {t}"
    )[0]
    out[f"dateshape:{t}"] = rows

# 3) The 157 compact moneyflow rows: which top_n, which dates?
out["moneyflow_compact_by_topn"] = q(
    "SELECT top_n, COUNT(*) n, MIN(trade_date) mn, MAX(trade_date) mx "
    "FROM moneyflow_dc WHERE trade_date NOT LIKE '%-%' GROUP BY top_n"
)
out["moneyflow_compact_dates"] = q(
    "SELECT trade_date, top_n, COUNT(*) n FROM moneyflow_dc "
    "WHERE trade_date NOT LIKE '%-%' GROUP BY trade_date, top_n ORDER BY trade_date"
)

# 4) On a date that has BOTH ISO and compact rows, are there duplicate
# (ts_code, top_n) pairs across the two forms? -> would double a ranking.
dup = q(
    """
    WITH norm AS (
      SELECT CASE WHEN trade_date LIKE '%-%'
             THEN REPLACE(trade_date,'-','') ELSE trade_date END d,
             ts_code, top_n, COUNT(*) copies
      FROM moneyflow_dc
      GROUP BY d, ts_code, top_n
    )
    SELECT d, ts_code, top_n, copies FROM norm WHERE copies > 1
    ORDER BY d, ts_code LIMIT 50
    """
)
out["moneyflow_duplicate_iso_compact_pairs"] = dup
out["moneyflow_duplicate_count"] = one(
    """
    SELECT COUNT(*) n FROM (
      SELECT REPLACE(trade_date,'-','') d, ts_code, top_n, COUNT(*) c
      FROM moneyflow_dc GROUP BY d, ts_code, top_n HAVING c > 1)
    """
)["n"]

# 5) Independent breadth for 2026-06-01 (ISO) top_n=0.
breadth = one(
    """
    SELECT
      SUM(CASE WHEN pct_change>0 THEN 1 ELSE 0 END) up,
      SUM(CASE WHEN pct_change<0 THEN 1 ELSE 0 END) down,
      SUM(CASE WHEN pct_change=0 THEN 1 ELSE 0 END) zero,
      SUM(CASE WHEN pct_change IS NULL THEN 1 ELSE 0 END) nullpct,
      COUNT(*) total,
      AVG(pct_change) meanpct
    FROM moneyflow_dc WHERE trade_date='2026-06-01' AND top_n=0
    """
)
out["independent_breadth_20260601"] = breadth

# 6) How many distinct top_n values exist, and per date row counts for top_n 0/32.
out["moneyflow_topn_values"] = q(
    "SELECT top_n, COUNT(*) n FROM moneyflow_dc GROUP BY top_n ORDER BY top_n"
)

# 7) trade_calendar: any is_open=0? any weekend?
out["calendar_isopen_values"] = q(
    "SELECT is_open, COUNT(*) n FROM trade_calendar GROUP BY is_open"
)

con.close()
Path(test_dir := Path(__file__).resolve().parents[1] / "raw").mkdir(exist_ok=True)
(Path(__file__).resolve().parents[1] / "raw" / "groundtruth_investigation.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(out, ensure_ascii=False, indent=2))
