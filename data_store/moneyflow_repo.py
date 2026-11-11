"""Moneyflow ranking (top-N) repository.

Mirrors `data/moneyflow_dc_YYYYMMDD_topN.csv`. Each row keyed by
(trade_date, ts_code, top_n).
"""
from __future__ import annotations

import json
from typing import Iterable

import pandas as pd

from data_store.connection import get_conn


_FIELDS = (
    "trade_date", "ts_code", "top_n",
    "name", "pct_change", "close",
    "net_amount", "net_amount_rate",
    "buy_elg_amount", "buy_elg_amount_rate",
    "buy_lg_amount",  "buy_lg_amount_rate",
    "buy_md_amount",  "buy_md_amount_rate",
    "buy_sm_amount",  "buy_sm_amount_rate",
    "amount_unit", "raw_json",
)


def upsert_df(df: pd.DataFrame, top_n: int) -> int:
    if df is None or df.empty:
        return 0
    work = df.copy()
    work["top_n"] = int(top_n)
    if "_amount_unit" in work.columns and "amount_unit" not in work.columns:
        work["amount_unit"] = work["_amount_unit"]
    # 双格式混存修复:写入边界统一归一为 ISO(YYYY-MM-DD),与读函数的 _date_key 对称
    work["trade_date"] = work["trade_date"].astype(str).map(_date_key)
    work["ts_code"] = work["ts_code"].astype(str)
    work["raw_json"] = work.apply(_row_raw_json, axis=1)
    for col in _FIELDS:
        if col not in work.columns:
            work[col] = None
    rows: Iterable = (tuple(_to_native(getattr(r, c)) for c in _FIELDS) for r in work.itertuples(index=False))
    placeholders = ",".join("?" * len(_FIELDS))
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _FIELDS if c not in ("trade_date", "ts_code", "top_n"))
    cur = get_conn().executemany(
        f"""
        INSERT INTO moneyflow_dc({",".join(_FIELDS)}) VALUES({placeholders})
        ON CONFLICT(trade_date, ts_code, top_n) DO UPDATE SET {set_clause}
        """,
        list(rows),
    )
    return cur.rowcount if cur.rowcount is not None else len(work)


def get_top_n(trade_date: str, top_n: int) -> pd.DataFrame:
    return pd.read_sql_query(
        f"SELECT {','.join(_FIELDS)} FROM moneyflow_dc WHERE trade_date=? AND top_n=?",
        get_conn(),
        params=(_date_key(trade_date), int(top_n)),
    )


def get_ranking(trade_date: str, limit: int = 50, snapshot_top_n: int = 0) -> pd.DataFrame:
    """资金榜单日:在 snapshot_top_n 快照内按主力净流入降序取前 limit。

    snapshot_top_n=0 约定为「资金榜全市场快照」,与 opportunity discovery 写入的
    正 top_n 快照互不干扰。"""
    return pd.read_sql_query(
        f"""
        SELECT {','.join(_FIELDS)} FROM moneyflow_dc
        WHERE trade_date=? AND top_n=?
        ORDER BY net_amount DESC
        LIMIT ?
        """,
        get_conn(),
        params=(_date_key(trade_date), int(snapshot_top_n), int(limit)),
    )


def get_aggregated(
    end_date: str,
    days: int = 5,
    limit: int = 50,
    snapshot_top_n: int = 0,
    sort_by: str = "net_amount",
) -> pd.DataFrame:
    """资金榜多日聚合:end_date 及之前最近 days 个有数据交易日,按累计主力净流入
    降序取前 limit;list_count = 窗口内上榜天数(去重日期)。

    ``main_buy_amount`` 是旧前端兼容字段；moneyflow_dc 的主排序口径应使用
    ``net_amount``(主力净流入),不要把它解释成独立的成交买入额。
    """
    order_expr = "net_amount DESC, main_buy_amount DESC"
    return pd.read_sql_query(
        """
        WITH recent_dates AS (
            SELECT DISTINCT trade_date FROM moneyflow_dc
            WHERE trade_date <= ? AND top_n = ?
            ORDER BY trade_date DESC
            LIMIT ?
        )
        SELECT ts_code,
               MAX(name)              AS name,
               MAX(close)             AS close,
               MAX(pct_change)        AS pct_change,
               SUM(net_amount)        AS net_amount,
               AVG(net_amount_rate)   AS net_amount_rate,
               SUM(buy_elg_amount)    AS buy_elg_amount,
               AVG(buy_elg_amount_rate) AS buy_elg_amount_rate,
               SUM(buy_lg_amount)     AS buy_lg_amount,
               AVG(buy_lg_amount_rate) AS buy_lg_amount_rate,
               SUM(buy_md_amount)     AS buy_md_amount,
               AVG(buy_md_amount_rate) AS buy_md_amount_rate,
               SUM(buy_sm_amount)     AS buy_sm_amount,
               AVG(buy_sm_amount_rate) AS buy_sm_amount_rate,
               SUM(COALESCE(buy_elg_amount, 0) + COALESCE(buy_lg_amount, 0)) AS main_buy_amount,
               SUM(COALESCE(buy_md_amount, 0) + COALESCE(buy_sm_amount, 0)) AS retail_buy_amount,
               MAX(amount_unit)       AS amount_unit,
               COUNT(DISTINCT trade_date) AS list_count,
               MIN(trade_date) AS first_date,
               MAX(trade_date) AS last_date,
               GROUP_CONCAT(raw_json, '\n') AS raw_json
        FROM moneyflow_dc
        WHERE top_n = ? AND trade_date IN (SELECT trade_date FROM recent_dates)
        GROUP BY ts_code
        ORDER BY {order_expr}
        LIMIT ?
        """.format(order_expr=order_expr),
        get_conn(),
        params=(_date_key(end_date), int(snapshot_top_n), int(days), int(snapshot_top_n), int(limit)),
    )


