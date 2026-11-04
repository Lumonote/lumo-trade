"""独立的大笔买卖异动通道：后台更新、去重、保留来源时间，不阻塞大屏。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import math
import re
import threading
import time
import urllib.parse

import httpx

from analysis.short_term_screen import clean
from data_store import kv_repo
from data_store.connection import close_conn
from webui.services.http_client import request_text
from webui.services.market_health_service import now

NAMESPACE = "short_term_screen"
KINDS = {"buy": ("8193", "大笔买入"), "sell": ("8194", "大笔卖出")}
_lock = threading.Lock()
_running = False
_attempt = -1000.0
PAGE_SIZE = 200
KEEP = 2000


def _cached(key="orders"):
    hit=kv_repo.get(NAMESPACE,key)
    return hit[0] if hit else {}


def normalize(rows, side, market_date, quote_time="23:59:59"):
    """异动源只给时分秒，日期用指数报价交易日校验；不猜测大单金额。"""
    result={}
    for raw in rows:
        if not raw.get("tm"):
            continue
        code=str(raw.get("c") or "")
        stamp=str(raw.get("tm") or "").zfill(6)
        if not re.fullmatch(r"\d{6}",code) or str(raw.get("t"))!=KINDS[side][0] or not re.fullmatch(r"\d{6}",stamp):
            continue
        try:
            clock=dt.time(int(stamp[:2]),int(stamp[2:4]),int(stamp[4:])).isoformat()
            dt.date.fromisoformat(market_date)
        except ValueError:
            continue
        if clock>quote_time:
            continue
        info=clean(raw.get("i"),300)
        ident="order:"+hashlib.sha256(f"{market_date}|{code}|{side}|{clock}|{info}".encode()).hexdigest()[:18]
        amounts={"amount":None,"price":None,"volume":None,"change_pct":None}
        try:
            values=[float(part) for part in info.split(",")]
            if len(values)==4 and all(math.isfinite(v) for v in values):
                volume,price,ratio,amount=values
                if volume>0 and volume.is_integer() and price>0 and amount>0 and abs(volume*price-amount)<=max(1,amount*.005):
                    amounts={"amount":amount,"price":price,"volume":int(volume),"change_pct":round(ratio*100,4)}
        except (ValueError,TypeError):
            pass
        result[ident]={"id":ident,"code":code,"name":clean(raw.get("n") or code,80),"side":side,
            "label":KINDS[side][1],"date":market_date,"time":clock,"occurred_at":market_date+"T"+clock,
            "raw_info":info,"source":"东方财富盘口异动","source_url":"https://quote.eastmoney.com/changes/",
            "date_basis":"指数报价交易日校验；异动源仅给时分秒", **amounts,
            "amount_basis":"源四字段，并经数量×价格一致性校验" if amounts["amount"] is not None else "源未给出可校验金额"}
    return sorted(result.values(),key=lambda r:(r["occurred_at"],r["id"]),reverse=True)


def _market_stamp():
    text=request_text("https://qt.gtimg.cn/q=sh000001",headers={"Referer":"https://gu.qq.com/","User-Agent":"Mozilla/5.0"},encoding="gbk",timeout=5,retries=1,trust_env=False)
    match=re.search(r'v_sh000001="([^"\n]+)',text)
    fields=match[1].split("~") if match else []
    if len(fields)<=30 or not re.fullmatch(r"\d{14}",fields[30]):
        raise ValueError("未取得交易日校验报价")
    stamp=dt.datetime.strptime(fields[30],"%Y%m%d%H%M%S")
    if stamp.date()>now().date():
        raise ValueError("报价交易日异常")
    return stamp.date().isoformat(),stamp.time().isoformat()


def _fetch_side(side, market_date, quote_time):
    params={"type":KINDS[side][0],"ut":"7eea3edcaed734bea9cbfc24409ed989","pageindex":0,"pagesize":PAGE_SIZE,"dpt":"wzchanges"}
    url="https://push2ex.eastmoney.com/getAllStockChanges?"+urllib.parse.urlencode(params)
    for trust in (False,True):
        try:
            with httpx.Client(timeout=5,follow_redirects=True,trust_env=trust) as client:
                response=client.get(url,headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"})
                response.raise_for_status();value=response.json()
            data=value.get("data")
            if isinstance(data,dict) and data.get("tc")==0 and data.get("allstock") is None:
                data={**data,"allstock":[]}
            if not isinstance(data,dict) or not isinstance(data.get("allstock"),list):
                raise ValueError("异动源未返回有效记录")
            return {"date":market_date,"quote_time":quote_time,"rows":normalize(data["allstock"],side,market_date,quote_time),
                    "source_total":data.get("tc"),"fetched_at":now().isoformat(),"error":""}
        except Exception:
            continue
    raise ValueError("盘口源暂不可用，保留上次记录与时间")


def merge(previous, current):
    """同一交易日按真实事件ID累积；跨日清空旧记录，失败由调用方保留旧日期。"""
    old=previous.get("rows",[]) if previous.get("date")==current["date"] else []
    rows={r["id"]:r for r in old}
    rows.update({r["id"]:r for r in current["rows"]})
    return {**current,"rows":sorted(rows.values(),key=lambda r:(r["occurred_at"],r["id"]),reverse=True)[:KEEP]}


def collect(fetch_side=None, market_stamp=None):
    saved=_cached()
    result={**saved}
    try:
        market_date,quote_time=(market_stamp or _market_stamp)()
        def one(side):
            try:
                return merge(saved.get(side,{}) or {},(fetch_side or _fetch_side)(side,market_date,quote_time))
            except Exception:
                return {**(saved.get(side) or {}),"error":"盘口源暂不可用，保留上次记录与时间","attempted_at":now().isoformat()}
        with ThreadPoolExecutor(max_workers=2) as pool:
            for side,value in zip(KINDS,pool.map(one,KINDS)):
                result[side]=value
        result["error"]=""
    except Exception:
        result["error"]="交易日校验暂不可用，保留上次盘口记录"
    result["attempted_at"]=now().isoformat()
    kv_repo.set_(NAMESPACE,"orders",result)
    for side in KINDS:
        value=result.get(side) or {}
        if value.get("date") and not value.get("error"):
            key="orders:"+value["date"]
            archived=_cached(key)
            kv_repo.set_(NAMESPACE,key,{**archived,side:value})
    return result


def snapshot(date="", auto=False):
    global _running,_attempt
    if date:
        date=dt.date.fromisoformat(date).isoformat()
        if date>now().date().isoformat():
            raise ValueError("不能查询未来盘口记录")
    value=_cached("orders:"+date if date else "orders")
    current=now()
    interval=15 if current.weekday()<5 and dt.time(9,15)<=current.time().replace(tzinfo=None)<=dt.time(15,10) else 300
    if auto and not date:
        with _lock:
            if not _running and time.monotonic()-_attempt>=interval:
                _running=True;_attempt=time.monotonic()
                def worker():
                    global _running
                    try:collect()
                    finally:
                        close_conn()
                        with _lock:_running=False
                threading.Thread(target=worker,name="short-term-orders",daemon=True).start()
    visible=lambda side:[r for r in (value.get(side) or {}).get("rows",[]) if date or r.get("date")==current.date().isoformat()]
    return {"big_buys":visible("buy"),"big_sells":visible("sell"),
        "order_meta":{side:({k:v for k,v in (value.get(side) or {}).items() if k!="rows"} if date or (value.get(side) or {}).get("date")==current.date().isoformat() else {"date":None,"error":"今日盘口记录正在自动连接"}) for side in KINDS},
        "error":value.get("error", ""),"refreshing":_running if not date else False,
        "poll_seconds":15,"source_page_size":PAGE_SIZE,"retained_per_side":KEEP,
        "note":"大笔买卖为盘口异动提示；只累计实际采集记录，不等同于完整逐笔委托或成交清单。"}
