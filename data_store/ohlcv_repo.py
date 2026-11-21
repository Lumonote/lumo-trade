"""OHLCV repository: time-series candle data.

Schema: (code, frequency, ts) primary key. `frequency` is one of
'1d','5m','15m','30m','60m'. `ts` is ISO 'YYYY-MM-DD HH:MM:SS'.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

import pandas as pd

from data_store.connection import get_conn


_COLUMNS = ("timestamps", "open", "high", "low", "close", "volume", "amount")


def upsert_df(code: str, frequency: str, df: pd.DataFrame) -> int:
    """Insert-or-update rows for (code, frequency). Returns row count written.

    `df` must contain a 'timestamps' column plus open/high/low/close;
    volume/amount default to NULL if missing.
    """
    if df is None or len(df) == 0:
        return 0
    work = df.copy()
    if "timestamps" not in work.columns:
        raise ValueError("df missing required 'timestamps' column")
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col not in work.columns:
            work[col] = None
    work["ts"] = pd.to_datetime(work["timestamps"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    rows: Iterable = (
        (
            code,
            frequency,
            r.ts,
            _to_float(r.open),
            _to_float(r.high),
            _to_float(r.low),
            _to_float(r.close),
            _to_float(r.volume),
            _to_float(r.amount),
        )
        for r in work.itertuples(index=False)
    )
    conn = get_conn()
    cur = conn.executemany(
        """
        INSERT INTO ohlcv(code, frequency, ts, open, high, low, close, volume, amount)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code, frequency, ts) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low,
          close=excluded.close, volume=excluded.volume, amount=excluded.amount
        """,
        list(rows),
    )
    return cur.rowcount if cur.rowcount is not None else len(work)


def load_dataframe(code: str, frequency: str, limit: Optional[int] = None, *, start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
    """Return OHLCV as a DataFrame with the 7 canonical columns sorted ascending."""
    conn = get_conn()
    sql = (
        "SELECT ts AS timestamps, open, high, low, close, volume, amount "
        "FROM ohlcv WHERE code=? AND frequency=?"
    )
    params: tuple = (code, frequency)
    if start is not None:
        sql += " AND ts >= ?"
        params += (start,)
    if end is not None:
        sql += " AND ts < ?"
        params += ((date.fromisoformat(end) + timedelta(days=1)).isoformat(),)
    if limit:
        sql += " ORDER BY ts DESC LIMIT ?"
        params += (int(limit),)
    else:
        sql += " ORDER BY ts ASC"
    df = pd.read_sql_query(sql, conn, params=params, parse_dates=["timestamps"])
    if limit:
        df = df.iloc[::-1].reset_index(drop=True)
    return df


def row_count(code: str, frequency: str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM ohlcv WHERE code=? AND frequency=?",
        (code, frequency),
    ).fetchone()
    return int(row[0]) if row else 0


def delete(code: str, frequency: Optional[str] = None) -> int:
    conn = get_conn()
    if frequency:
        cur = conn.execute(
            "DELETE FROM ohlcv WHERE code=? AND frequency=?",
            (code, frequency),
        )
    else:
        cur = conn.execute("DELETE FROM ohlcv WHERE code=?", (code,))
    return cur.rowcount or 0


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f