def get_range_aggregated(
    start_date: str,
    end_date: str,
    limit: int = 50,
    snapshot_top_n: int = 0,
    sort_by: str = "net_amount",
) -> pd.DataFrame:
    """资金榜日期区间聚合:按 [start_date, end_date] 内累计主力净流入排序。"""
    order_expr = "net_amount DESC, main_buy_amount DESC"
    return pd.read_sql_query(
        """
        SELECT ts_code,
               MAX(name)              AS name,
               MAX(close)             AS close,
               MAX(pct_change)        AS pct_change,
               SUM(net_amount)        AS net_amount,
               AVG(net_amount_rate)   AS net_amount_rate,
               SUM(buy_elg_amount)    AS buy_elg_amount,
               AVG(buy_elg_amount_rate) AS buy_elg_amount_rate,
               SUM(buy_lg_amount)     AS buy_lg_amount,
               AVG(buy_lg_amount_rate) AS buy_lg_amount_rate,
               SUM(buy_md_amount)     AS buy_md_amount,
               AVG(buy_md_amount_rate) AS buy_md_amount_rate,
               SUM(buy_sm_amount)     AS buy_sm_amount,
               AVG(buy_sm_amount_rate) AS buy_sm_amount_rate,
               SUM(COALESCE(buy_elg_amount, 0) + COALESCE(buy_lg_amount, 0)) AS main_buy_amount,
               SUM(COALESCE(buy_md_amount, 0) + COALESCE(buy_sm_amount, 0)) AS retail_buy_amount,
               MAX(amount_unit)       AS amount_unit,
               COUNT(DISTINCT trade_date) AS list_count,
               MIN(trade_date) AS first_date,
               MAX(trade_date) AS last_date,
               GROUP_CONCAT(raw_json, '\n') AS raw_json
        FROM moneyflow_dc
        WHERE top_n = ? AND trade_date >= ? AND trade_date <= ?
        GROUP BY ts_code
        ORDER BY {order_expr}
        LIMIT ?
        """.format(order_expr=order_expr),
        get_conn(),
        params=(int(snapshot_top_n), _date_key(start_date), _date_key(end_date), int(limit)),
    )


