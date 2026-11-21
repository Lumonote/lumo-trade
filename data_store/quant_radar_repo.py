"""Quant radar per-day stock snapshot repository.

Table: quant_radar_stock_daily (schema v18). One row per (trade_date, code) —
每日量化活跃股五机制评分,由 quant_radar_service 在实时抓取成功后落库,
支持按天回看、按代码/名称/行业搜索、个股逐日历史。完整页面快照(温度计/
板块/changes_agg)仍存 kv_cache(namespace ``quant_radar``),本表只存可检索行。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Dict, List

from data_store.connection import get_conn

_FIELDS = (
    "trade_date", "code", "name", "industry",
    "activity", "level", "level_rank",
    "spoof", "hft", "orderbook", "sentiment", "bias",
    "changes_total", "changes_bull", "changes_bear", "quant_seat",
    "direction", "smash",
    "price", "change_pct", "volume_ratio", "turnover", "amplitude",
    "main_net_inflow", "badges", "reasons", "updated_at",
)
_PK = ("trade_date", "code")
_SCORE_KEYS = ("spoof", "hft", "orderbook", "sentiment", "bias")


def _date_key(value) -> str:
    """'20260710' / '2026-07-10' → ISO 'YYYY-MM-DD'。"""
    s = str(value or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def upsert_day(trade_date: str, items: List[Dict[str, Any]]) -> int:
    """量化雷达 stock item(service `_light_stock_item` 形状) 批量落库。"""
    date_iso = _date_key(trade_date)
    if not date_iso or not items:
        return 0
    now = _dt.datetime.now().isoformat(timespec="seconds")
    values = []
    for item in items:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        scores = item.get("scores") or {}
        values.append((
            date_iso, code,
            str(item.get("name") or ""), str(item.get("industry") or ""),
            _int(item.get("activity")), str(item.get("level") or ""),
            _int(item.get("level_rank")),
            *(_int(scores.get(k)) for k in _SCORE_KEYS),
            _int(item.get("changes_total")), _int(item.get("changes_bull")),
            _int(item.get("changes_bear")), int(bool(item.get("quant_seat"))),
            str(item.get("direction") or ""), _int(item.get("smash")),
            _num(item.get("price")), _num(item.get("change_pct")),
            _num(item.get("volume_ratio")), _num(item.get("turnover")),
            _num(item.get("amplitude")), _num(item.get("main_net_inflow")),
            json.dumps(list(item.get("badges") or []), ensure_ascii=False),
            json.dumps(list(item.get("reasons") or []), ensure_ascii=False),
            now,
        ))
    if not values:
        return 0
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _FIELDS if c not in _PK)
    cur = get_conn().executemany(
        f"""
        INSERT INTO quant_radar_stock_daily({",".join(_FIELDS)})
        VALUES({",".join("?" * len(_FIELDS))})
        ON CONFLICT({",".join(_PK)}) DO UPDATE SET {set_clause}
        """,
        values,
    )
    return cur.rowcount if cur.rowcount else len(values)


def _row_to_item(row) -> Dict[str, Any]:
    item = dict(row)
    item["quant_seat"] = bool(item.get("quant_seat"))
    item["scores"] = {k: _int(item.pop(k, 0)) for k in _SCORE_KEYS}
    for col in ("badges", "reasons"):
        try:
            item[col] = json.loads(item.get(col) or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            item[col] = []
    return item


def get_day(trade_date: str, limit: int = 200, q: str = "",
            min_activity: int = 0, direction: str = "") -> List[Dict[str, Any]]:
    """某日榜单(activity 降序),可按代码前缀/名称/行业关键字与方向(砸盘/拉抬/拉锯)过滤。

    ``limit <= 0`` 返回该日全量(单日约 2500 行,由前端分页展示)。
    """
    date_iso = _date_key(trade_date)
    if not date_iso:
        return []
    sql = f"SELECT {','.join(_FIELDS)} FROM quant_radar_stock_daily WHERE trade_date=?"
    params: List[Any] = [date_iso]
    keyword = str(q or "").strip()
    if keyword:
        sql += " AND (code LIKE ? OR name LIKE ? OR industry LIKE ?)"
        params.extend((f"{keyword}%", f"%{keyword}%", f"%{keyword}%"))
    if min_activity:
        sql += " AND activity >= ?"
        params.append(int(min_activity))
    direction_kw = str(direction or "").strip()
    if direction_kw:
        sql += " AND direction = ?"
        params.append(direction_kw)
    sql += (" ORDER BY smash DESC, activity DESC" if direction_kw == "砸盘"
            else " ORDER BY activity DESC, changes_total DESC")
    limit_n = _int(limit, 0)
    if limit_n > 0:
        sql += " LIMIT ?"
        params.append(min(limit_n, 5000))
    return [_row_to_item(r) for r in get_conn().execute(sql, params)]


def _trade_days(limit: int) -> List[str]:
    """最近 ``limit`` 个交易日 YYYYMMDD(最新在前)。

    本地交易日历优先;日历最新日落后今天超过 14 个自然日视为过期(历史上
    dev 库日历停在 5 月,直接取会把历史窗口拖回几个月前),退化为工作日近似。
    """
    try:
        from data_store import calendar_repo

        today = _dt.date.today()
        today_key = today.strftime("%Y%m%d")
        opens = [d for d in calendar_repo.open_days() if str(d) <= today_key]
        if opens:
            latest = str(opens[-1])
            try:
                stale = (today - _dt.date(int(latest[:4]), int(latest[4:6]), int(latest[6:8]))).days > 14
            except (TypeError, ValueError):
                stale = True
            if not stale:
                return [str(d) for d in opens[-limit:][::-1]]
    except Exception:
        pass
    day, out = _dt.date.today(), []
    while len(out) < limit:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day -= _dt.timedelta(days=1)
    return out


def get_stock_history(code: str, days: int = 60) -> List[Dict[str, Any]]:
    """个股逐日量化行为历史,按交易日历连续合成(最新在前)。

    按日表只落当天有盘口异动/活跃的股票,直接查表会把「无异动日」和「整个
    量化雷达当天没运行的日子」都显示成空白,看起来像数据缺失。这里按交易日
    历补全三态:表内有行 → 原样;页面运行过但该股无异动 → ``status="no_activity"``
    占位行;当天没运行 → ``status="unrecorded"`` 占位行。占位行的涨跌%从
    moneyflow_dc 补当天真实值(无则空)。
    """
    core = str(code or "").split(".")[0].strip()
    if not core:
        return []
    limit = max(1, min(int(days or 60), 365))
    rows = {_row_to_item(r)["trade_date"]: _row_to_item(r) for r in get_conn().execute(
        f"SELECT {','.join(_FIELDS)} FROM quant_radar_stock_daily "
        "WHERE code=? ORDER BY trade_date DESC LIMIT ?",
        (core, limit),
    )}
    calendar = _trade_days(limit)
    if not calendar:
        return [rows[d] for d in sorted(rows, reverse=True)]
    recorded = {str(r[0]) for r in get_conn().execute(
        "SELECT DISTINCT trade_date FROM quant_radar_stock_daily")}
    # 涨跌%回填:无异动/未运行日也从 moneyflow_dc 拿当天真实涨跌,占位行不显空
    pct_by_day: Dict[str, Any] = {}
    try:
        placeholders = ",".join("?" * len(calendar))
        for r in get_conn().execute(
            f"SELECT trade_date, pct_change FROM moneyflow_dc "
            f"WHERE top_n=0 AND (ts_code=? OR ts_code LIKE ?) "
            f"AND trade_date IN ({placeholders})",
            (core, f"{core}.%", *[_date_key(d) for d in calendar]),
        ):
            pct_by_day[str(r[0])] = r[1]
    except Exception:
        pass
    out: List[Dict[str, Any]] = []
    for day in calendar:
        iso = _date_key(day)
        if iso in rows:
            out.append(rows[iso])
            continue
        status = "no_activity" if iso in recorded else "unrecorded"
        out.append({
            "code": core, "trade_date": iso, "status": status,
            "activity": 0, "level": "", "level_rank": 0, "scores": {},
            "badges": [], "reasons": [], "changes_total": 0, "changes_bull": 0,
            "changes_bear": 0, "quant_seat": False, "direction": "", "smash": 0,
            "change_pct": pct_by_day.get(iso),
        })
    return out


def get_stock_range(code: str, start: str, end: str, calendar: List[str]) -> List[Dict[str, Any]]:
    """按指定区间读取三态历史；不把历史查询锚定在今天。"""
    core = str(code).split(".")[0]
    rows = {r["trade_date"]: _row_to_item(r) for r in get_conn().execute(
        f"SELECT {','.join(_FIELDS)} FROM quant_radar_stock_daily "
        "WHERE (code=? OR code=?) AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (core, code, start, end),
    )}
    recorded = {r[0] for r in get_conn().execute(
        "SELECT DISTINCT trade_date FROM quant_radar_stock_daily WHERE trade_date BETWEEN ? AND ?", (start, end))}
    dates = sorted({d for d in map(_date_key, calendar) if start <= d <= end} | recorded | rows.keys())
    return [dict(rows[d], status="observed") if d in rows else {
        "code": core, "trade_date": d, "status": "no_activity" if d in recorded else "unrecorded",
        "activity": 0 if d in recorded else None, "changes_bull": 0 if d in recorded else None,
        "changes_bear": 0 if d in recorded else None,
    } for d in dates]


def date_bounds():
    """实际采集区间，供历史查询导航；不受当前查询区间影响。"""
    row = get_conn().execute("SELECT MIN(trade_date), MAX(trade_date) FROM quant_radar_stock_daily").fetchone()
    return row[0], row[1]


def latest_in_range(start: str, end: str) -> List[Dict[str, Any]]:
    """Each stock's latest actual observation within the selected dates."""
    rows = get_conn().execute('''
        WITH latest AS (SELECT code, MAX(trade_date) AS trade_date FROM quant_radar_stock_daily
                        WHERE trade_date BETWEEN ? AND ? GROUP BY code)
        SELECT q.code,q.trade_date,q.name,q.industry,q.activity,q.direction,q.changes_bull,q.changes_bear
        FROM latest JOIN quant_radar_stock_daily q USING (code, trade_date)
        ORDER BY q.trade_date DESC, q.activity DESC, q.code
    ''', (start, end))
    return [dict(row) for row in rows]


def list_dates(limit: int = 120) -> List[str]:
    """有按日榜单数据的日期(最新在前)。"""
    rows = get_conn().execute(
        "SELECT DISTINCT trade_date FROM quant_radar_stock_daily "
        "ORDER BY trade_date DESC LIMIT ?",
        (max(1, min(int(limit or 120), 1000)),),
    )
    return [r[0] for r in rows]


def count() -> int:
    row = get_conn().execute("SELECT COUNT(*) FROM quant_radar_stock_daily").fetchone()
    return int(row[0]) if row else 0
