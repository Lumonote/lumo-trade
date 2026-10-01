# -*- coding: utf-8 -*-
"""板块 × 日序列聚合 —— moneyflow_dc 全市场行按板块预聚合并落表。

``aggregate_day`` 是纯函数(零 I/O,离线可测);读写与编排在同模块下半部分。

口径:
- 只取 ``top_n=0`` 全市场快照行,其它 top_n 是榜单快照,混入会重复计数。
- 金额一律归一到**万元**(``amount_unit='元'`` 的行乘 1e-4)。
- 成交额由 ``net_amount / (net_amount_rate/100)`` 反推(moneyflow_dc 无成交额列),
  净流入率接近 0 时不可靠,置 None。
- ``excess_vs_market`` = 板块等权涨跌幅 - **参与聚合的全市场**等权涨跌幅。

设计 spec: docs/superpowers/specs/2026-08-21-market-pulse-index-sector-turning-design.md
"""
from __future__ import annotations

import logging
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from data_store.connection import get_conn
from data_store.sector_map_repo import bare_code

logger = logging.getLogger(__name__)

# 净流入率绝对值低于该值时,反推成交额会被放大到失真
_RATE_EPS = 0.05

_COLUMNS = (
    "trade_date", "sector", "sector_type", "member_count", "net_amount",
    "net_rate_median", "pct_chg_mean", "breadth", "amount_median",
    "excess_vs_market", "seat_count", "provisional",
)


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def _unit_mult(unit: Any) -> float:
    """金额归一到万元。照抄 accumulation_detector 口径。"""
    return 1e-4 if str(unit or "万元") == "元" else 1.0


def _median_or_none(values: Sequence[float]) -> Optional[float]:
    return round(median(values), 4) if values else None