def get_stock_aggregated(
    ts_code: str,
    end_date: str | None = None,
    days: int = 5,
    snapshot_top_n: int = 0,
) -> pd.DataFrame:
    """个股资金榜近 N 个有数据交易日聚合。

    rank 为该股在同窗口全市场累计主力净流入额榜单中的名次,而不是过滤后名次。
    """
    core = _bare_code(ts_code)
    as_of = _date_key(end_date) or str(latest_date(snapshot_top_n) or "")
    if not core or not as_of:
        return pd.DataFrame()
    return pd.read_sql_query(
        """
        WITH recent_dates AS (
            SELECT DISTINCT trade_date FROM moneyflow_dc
            WHERE trade_date <= ? AND top_n = ?
            ORDER BY trade_date DESC
            LIMIT ?
        ),
        grouped AS (
            SELECT ts_code,
                   MAX(name)              AS name,
                   MAX(close)             AS close,
                   MAX(pct_change)        AS pct_change,
                   SUM(net_amount)        AS net_amount,
                   AVG(net_amount_rate)   AS net_amount_rate,
                   SUM(buy_elg_amount)    AS buy_elg_amount,
                   AVG(buy_elg_amount_rate) AS buy_elg_amount_rate,
                   SUM(buy_lg_amount)     AS buy_lg_amount,
                   AVG(buy_lg_amount_rate) AS buy_lg_amount_rate,
                   SUM(buy_md_amount)     AS buy_md_amount,
                   AVG(buy_md_amount_rate) AS buy_md_amount_rate,
                   SUM(buy_sm_amount)     AS buy_sm_amount,
                   AVG(buy_sm_amount_rate) AS buy_sm_amount_rate,
                   SUM(COALESCE(buy_elg_amount, 0) + COALESCE(buy_lg_amount, 0)) AS main_buy_amount,
                   SUM(COALESCE(buy_md_amount, 0) + COALESCE(buy_sm_amount, 0)) AS retail_buy_amount,
                   MAX(amount_unit)       AS amount_unit,
                   COUNT(DISTINCT trade_date) AS list_count,
                   MIN(trade_date) AS first_date,
                   MAX(trade_date) AS last_date,
                   GROUP_CONCAT(raw_json, '\n') AS raw_json
            FROM moneyflow_dc
            WHERE top_n = ? AND trade_date IN (SELECT trade_date FROM recent_dates)
            GROUP BY ts_code
        ),
        ranked AS (
            SELECT grouped.*,
                   RANK() OVER (ORDER BY net_amount DESC) AS market_rank
            FROM grouped
        )
        SELECT * FROM ranked
        WHERE ts_code=? OR ts_code LIKE ?
        """,
        get_conn(),
        params=(as_of, int(snapshot_top_n), int(days), int(snapshot_top_n), core, f"{core}.%"),
    )


def get_stock_range_aggregated(
    ts_code: str,
    start_date: str,
    end_date: str,
    snapshot_top_n: int = 0,
) -> pd.DataFrame:
    """个股资金榜日期区间聚合,rank 为区间全市场累计主力净流入额名次。"""
    core = _bare_code(ts_code)
    if not core or not start_date or not end_date:
        return pd.DataFrame()
    return pd.read_sql_query(
        """
        WITH grouped AS (
            SELECT ts_code,
                   MAX(name)              AS name,
                   MAX(close)             AS close,
                   MAX(pct_change)        AS pct_change,
                   SUM(net_amount)        AS net_amount,
                   AVG(net_amount_rate)   AS net_amount_rate,
                   SUM(buy_elg_amount)    AS buy_elg_amount,
                   AVG(buy_elg_amount_rate) AS buy_elg_amount_rate,
                   SUM(buy_lg_amount)     AS buy_lg_amount,
                   AVG(buy_lg_amount_rate) AS buy_lg_amount_rate,
                   SUM(buy_md_amount)     AS buy_md_amount,
                   AVG(buy_md_amount_rate) AS buy_md_amount_rate,
                   SUM(buy_sm_amount)     AS buy_sm_amount,
                   AVG(buy_sm_amount_rate) AS buy_sm_amount_rate,
                   SUM(COALESCE(buy_elg_amount, 0) + COALESCE(buy_lg_amount, 0)) AS main_buy_amount,
                   SUM(COALESCE(buy_md_amount, 0) + COALESCE(buy_sm_amount, 0)) AS retail_buy_amount,
                   MAX(amount_unit)       AS amount_unit,
                   COUNT(DISTINCT trade_date) AS list_count,
                   MIN(trade_date) AS first_date,
                   MAX(trade_date) AS last_date,
                   GROUP_CONCAT(raw_json, '\n') AS raw_json
            FROM moneyflow_dc
            WHERE top_n = ? AND trade_date >= ? AND trade_date <= ?
            GROUP BY ts_code
        ),
        ranked AS (
            SELECT grouped.*,
                   RANK() OVER (ORDER BY net_amount DESC) AS market_rank
            FROM grouped
        )
        SELECT * FROM ranked
        WHERE ts_code=? OR ts_code LIKE ?
        """,
        get_conn(),
        params=(int(snapshot_top_n), _date_key(start_date), _date_key(end_date), core, f"{core}.%"),
    )


def get_stock_rows(
    ts_code: str,
    start_date: str,
    end_date: str,
    snapshot_top_n: int = 0,
) -> pd.DataFrame:
    """个股资金榜原始日记录,按日期倒序返回。"""
    core = _bare_code(ts_code)
    if not core or not start_date or not end_date:
        return pd.DataFrame()
    return pd.read_sql_query(
        f"""
        SELECT {','.join(_FIELDS)} FROM moneyflow_dc
        WHERE top_n=? AND trade_date >= ? AND trade_date <= ?
          AND (ts_code=? OR ts_code LIKE ?)
        ORDER BY trade_date DESC
        """,
        get_conn(),
        params=(int(snapshot_top_n), _date_key(start_date), _date_key(end_date), core, f"{core}.%"),
    )


