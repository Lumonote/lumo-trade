"""条件选股日K / 形态特征本地缓存。

复用 ``kv_cache``(payload=JSON, 带 ttl_seconds + updated_at),按 6 位代码存两份:

- ``namespace='screener_kline'``  payload = 日K records 列表(升序)
- ``namespace='screener_feat'``   payload = :func:`tech_features` 结果字典

TTL 按交易时段区分:盘中当日 bar 还在变 → 2 分钟;收盘后 / 非交易日 → 6 小时。
这样同一批股票重复选股、以及换一组条件再选,都只读本地库,不再打新浪接口。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional, Tuple

from data_store.connection import get_conn

NS_KLINE = "screener_kline"
NS_FEAT = "screener_feat"

INTRADAY_TTL = 120        # 盘中:当日 K 线仍在变化
OFFHOUR_TTL = 6 * 3600    # 收盘后/非交易日:当日 K 线已定型
STALE_KEEP_DAYS = 10      # 清理阈值:超过 N 天未更新的条目在 purge 时删除


def _now() -> _dt.datetime:
    return _dt.datetime.now()


def is_trading_window(moment: Optional[_dt.datetime] = None) -> bool:
    """粗略判断是否处于 A 股连续竞价时段(工作日 09:10–15:10,不含节假日日历)。"""
    moment = moment or _now()
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return 9 * 60 + 10 <= minutes <= 15 * 60 + 10


def ttl_seconds(moment: Optional[_dt.datetime] = None) -> int:
    """当前时点应使用的缓存有效期。"""
    return INTRADAY_TTL if is_trading_window(moment) else OFFHOUR_TTL


def _load(namespace: str, code: str, ttl: Optional[int] = None) -> Optional[Any]:
    """读一条未过期的 JSON;过期/缺失/损坏 → None(过期行留给 purge 清理)。"""
    try:
        row = get_conn().execute(
            "SELECT payload, updated_at, ttl_seconds FROM kv_cache WHERE namespace=? AND key=?",
            (namespace, str(code)),
        ).fetchone()
    except Exception:  # noqa: BLE001 — 缓存不可用不应影响选股主流程
        return None
    if not row:
        return None
    payload_text, updated_at, stored_ttl = row
    try:
        age = (_now() - _dt.datetime.fromisoformat(str(updated_at))).total_seconds()
    except (TypeError, ValueError):
        return None
    limit = ttl if ttl is not None else int(stored_ttl or 0)
    if limit > 0 and age > limit:
        return None
    try:
        import json

        return json.loads(payload_text)
    except Exception:  # noqa: BLE001
        return None


def get_kline(code: str, ttl: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
    data = _load(NS_KLINE, code, ttl)
    return data if isinstance(data, list) and data else None


def get_feature(code: str, ttl: Optional[int] = None) -> Optional[Dict[str, Any]]:
    data = _load(NS_FEAT, code, ttl)
    return data if isinstance(data, dict) and data else None


def _upsert_many(namespace: str, items: Iterable[Tuple[str, Any]], ttl: int) -> int:
    """单事务批量写入(主线程调用,避免多线程 sqlite 写锁竞争)。"""
    import json

    stamp = _now().isoformat(timespec="seconds")
    rows = []
    for code, payload in items:
        if not code or payload in (None, [], {}):
            continue
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            continue
        rows.append((namespace, str(code), text, stamp, int(ttl)))
    if not rows:
        return 0
    try:
        conn = get_conn()
        conn.executemany(
            """
            INSERT INTO kv_cache(namespace, key, payload, updated_at, ttl_seconds)
            VALUES(?,?,?,?,?)
            ON CONFLICT(namespace, key) DO UPDATE SET
              payload=excluded.payload, updated_at=excluded.updated_at,
              ttl_seconds=excluded.ttl_seconds
            """,
            rows,
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        return 0
    return len(rows)


def put_many(records: Iterable[Tuple[str, Optional[List[Dict[str, Any]]],
                                     Optional[Dict[str, Any]]]],
             ttl: Optional[int] = None) -> int:
    """一次写入一批 (code, bars, features);None 项跳过。返回写入条目数。"""
    ttl = ttl_seconds() if ttl is None else ttl
    records = list(records)
    bars_items = [(code, bars) for code, bars, _ in records]
    feat_items = [(code, feat) for code, _, feat in records]
    return _upsert_many(NS_KLINE, bars_items, ttl) + _upsert_many(NS_FEAT, feat_items, ttl)


def put_kline(code: str, bars: List[Dict[str, Any]], ttl: Optional[int] = None) -> int:
    return _upsert_many(NS_KLINE, [(code, bars)], ttl_seconds() if ttl is None else ttl)


def put_feature(code: str, feat: Dict[str, Any], ttl: Optional[int] = None) -> int:
    return _upsert_many(NS_FEAT, [(code, feat)], ttl_seconds() if ttl is None else ttl)


def purge(stale_days: int = STALE_KEEP_DAYS) -> int:
    """删除过期过久的行,避免缓存无限增长。返回删除条数。"""
    cutoff = (_now() - _dt.timedelta(days=max(1, stale_days))).isoformat(timespec="seconds")
    try:
        cur = get_conn().execute(
            "DELETE FROM kv_cache WHERE namespace IN (?,?) AND updated_at < ?",
            (NS_KLINE, NS_FEAT, cutoff),
        )
        get_conn().commit()
    except Exception:  # noqa: BLE001
        return 0
    return int(cur.rowcount or 0)


def stats() -> Dict[str, Any]:
    """缓存规模(供 /api/screener/meta 展示)。"""
    out: Dict[str, Any] = {"kline": 0, "feature": 0, "newest": ""}
    try:
        conn = get_conn()
        for ns, key in ((NS_KLINE, "kline"), (NS_FEAT, "feature")):
            row = conn.execute(
                "SELECT COUNT(*), MAX(updated_at) FROM kv_cache WHERE namespace=?", (ns,)
            ).fetchone()
            if row:
                out[key] = int(row[0] or 0)
                out["newest"] = max(out["newest"], str(row[1] or ""))
    except Exception:  # noqa: BLE001
        pass
    return out
