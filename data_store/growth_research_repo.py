"""成长研究快照与输入缓存，复用现有SQLite，不绑定报告目录。"""
from __future__ import annotations

import json
from collections import defaultdict

from data_store import kv_repo
from data_store.connection import get_conn

NAMESPACE = "growth_research"


def read(key: str, default=None):
    cached = kv_repo.get(NAMESPACE, key)
    return cached[0] if cached else default


def write(key: str, value) -> None:
    kv_repo.set_(NAMESPACE, key, value)


def local_inputs() -> dict:
    conn = get_conn()
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM moneyflow_dc WHERE top_n=0 "
        "ORDER BY REPLACE(trade_date,'-','') DESC LIMIT 140")]
    if not dates:
        return {"as_of":None,"market":[],"histories":{},"concepts":{}}
    latest = dates[0]
    market = [dict(r) for r in conn.execute(
        "SELECT ts_code,name,close,pct_change FROM moneyflow_dc WHERE top_n=0 AND trade_date=?",(latest,))]
    placeholders = ",".join("?" for _ in dates)
    histories = defaultdict(list)
    for r in conn.execute(
        f"SELECT ts_code,trade_date,close,pct_change FROM moneyflow_dc WHERE top_n=0 "
        f"AND trade_date IN ({placeholders}) AND close>0 ORDER BY REPLACE(trade_date,'-','')",dates):
        date = str(r["trade_date"]).replace("-","")
        histories[str(r["ts_code"])[:6]].append({"date":f"{date[:4]}-{date[4:6]}-{date[6:8]}","close":r["close"],"pct_change":r["pct_change"]})
    concepts={}
    for r in conn.execute("SELECT identifier,cache_type,payload FROM sentiment_cache WHERE cache_type IN ('stock_boards','sector_name')"):
        try:
            value=json.loads(r["payload"])
        except (ValueError,TypeError):
            continue
        entry=concepts.setdefault(r["identifier"],{})
        if r["cache_type"]=="stock_boards" and isinstance(value,list):entry["concepts"]=",".join(str(v) for v in value)
        elif isinstance(value,str):entry["industry"]=value
    d=str(latest).replace("-","")
    return {"as_of":f"{d[:4]}-{d[4:6]}-{d[6:8]}","market":market,"histories":dict(histories),"concepts":concepts}