def get_market_window(end_date: str, days: int, snapshot_top_n: int = 0) -> pd.DataFrame:
    """近 N 个交易日(<= end_date)的全市场资金流行,按 trade_date 升序。

    供吸筹检测按窗口批量扫描(约 5000 股 × N 日);days 上限 250。
    """
    end_iso = _date_key(end_date)
    n = max(1, min(int(days or 1), 250))
    if not end_iso:
        return pd.DataFrame()
    dates = [r[0] for r in get_conn().execute(
        """
        SELECT DISTINCT trade_date FROM moneyflow_dc
        WHERE top_n=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?
        """,
        (int(snapshot_top_n), end_iso, n),
    )]
    if not dates:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in dates)
    return pd.read_sql_query(
        f"""
        SELECT {','.join(_FIELDS)} FROM moneyflow_dc
        WHERE top_n=? AND trade_date IN ({placeholders})
        ORDER BY trade_date ASC
        """,
        get_conn(),
        params=(int(snapshot_top_n), *dates),
    )


_LEAN_FIELDS = (
    "ts_code", "name", "pct_change", "close", "net_amount", "net_amount_rate",
    "buy_elg_amount", "buy_elg_amount_rate", "buy_lg_amount", "buy_lg_amount_rate",
    "buy_md_amount", "buy_md_amount_rate", "buy_sm_amount", "buy_sm_amount_rate",
)


def recent_dates(days: int, snapshot_top_n: int = 0) -> list[str]:
    """最近 N 个有数据的交易日(升序 ISO)。"""
    rows = get_conn().execute(
        """
        SELECT DISTINCT trade_date FROM moneyflow_dc
        WHERE top_n=? ORDER BY trade_date DESC LIMIT ?
        """,
        (int(snapshot_top_n), max(1, int(days or 1))),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def get_day_lean(trade_date: str, snapshot_top_n: int = 0) -> list[dict]:
    """单个交易日的全市场行,只取数值列(不带 raw_json)。

    供需要长窗口全市场面板的调用方(牛股挖掘 61 日 × 全市场)逐日装载:
    走主键前缀定位,且不读每行约 400 字节的 raw_json,比 get_market_window 轻一个量级。
    """
    rows = get_conn().execute(
        f"SELECT {','.join(_LEAN_FIELDS)} FROM moneyflow_dc WHERE trade_date=? AND top_n=?",
        (_date_key(trade_date), int(snapshot_top_n)),
    ).fetchall()
    return [dict(zip(_LEAN_FIELDS, row)) for row in rows]


def latest_date(top_n: int = None):
    if top_n is None:
        row = get_conn().execute("SELECT MAX(trade_date) FROM moneyflow_dc").fetchone()
    else:
        row = get_conn().execute(
            "SELECT MAX(trade_date) FROM moneyflow_dc WHERE top_n=?", (int(top_n),)
        ).fetchone()
    return row[0] if row and row[0] else None


def existing_dates(dates, snapshot_top_n: int = 0) -> set[str]:
    """Return ISO trade dates already stored for the given snapshot bucket."""
    return set(date_counts(dates, snapshot_top_n))


def date_counts(dates, snapshot_top_n: int = 0) -> dict[str, int]:
    """Rows stored per ISO trade date (dates with no rows are absent).

    Callers that need to tell a *complete* day from one whose fetch was
    truncated compare these counts; presence alone can't distinguish them.
    """
    normalized = [_date_key(d) for d in (dates or []) if _date_key(d)]
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = get_conn().execute(
        f"""
        SELECT trade_date, COUNT(*) FROM moneyflow_dc
        WHERE top_n=? AND trade_date IN ({placeholders})
        GROUP BY trade_date
        """,
        (int(snapshot_top_n), *normalized),
    ).fetchall()
    return {str(row[0]): int(row[1] or 0) for row in rows}


def count() -> int:
    row = get_conn().execute("SELECT COUNT(*) FROM moneyflow_dc").fetchone()
    return int(row[0]) if row else 0


def _to_native(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def _date_key(value) -> str:
    s = str(value or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _bare_code(ts_code: str) -> str:
    return str(ts_code or "").split(".")[0].strip()


def _row_raw_json(row) -> str:
    payload = {
        str(c): _to_native(row.get(c))
        for c in row.index
        if str(c) != "raw_json"
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
