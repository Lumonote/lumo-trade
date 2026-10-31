"""Read-only SQLite gateway over the project's ``lumo_data.sqlite``.

Design rules
------------
* **Physically read-only**: the connection is opened with ``?mode=ro``. The
  gateway cannot create, update or delete any row, so it is safe to run against
  the user's real database and never touches the WAL/reset paths.
* **Same schema as the app**: queries mirror ``data_store/schema.py`` (v21).
  No data is invented; when a table is empty or a date is missing the caller
  receives an explicit "no data" result plus the nearest obtainable date.
* **Date/code tolerant**: the database stores dates in *both* ISO and compact
  forms and codes in *both* bare and suffixed forms. The gateway normalizes
  these at query time instead of assuming a single shape.
* **Stdlib only**: works on CPython without pandas/requests.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .envelope import (
    compact_of,
    iso_of,
    normalize_code,
    normalize_date,
)


class DatabaseUnavailable(RuntimeError):
    """Raised when the SQLite file cannot be opened (missing / locked / corrupt)."""


def _iso_from_compact(compact: str) -> str:
    """'YYYYMMDD' -> 'YYYY-MM-DD' (assumes already validated compact)."""
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"


def _any_to_iso(value: str) -> str:
    """Normalize either a compact or an ISO date string to ISO."""
    v = str(value).strip()
    if "-" in v:
        return v[:10]
    return _iso_from_compact(v)


# --------------------------------------------------------------------------- #
# Table metadata: code column + style, date column + style.
# code style:  'bare' | 'suffix' | 'mixed'
# date style:  'date'   | 'datetime' | 'mixed'
# --------------------------------------------------------------------------- #
TABLES: Dict[str, Dict[str, str]] = {
    "ohlcv":                  {"code": "code",      "code_style": "bare",
                               "date": "ts",        "date_style": "datetime"},
    "daily_basic":            {"code": "ts_code",   "code_style": "suffix",
                               "date": "trade_date", "date_style": "mixed"},
    "trade_calendar":         {"code": "",          "code_style": "bare",
                               "date": "cal_date",  "date_style": "mixed"},
    "moneyflow_dc":           {"code": "ts_code",   "code_style": "suffix",
                               "date": "trade_date", "date_style": "mixed"},
    "kv_cache":               {"code": "",          "code_style": "bare",
                               "date": "updated_at", "date_style": "mixed"},
    "market_daily":           {"code": "ts_code",   "code_style": "suffix",
                               "date": "trade_date", "date_style": "mixed"},
    "market_flow_daily":      {"code": "ts_code",   "code_style": "suffix",
                               "date": "trade_date", "date_style": "mixed"},
    "dragon_tiger_inst":      {"code": "ts_code",   "code_style": "suffix",
                               "date": "trade_date", "date_style": "mixed"},
    "hsgt_individual":       {"code": "ts_code",   "code_style": "bare",
                               "date": "trade_date", "date_style": "mixed"},
    "top10_floatholders":    {"code": "ts_code",   "code_style": "mixed",
                               "date": "end_date",  "date_style": "mixed"},
    "stk_holdernumber":      {"code": "ts_code",   "code_style": "mixed",
                               "date": "end_date",  "date_style": "mixed"},
    "jgdy_detail":           {"code": "ts_code",   "code_style": "mixed",
                               "date": "survey_date", "date_style": "mixed"},
    "fund_hold_detail":      {"code": "ts_code",   "code_style": "mixed",
                               "date": "end_date",  "date_style": "mixed"},
    "sync_log":              {"code": "source",    "code_style": "bare",
                               "date": "ran_at",    "date_style": "mixed"},
    "dragon_tiger_list":     {"code": "ts_code",   "code_style": "suffix",
                               "date": "trade_date", "date_style": "mixed"},
    "financial_statement":   {"code": "code",      "code_style": "bare",
                               "date": "report_date", "date_style": "mixed"},
    "sentiment_cache":       {"code": "identifier", "code_style": "bare",
                               "date": "updated_at", "date_style": "mixed"},
    "quant_radar_stock_daily": {"code": "code",    "code_style": "bare",
                               "date": "trade_date", "date_style": "mixed"},
    "sector_daily_metrics":  {"code": "",          "code_style": "bare",
                               "date": "trade_date", "date_style": "mixed"},
    "sector_turning_signal": {"code": "",          "code_style": "bare",
                               "date": "trade_date", "date_style": "mixed"},
    "stock_related_news":    {"code": "code",      "code_style": "bare",
                               "date": "fetched_at", "date_style": "mixed"},
    "star_orbit_ring":       {"code": "",          "code_style": "bare",
                               "date": "created_at", "date_style": "mixed"},
    "star_orbit_board":      {"code": "board_code", "code_style": "bare",
                               "date": "created_at", "date_style": "mixed"},
    "star_orbit_stock":      {"code": "stock_code", "code_style": "bare",
                               "date": "created_at", "date_style": "mixed"},
    "star_orbit_board_member": {"code": "stock_code", "code_style": "bare",
                               "date": "updated_at", "date_style": "mixed"},
    "backtest_recommendation": {"code": "code",    "code_style": "bare",
                               "date": "report_date", "date_style": "mixed"},
}


def resolve_db_path(explicit: Optional[str] = None) -> Path:
    """Resolve the database path.

    Order: explicit arg -> ``KRONOS_SQLITE_PATH`` ->
    ``<project_root>/data/lumo_data.sqlite``.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("KRONOS_SQLITE_PATH")
    if env:
        return Path(env).expanduser()
    # _shared -> skills -> test -> project root
    root = Path(__file__).resolve().parents[3]
    return root / "data" / "lumo_data.sqlite"