def aggregate_day(
    rows: Iterable[Mapping[str, Any]],
    mapping: Mapping[str, Sequence[Tuple[str, str]]],
    *,
    seat_counts: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """单日 moneyflow_dc 行 + 个股→板块映射 → 每板块一条 metrics(纯函数)。"""
    seats = seat_counts or {}
    buckets: Dict[Tuple[str, str], Dict[str, List[Any]]] = {}
    market_pcts: List[float] = []
    trade_date = ""

    for row in rows or []:
        if int(row.get("top_n") or 0) != 0:
            continue
        code = bare_code(row.get("ts_code"))
        sectors = mapping.get(code) or ()
        if not code or not sectors:
            continue
        pct = _num(row.get("pct_change"))
        if pct is None:
            continue
        trade_date = trade_date or str(row.get("trade_date") or "")
        mult = _unit_mult(row.get("amount_unit"))
        net = _num(row.get("net_amount"))
        net = None if net is None else net * mult
        rate = _num(row.get("net_amount_rate"))
        amount = (net / (rate / 100.0)) if (net is not None and rate is not None
                                            and abs(rate) >= _RATE_EPS) else None
        market_pcts.append(pct)
        seat = _num(seats.get(code)) or 0.0

        for sector, sector_type in sectors:
            bucket = buckets.setdefault((sector, sector_type), {
                "pct": [], "net": [], "rate": [], "amount": [], "seat": []})
            bucket["pct"].append(pct)
            if net is not None:
                bucket["net"].append(net)
            if rate is not None:
                bucket["rate"].append(rate)
            if amount is not None:
                bucket["amount"].append(amount)
            bucket["seat"].append(seat)

    if not buckets:
        return []

    market_mean = sum(market_pcts) / len(market_pcts) if market_pcts else 0.0
    records: List[Dict[str, Any]] = []
    for (sector, sector_type), bucket in buckets.items():
        pcts = bucket["pct"]
        pct_mean = sum(pcts) / len(pcts)
        records.append({
            "trade_date": trade_date,
            "sector": sector,
            "sector_type": sector_type,
            "member_count": len(pcts),
            "net_amount": round(sum(bucket["net"]), 4) if bucket["net"] else None,
            "net_rate_median": _median_or_none(bucket["rate"]),
            "pct_chg_mean": round(pct_mean, 4),
            "breadth": round(sum(1 for p in pcts if p > 0) / len(pcts), 4),
            "amount_median": _median_or_none(bucket["amount"]),
            "excess_vs_market": round(pct_mean - market_mean, 4),
            "seat_count": int(sum(bucket["seat"])),
        })
    return records


# ============================================================ 读写
def upsert_days(records: Iterable[Mapping[str, Any]], *, provisional: bool = False) -> int:
    """板块 metrics 落表(按 (trade_date, sector, sector_type) 覆盖)。"""
    payload = []
    for rec in records or []:
        if not rec.get("trade_date") or not rec.get("sector"):
            continue
        payload.append(tuple(
            int(bool(provisional)) if col == "provisional" else rec.get(col)
            for col in _COLUMNS))
    if not payload:
        return 0
    placeholders = ",".join("?" for _ in _COLUMNS)
    get_conn().executemany(
        f"INSERT OR REPLACE INTO sector_daily_metrics({','.join(_COLUMNS)}) "
        f"VALUES({placeholders})",
        payload,
    )
    return len(payload)


def load_series(sector: str, sector_type: str, *, end_date: Optional[str] = None,
                limit: int = 60) -> List[Dict[str, Any]]:
    """单板块日序列,按 trade_date 升序(最老在前)。"""
    end = (end_date or "9999-12-31").strip()
    rows = get_conn().execute(
        f"SELECT {','.join(_COLUMNS)} FROM sector_daily_metrics "
        "WHERE sector=? AND sector_type=? AND trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?",
        (sector, sector_type, end, int(limit)),
    ).fetchall()
    return [dict(zip(_COLUMNS, row)) for row in reversed(rows)]


def load_all_series(*, end_date: Optional[str] = None, limit: int = 60,
                    min_members: int = 5, include_stale: bool = False) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """全部板块的日序列。成分股不足 min_members 的板块直接剔除(噪声太大)。"""
    end = (end_date or "9999-12-31").strip()
    dates = [r[0] for r in get_conn().execute(
        "SELECT DISTINCT trade_date FROM sector_daily_metrics WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT ?", (end, int(limit)))]
    if not dates:
        return {}
    placeholders = ",".join("?" for _ in dates)
    rows = get_conn().execute(
        f"SELECT {','.join(_COLUMNS)} FROM sector_daily_metrics "
        f"WHERE trade_date IN ({placeholders}) ORDER BY trade_date ASC",
        tuple(dates),
    ).fetchall()
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        rec = dict(zip(_COLUMNS, row))
        out.setdefault((rec["sector"], rec["sector_type"]), []).append(rec)
    latest = max(dates)
    return {
        key: series for key, series in out.items()
        if series and (include_stale or series[-1]["trade_date"] == latest)
        and (series[-1]["member_count"] or 0) >= int(min_members)
    }


def _seat_counts_for(trade_date: str) -> Dict[str, int]:
    """当日量化异动席位数 + 龙虎榜上榜数(裸码 → 计数)。任一源失败按 0 计。"""
    counts: Dict[str, int] = {}
    conn = get_conn()
    try:
        for code, seat in conn.execute(
            "SELECT code, COALESCE(quant_seat,0) FROM quant_radar_stock_daily "
            "WHERE trade_date=?", (trade_date,)
        ):
            key = bare_code(code)
            if key:
                counts[key] = counts.get(key, 0) + int(seat or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("_seat_counts_for 量化席位失败: %s", exc)
    try:
        for (ts_code,) in conn.execute(
            "SELECT ts_code FROM dragon_tiger_list WHERE trade_date=?", (trade_date,)
        ):
            key = bare_code(ts_code)
            if key:
                counts[key] = counts.get(key, 0) + 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("_seat_counts_for 龙虎榜失败: %s", exc)
    return counts


def rebuild_day(trade_date: str, *, provisional: bool = False,
                mapping: Optional[Mapping[str, Any]] = None) -> int:
    """取当日 moneyflow_dc → 聚合 → 落表,返回写入板块条数。"""
    from data_store import sector_map_repo

    date = str(trade_date or "").strip()
    if not date:
        return 0
    cols = ("trade_date", "ts_code", "top_n", "pct_change", "net_amount",
            "net_amount_rate", "amount_unit")
    rows = [dict(zip(cols, r)) for r in get_conn().execute(
        f"SELECT {','.join(cols)} FROM moneyflow_dc WHERE trade_date=? AND top_n=0",
        (date,))]
    if not rows:
        return 0
    mp = mapping if mapping is not None else sector_map_repo.build_map(as_of=date)
    records = aggregate_day(rows, mp, seat_counts=_seat_counts_for(date))
    return upsert_days(records, provisional=provisional)


def available_dates(limit: int = 250) -> List[str]:
    return [r[0] for r in get_conn().execute(
        "SELECT DISTINCT trade_date FROM sector_daily_metrics "
        "ORDER BY trade_date DESC LIMIT ?", (int(limit),))]


# ============================================================ 盘中重算 / 收盘定稿
_FINALIZE_AFTER = "17:30"       # 东财资金流当日数据的稳定发布时点之后
_REFRESH_INTERVAL = 300
_finalize_thread: Optional[Any] = None


def _finalize_mapping(date: str) -> Mapping[str, Any]:
    from data_store import sector_map_repo

    return sector_map_repo.build_map(as_of=date)


def _cutoff_from_env() -> str:
    import os

    return os.environ.get("KRONOS_SECTOR_FINALIZE_AFTER", _FINALIZE_AFTER)


def _day_state(date: str) -> Dict[str, int]:
    """当日已有的板块记录情况:总条数与其中 provisional 条数。"""
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM sector_daily_metrics WHERE trade_date=?", (date,)).fetchone()[0]
    prov = conn.execute(
        "SELECT COUNT(*) FROM sector_daily_metrics WHERE trade_date=? AND provisional=1",
        (date,)).fetchone()[0]
    return {"total": int(total), "provisional": int(prov)}


def _has_flow(date: str) -> bool:
    return bool(get_conn().execute(
        "SELECT 1 FROM moneyflow_dc WHERE trade_date=? AND top_n=0 LIMIT 1",
        (date,)).fetchone())


def refresh_intraday(*, now: Optional[Any] = None) -> Dict[str, Any]:
    """盘中:用当日已有的 moneyflow_dc 行重算板块序列,标记 provisional=1。

    不做任何外部抓取 —— 当日资金流由既有的资金榜单回填链路维护。
    当日若已定稿(provisional 全为 0 且有记录),不回退为未定稿。
    """
    from datetime import datetime as _dt

    moment = now or _dt.now()
    date = moment.strftime("%Y-%m-%d")
    if not _has_flow(date):
        return {"ran": False, "date": date, "written": 0, "reason": "当日无资金流数据"}
    state = _day_state(date)
    if state["total"] and not state["provisional"]:
        return {"ran": False, "date": date, "written": 0, "reason": "当日已定稿"}
    written = rebuild_day(date, provisional=True, mapping=_finalize_mapping(date))
    logger.debug("板块日序列盘中重算 %s → %s 条", date, written)
    return {"ran": True, "date": date, "written": written, "reason": "ok"}


def finalize_once(*, now: Optional[Any] = None, force: bool = False) -> Dict[str, Any]:
    """收盘后把当日 provisional 数据重算为定稿。可注入 now,便于测试。"""
    from datetime import datetime as _dt

    moment = now or _dt.now()
    date = moment.strftime("%Y-%m-%d")
    if not force:
        cutoff = _cutoff_from_env()
        if moment.strftime("%H:%M") < cutoff:
            return {"ran": False, "date": date, "written": 0,
                    "reason": f"未到收盘定稿时点 {cutoff}"}
    if not _has_flow(date):
        return {"ran": False, "date": date, "written": 0, "reason": "当日无资金流数据"}
    state = _day_state(date)
    if state["total"] and not state["provisional"]:
        return {"ran": False, "date": date, "written": 0, "reason": "当日已定稿"}
    written = rebuild_day(date, provisional=False, mapping=_finalize_mapping(date))
    logger.info("板块日序列收盘定稿 %s → %s 条", date, written)
    return {"ran": True, "date": date, "written": written, "reason": "ok"}


def start_finalize_daemon() -> Optional[Any]:
    """守护线程:收盘前盘中重算,收盘后定稿。``KRONOS_DISABLE_SECTOR_FINALIZE=1`` 关闭。"""
    import os
    import threading

    global _finalize_thread
    if os.environ.get("KRONOS_DISABLE_SECTOR_FINALIZE") == "1":
        return None
    if _finalize_thread is not None and _finalize_thread.is_alive():
        return _finalize_thread
    interval = int(os.environ.get("KRONOS_SECTOR_REFRESH_INTERVAL", _REFRESH_INTERVAL))

    def _loop() -> None:
        import time as _time
        from datetime import datetime as _dt

        while True:
            try:
                if _dt.now().strftime("%H:%M") >= _cutoff_from_env():
                    finalize_once()
                else:
                    refresh_intraday()
            except Exception as exc:  # noqa: BLE001 — 守护线程不能死
                logger.debug("板块序列刷新失败: %s", exc)
            _time.sleep(interval)

    _finalize_thread = threading.Thread(target=_loop, name="sector-refresh", daemon=True)
    _finalize_thread.start()
    return _finalize_thread