class Gateway:
    """Read-only data access. One instance per process/CLI call."""

    def __init__(self, db_path: Optional[str] = None):
        self.path = resolve_db_path(db_path)
        if not self.path.exists():
            raise DatabaseUnavailable(f"数据库文件不存在: {self.path}")
        try:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            raise DatabaseUnavailable(f"无法以只读方式打开数据库 {self.path}: {exc}") from exc

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ #
    # Low-level helpers
    # ------------------------------------------------------------------ #
    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    def _columns(self, table: str) -> List[str]:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def _date_expr(self, table: str) -> str:
        """SQL expression that reduces a table's date column to compact form."""
        col = TABLES[table]["date"]
        style = TABLES[table]["date_style"]
        if style == "datetime":
            return f"REPLACE(SUBSTR({col},1,10),'-','')"
        return f"REPLACE({col},'-','')"

    def _date_iso_expr(self, table: str) -> str:
        """SQL expression that reduces a table's date column to ISO form."""
        col = TABLES[table]["date"]
        style = TABLES[table]["date_style"]
        if style == "datetime":
            return f"SUBSTR({col},1,10)"
        # compact -> ISO via string slicing; ISO passes through.
        return (
            f"CASE WHEN {col} LIKE '%-%' THEN {col} "
            f"ELSE SUBSTR({col},1,4)||'-'||SUBSTR({col},5,2)||'-'||SUBSTR({col},7,2) END"
        )

    def _code_clause(self, table: str, bare: str) -> Tuple[str, tuple]:
        """WHERE fragment matching a code for the table's code style."""
        col = TABLES[table]["code"]
        style = TABLES[table]["code_style"]
        if style == "bare":
            return f"{col}=?", (bare,)
        if style == "suffix":
            return f"{col} LIKE ?", (f"{bare}.%",)
        # mixed
        return f"({col}=? OR {col} LIKE ?)", (bare, f"{bare}.%")

    def _date_match(self, table: str, iso: str, compact: str) -> Tuple[str, tuple]:
        """Sargable equality for one calendar day, tolerating both stored forms.

        Date/mixed columns may hold either 'YYYY-MM-DD' or 'YYYYMMDD', so we
        match both with OR (the date column index can satisfy either branch).
        Datetime columns hold 'YYYY-MM-DD HH:MM:SS', so we match a half-open
        day range (sargable).
        """
        col = TABLES[table]["date"]
        if TABLES[table]["date_style"] == "datetime":
            return f"({col}>=? AND {col}<?)", (f"{iso} 00:00:00", f"{iso} 23:59:59.999")
        return f"({col}=? OR {col}=?)", (iso, compact)

    def _date_range(self, table: str,
                    start_iso: str, start_compact: str,
                    end_iso: str, end_compact: str) -> Tuple[str, tuple]:
        """Sargable inclusive range over a date/mixed column.

        Splits the two stored lexicographic bases (ISO vs compact) so each
        branch is range-sargable; they are combined with OR.
        """
        col = TABLES[table]["date"]
        if TABLES[table]["date_style"] == "datetime":
            return f"({col}>=? AND {col}<?)", (
                f"{start_iso} 00:00:00", f"{end_iso} 23:59:59.999")
        iso_branch = f"({col} LIKE '%-%' AND {col}>=? AND {col}<=?)"
        cmp_branch = f"({col} NOT LIKE '%-%' AND {col}>=? AND {col}<=?)"
        return f"({iso_branch} OR {cmp_branch})", (
            start_iso, end_iso, start_compact, end_compact)

    def _fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def _date_bounds(self, table: str) -> Optional[Dict[str, Any]]:
        """Min/max (ISO) + distinct date count for a table, or None if empty."""
        if not self._table_exists(table):
            return None
        iso_expr = self._date_iso_expr(table)
        row = self.conn.execute(
            f"SELECT MIN({iso_expr}) mn, MAX({iso_expr}) mx, COUNT(*) n FROM {table}"
        ).fetchone()
        if not row or row["n"] == 0:
            return {"min": None, "max": None, "rows": 0, "distinct_dates": 0}
        dcol = TABLES[table]["date"]
        drow = self.conn.execute(
            f"SELECT COUNT(DISTINCT {iso_expr}) d FROM {table}"
        ).fetchone()
        return {
            "min": row["mn"],
            "max": row["mx"],
            "rows": row["n"],
            "distinct_dates": drow["d"] if drow else 0,
        }

    # ------------------------------------------------------------------ #
    # Inventory / provenance
    # ------------------------------------------------------------------ #
    def inventory(self) -> List[Dict[str, Any]]:
        """One row per known table: bounds + date column. The data catalogue."""
        out: List[Dict[str, Any]] = []
        for table in TABLES:
            meta = TABLES[table]
            if not self._table_exists(table):
                out.append({
                    "table": table, "exists": False, "rows": 0,
                    "min_date": None, "max_date": None, "distinct_dates": 0,
                    "date_column": meta["date"],
                })
                continue
            b = self._date_bounds(table) or {}
            out.append({
                "table": table, "exists": True, "rows": b.get("rows", 0),
                "min_date": b.get("min"), "max_date": b.get("max"),
                "distinct_dates": b.get("distinct_dates", 0),
                "date_column": meta["date"],
            })
        return out

    def rows_for_date(self, table: str, compact: str, iso: Optional[str] = None) -> int:
        """Count rows a table holds for a given compact date (0 if none)."""
        if not self._table_exists(table):
            return 0
        iso = iso or _iso_from_compact(compact)
        clause, params = self._date_match(table, iso, compact)
        row = self.conn.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE {clause}", params
        ).fetchone()
        return int(row["n"]) if row else 0

    def nearest_date(self, table: str, compact: str, *, direction: str = "before") -> Optional[str]:
        """Nearest date present in a table relative to compact.

        direction 'before' -> latest date <= compact; 'after' -> earliest >=.
        The two stored lexicographic bases are searched separately (each branch
        is sargable) and the best candidate is chosen in Python.
        """
        if not self._table_exists(table):
            return None
        col = TABLES[table]["date"]
        iso = _iso_from_compact(compact)
        if TABLES[table]["date_style"] == "datetime":
            # Uniform 'YYYY-MM-DD HH:MM:SS': compare on the date portion via a
            # bounded range, then take min/max ts (sargable).
            if direction == "before":
                sql = f"SELECT MAX({col}) d FROM {table} WHERE {col}<=?"
                row = self.conn.execute(sql, (f"{iso} 23:59:59.999",)).fetchone()
            else:
                sql = f"SELECT MIN({col}) d FROM {table} WHERE {col}>=?"
                row = self.conn.execute(sql, (f"{iso} 00:00:00",)).fetchone()
            return row["d"][:10] if row and row["d"] else None
        # date/mixed: search ISO and compact bases separately.
        if direction == "before":
            ri = self.conn.execute(
                f"SELECT MAX({col}) d FROM {table} WHERE {col} LIKE '%-%' AND {col}<=?",
                (iso,)).fetchone()
            rc = self.conn.execute(
                f"SELECT MAX({col}) d FROM {table} WHERE {col} NOT LIKE '%-%' AND {col}<=?",
                (compact,)).fetchone()
            cands = [ri["d"] for ri in (ri,) if ri and ri["d"]]
            cands += [rc["d"] for rc in (rc,) if rc and rc["d"]]
            if not cands:
                return None
            return max(_any_to_iso(x) for x in cands)
        else:
            ri = self.conn.execute(
                f"SELECT MIN({col}) d FROM {table} WHERE {col} LIKE '%-%' AND {col}>=?",
                (iso,)).fetchone()
            rc = self.conn.execute(
                f"SELECT MIN({col}) d FROM {table} WHERE {col} NOT LIKE '%-%' AND {col}>=?",
                (compact,)).fetchone()
            cands = [ri["d"] for ri in (ri,) if ri and ri["d"]]
            cands += [rc["d"] for rc in (rc,) if rc and rc["d"]]
            if not cands:
                return None
            return min(_any_to_iso(x) for x in cands)

    # ------------------------------------------------------------------ #
    # Calendar
    # ------------------------------------------------------------------ #
    def calendar_open(self, compact: str) -> Optional[int]:
        if not self._table_exists("trade_calendar"):
            return None
        row = self.conn.execute(
            "SELECT is_open FROM trade_calendar WHERE cal_date=?", (compact,)
        ).fetchone()
        return int(row["is_open"]) if row else None

    def calendar_days(self, start_compact: str, end_compact: str,
                      only_open: bool = True) -> List[Dict[str, Any]]:
        if not self._table_exists("trade_calendar"):
            return []
        sql = ("SELECT cal_date, is_open FROM trade_calendar "
               "WHERE cal_date>=? AND cal_date<=? ")
        params: List[Any] = [start_compact, end_compact]
        if only_open:
            sql += "AND is_open=1 "
        sql += "ORDER BY cal_date ASC"
        rows = self._fetchall(sql, tuple(params))
        for r in rows:
            r["date"] = iso_of(r["cal_date"])
        return rows

    def calendar_nearest_open(self, compact: str, direction: str) -> Optional[str]:
        if not self._table_exists("trade_calendar"):
            return None
        op = "<" if direction == "before" else ">"
        order = "DESC" if direction == "before" else "ASC"
        row = self.conn.execute(
            f"SELECT cal_date d FROM trade_calendar WHERE is_open=1 AND cal_date{op}? "
            f"ORDER BY cal_date {order} LIMIT 1",
            (compact,),
        ).fetchone()
        return iso_of(row["d"]) if row else None

    # ------------------------------------------------------------------ #
    # Generic "table on date" / "table for code" readers
    # ------------------------------------------------------------------ #
    def read_on_date(self, table: str, compact: str,
                     limit: Optional[int] = None, order: Optional[str] = None) -> List[Dict[str, Any]]:
        cols = self._columns(table)
        select = ",".join(cols)
        iso = _iso_from_compact(compact)
        clause, params = self._date_match(table, iso, compact)
        sql = f"SELECT {select} FROM {table} WHERE {clause} "
        if order:
            sql += f"ORDER BY {order} "
        if limit:
            sql += "LIMIT ?"
            return self._fetchall(sql, params + (int(limit),))
        return self._fetchall(sql, params)

    def read_for_code(self, table: str, code: Any,
                      start: Optional[str] = None, end: Optional[str] = None,
                      limit: Optional[int] = None) -> List[Dict[str, Any]]:
        cols = self._columns(table)
        select = ",".join(cols)
        bare = normalize_code(code)
        clause, cparams = self._code_clause(table, bare)
        sql = f"SELECT {select} FROM {table} WHERE {clause} "
        params: List[Any] = list(cparams)
        if start and end:
            s, e = normalize_date(start), normalize_date(end)
            rclause, rparams = self._date_range(table, s[0], s[1], e[0], e[1])
            sql += f"AND {rclause} "
            params += list(rparams)
        elif start:
            s = normalize_date(start)
            rclause, rparams = self._date_range(table, s[0], s[1], "9999-12-31", "99991231")
            sql += f"AND {rclause} "
            params += list(rparams)
        elif end:
            e = normalize_date(end)
            rclause, rparams = self._date_range(table, "0001-01-01", "00010101", e[0], e[1])
            sql += f"AND {rclause} "
            params += list(rparams)
        sql += f"ORDER BY {TABLES[table]['date']} DESC "
        if limit:
            sql += "LIMIT ?"
            params.append(int(limit))
        return self._fetchall(sql, tuple(params))

    # ------------------------------------------------------------------ #
    # Specific convenience readers
    # ------------------------------------------------------------------ #
    def ohlcv_window(self, code: Any, start: str, end: str,
                     freq: str = "1d") -> List[Dict[str, Any]]:
        bare = normalize_code(code)
        s, e = normalize_date(start), normalize_date(end)
        rclause, rparams = self._date_range("ohlcv", s[0], s[1], e[0], e[1])
        sql = ("SELECT code,frequency,ts,open,high,low,close,volume,amount "
               "FROM ohlcv WHERE code=? AND frequency=? "
               f"AND {rclause} ORDER BY ts ASC")
        return self._fetchall(sql, (bare, freq) + rparams)

    def kv_get(self, namespace: str, key: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT namespace,key,payload,updated_at,ttl_seconds FROM kv_cache WHERE namespace=?"
        params: List[Any] = [namespace]
        if key:
            sql += " AND key=?"
            params.append(key)
        return self._fetchall(sql, tuple(params))

    def sentiment_rows(self, cache_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT cache_type,identifier,payload,updated_at,ttl_seconds FROM sentiment_cache"
        if cache_type:
            return self._fetchall(sql + " WHERE cache_type=?", (cache_type,))
        return self._fetchall(sql)

    def sync_log_rows(self, source: Optional[str] = None,
                      start: Optional[str] = None, end: Optional[str] = None,
                      limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT source,ts_code,ran_at,status,rows,error FROM sync_log WHERE 1=1"
        params: List[Any] = []
        if source:
            sql += " AND source=?"
            params.append(source)
        if start:
            sql += " AND ran_at>=?"
            params.append(iso_of(start))
        if end:
            sql += " AND ran_at<=?"
            params.append(iso_of(end) + "T23:59:59")
        sql += " ORDER BY ran_at DESC LIMIT ?"
        params.append(int(limit))
        return self._fetchall(sql, tuple(params))
