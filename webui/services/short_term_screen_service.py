"""短线大屏：只读秒开、独立后台更新、多来源降级与各自数据时点。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
import re
import threading
import time

from analysis.market_limits import build_report as build_limits
from analysis.short_term_screen import build_report, classify_news
from data_store import kv_repo, quant_radar_repo, tushare_client
from data_store.connection import close_conn, get_conn
from webui.services import market_limits_service, sector_depth_service, quant_radar_service
from webui.services.market_health_service import now
from webui.services.short_term_calendar import CONFERENCES, calendar_rows
from webui.services import short_term_orders

NAMESPACE = "short_term_screen"
_lock = threading.Lock()
_publish_lock = threading.Lock()
_feed_lock = threading.Lock()
_feeds = {}
# 收盘核验、资金研究、人气和实时异动不共享刷新节拍。
FEED_INTERVALS = {"radar":60,"news":45,"popularity":180,"sectors":600,"limits":1800}
FEED_FIELDS = {
    "radar": ("stocks","sectors","summary","graph"),
    "news": ("news","core_news","short_news","calendar","stocks","sectors","summary"),
    "popularity": ("hot_stocks","popularity_rises","hot_fetched_at","stocks"),
    "sectors": ("sectors","summary","graph"),
    "limits": ("limits","limit_up","limit_down","limit_as_of","summary","stocks","sectors"),
}


def _read(key):
    hit = kv_repo.get(NAMESPACE, key)
    return hit[0] if hit else None


def _local_radar():
    latest = get_conn().execute("SELECT MAX(trade_date) FROM quant_radar_stock_daily").fetchone()[0]
    if not latest:
        return {"stocks": [], "sectors": [], "fallback_date": None}
    hit = kv_repo.get("quant_radar", "day:" + latest.replace("-", ""))
    snapshot = hit[0] if hit else {}
    stocks = quant_radar_repo.get_day(latest, limit=0)
    return {**snapshot, "stocks": stocks, "sectors": quant_radar_service._aggregate_sectors(stocks),
            "fallback_date": latest, "live": False}


def _local_sources():
    stored = _read("inputs") or {}
    result = {**stored, "source_status": list(stored.get("source_status") or []), "notes": list(stored.get("notes") or [])}
    for key, fn in (("radar", _local_radar), ("sectors", sector_depth_service.query), ("limits", market_limits_service.query)):
        if key in result:
            continue
        try:
            result[key] = fn()
        except Exception:
            result[key] = {}
    result["calendar"] = list(stored.get("calendar") or CONFERENCES)
    result["orders"] = short_term_orders.snapshot()
    return result


def query(jobs=None, date=""):
    if date:
        try:
            date = dt.date.fromisoformat(str(date)).isoformat()
        except (TypeError, ValueError):
            raise ValueError("日期必须为YYYY-MM-DD") from None
        if date > now().date().isoformat():
            raise ValueError("不能查询未来的行情快照；未来日程请在催化日历查看")
        stored = _read("report:" + date)
        if not stored:
            return {**build_report({}, date, now().isoformat()), "available": False, "job": None, "historical": True,
                    "notes": ["该日尚无短线大屏快照，未使用其他日期冒充所选日行情"]}
        # 兼容分流规则加入前的已归档快照，仍只使用归档消息和原发表日。
        news = [{**r, **classify_news({**r, "is_today": r.get("date") == date})} for r in stored.get("news", [])]
        return {**stored, "news": news, "core_news": [r for r in news if r["is_core"]],
                "short_news": [r for r in news if not r["is_core"]],
                "limit_up": [r for r in stored.get("limits", []) if r.get("kind") == "U"],
                "limit_down": [r for r in stored.get("limits", []) if r.get("kind") == "D"],
                "hot_stocks": stored.get("hot_stocks", []), "popularity_rises": stored.get("popularity_rises", []),
                "big_buys":stored.get("big_buys",[]), "big_sells":stored.get("big_sells",[]),
                "order_events":stored.get("order_events",[]),"order_meta":stored.get("order_meta",{}),
                "historical": True, "job": None, "available": True}
    inputs = _local_sources()
    result = build_report(inputs, now().date().isoformat(), now().isoformat())
    job_id = _read("latest_job")
    result.update(job=jobs.get(job_id) if jobs and job_id else None,
                  updated_at=_read("updated_at"), available=bool(result["stocks"] or result["sectors"] or result["calendar"]), historical=False)
    return result


def section(kind, jobs=None, date="", keyword=""):
    if kind not in ("sectors", "stocks", "news", "core_news", "short_news", "calendar", "limits", "limit_up", "limit_down", "hot_stocks", "popularity_rises", "big_buys", "big_sells"):
        raise ValueError("不支持的短线数据分区")
    report = query(jobs, date)
    keyword = str(keyword or "").strip().casefold()[:100]
    rows = report[kind]
    if keyword:
        rows = [r for r in rows if keyword in " ".join(str(r.get(k) or "") for k in ("name", "code", "title", "summary", "source", "industry")).casefold()]
    return {"kind": kind, "total": len(rows), "rows": rows, "today": report["today"],
            "generated_at": report["generated_at"], "method": report["method"], "notes": report["notes"]}


def detail(kind, ident, jobs=None, date=""):
    if kind not in ("sector", "stock", "news", "event", "order"):
        raise ValueError("不支持的明细类型")
    report = query(jobs, date)
    field = {"sector": "sectors", "stock": "stocks", "news": "news", "event": "calendar", "order":"order_events"}[kind]
    row = next((r for r in report[field] if r.get("code" if kind == "stock" else "id") == ident), None)
    if row is None:
        raise ValueError("所选快照中没有该条记录")
    sector_ids = {row["id"]} if kind == "sector" else set(row.get("sector_ids") or [])
    codes = {row["code"]} if kind in ("stock","order") else set(row.get("stock_codes") or [])
    return {"kind": kind, "row": row, "today": report["today"], "method": report["method"],
            "sectors": [r for r in report["sectors"] if r["id"] in sector_ids],
            "stocks": [r for r in report["stocks"] if r["code"] in codes],
            "news": [r for r in report["news"] if r["id"] in row.get("news_ids", [])],
            "calendar": [r for r in report["calendar"] if r["id"] in row.get("event_ids", [])],
            "limits": [r for r in report["limits"] if r["code"] in codes]}


def _fresh_limits():
    """短线只回补最近六个收盘日；仍在连续的左端保留下界，不伪造无限历史。"""
    try:
        pro = tushare_client.get_pro()
        if pro is None:
            raise ValueError("涨跌停更新需要现有TuShare配置")
        current = now()
        dates = sorted(d[:4] + "-" + d[4:6] + "-" + d[6:] for d in tushare_client.recent_trade_dates(8))
        dates = [d for d in dates if d < current.date().isoformat() or current.time() >= dt.time(16, 10)][-6:]
        if not dates:
            raise ValueError("未取得最近已收盘交易日历")
        existing = market_limits_service.query()
        if existing.get("as_of") == dates[-1] and existing.get("events"):
            return existing
        frame = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
        basics = {r["ts_code"]: r for r in ([] if frame is None else frame.to_dict("records"))}
        days = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(market_limits_service._daily, date, basics) for date in dates]
            for f in as_completed(futures):
                try:
                    days.append(f.result())
                except Exception:
                    pass
        if not days:
            raise ValueError("最近收盘限价数据尚未取得")
        end = max(r["date"] for r in days)
        existing = market_limits_service.query()
        if existing.get("as_of") == end and existing.get("events"):
            return existing
        return build_limits(days, dates, end, end)
    finally:
        close_conn()


def _fresh_radar():
    try:
        value = quant_radar_service.overview(force=True)
        day = value.get("snapshot_date") or value.get("fallback_date")
        if day:
            hit = kv_repo.get("quant_radar", "day:" + day.replace("-", ""))
            if hit:
                value = {**value, "changes_agg": hit[0].get("changes_agg") or {}}
        if not value.get("stocks"):
            raise ValueError("异动源暂无有效股票，保留已有快照")
        return value
    finally:
        close_conn()


def _fresh_sectors():
    try:
        value = sector_depth_service.query()
        # 补齐最近收盘的全板块资金，避免仅有旧研究基线时一直展示旧日期。
        dates = tushare_client.recent_trade_dates(3)
        current = now()
        eligible = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates
                    if d < current.strftime("%Y%m%d") or current.time() >= dt.time(17, 30)]
        if eligible:
            date = max(eligible)
            if date > (value.get("as_of") or ""):
                rows = sector_depth_service._daily(date)
                if rows:
                    value = {"as_of": date, "rows": [{**r, "phase": "单日资金观测，完整持续性见板块深度"} for r in rows]}
        live = sector_depth_service.fetch_live()
        if live.get("rows"):
            # 盘中资金独立记录，不覆盖收盘资金的实际日期。
            by_code = {r["code"]: r for r in live["rows"]}
            value["rows"] = [{**r, "live": by_code.get(r["code"])} for r in value.get("rows", [])]
        return value
    finally:
        close_conn()


def _fresh_calendar():
    try:
        return list(CONFERENCES) + calendar_rows()
    finally:
        close_conn()


def _fresh_popularity():
    """人气榜与人气飙升榜：保留原始当前排名、较昨日变化及比较口径。"""
    from scripts.hot_stocks_fetcher import HotStocksFetcher
    from data_store.connection import db_path
    from webui.services.http_client import request_json_post
    payload={"appId":"appId01","globalId":"786e4c21-70dc-435a-93bb-38","marketType":"","pageNo":1,"pageSize":100}
    headers={"User-Agent":"Mozilla/5.0","Content-Type":"application/json","Origin":"https://guba.eastmoney.com","Referer":"https://guba.eastmoney.com/rank/"}
    def fetch(endpoint):
        result=request_json_post("https://emappdata.eastmoney.com/stockrank/"+endpoint,payload,headers=headers,timeout=10,retries=1)
        if result.get("code") != 0 or not isinstance(result.get("data"),list):
            raise ValueError("股吧人气源未返回有效榜单，保留已有排名与抓取时间")
        return result["data"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        current, rising=list(pool.map(fetch,("getAllCurrentList","getAllHisRcList")))
    if not current:
        raise ValueError("股吧人气源暂无有效排名，保留已有排名及抓取时间")
    fetcher = HotStocksFetcher(cache_dir=str(db_path().parent / "cache"), disable_cache=True)
    by_code={}
    for items, field, on_list in ((current,"hisRc",True),(rising,"hrc",False)):
        for item in items:
            match=re.fullmatch(r"(?:SH|SZ|BJ)(\d{6})",str(item.get("sc") or ""))
            if not match:
                continue
            code=match[1]
            if code in by_code:
                continue
            by_code[code]={"code":code,"name":fetcher._get_stock_name(code),"rank":item.get("rk"),
                          "rank_change":item.get(field),"on_hot_list":on_list,"rank_only":True}
    return {"rows":list(by_code.values()), "fetched_at":now().isoformat(),
            "comparison":"较昨日", "time_basis":"抓取时刻；源未提供排名生成时刻",
            "source_url":"https://guba.eastmoney.com/rank/"}


def feed(kind, intelligence, after="", date="", readers=None):
    """立即读独立分区缓存；来源后台采集，各自发布，不等待其他分区。"""
    if kind not in FEED_INTERVALS:
        raise ValueError("不支持的实时分区")
    if date:
        report=query(date=date)
        return {"kind":kind,"historical":True,"running":False,"version":date,
                "payload":{key:report.get(key) for key in FEED_FIELDS[kind]}}
    interval=FEED_INTERVALS[kind]
    current=now()
    if current.weekday()>4 or not dt.time(9,15)<=current.time().replace(tzinfo=None)<=dt.time(15,10):
        interval=max(interval,300)
    with _feed_lock:
        state=_feeds.setdefault(kind,{"version":"","payload":None,"revision":0,"checked_at":"","attempt":-10000,"running":False,"error":""})
        result={"kind":kind,"version":state["version"],"changed":bool(state["version"] and state["version"]!=after),
                "payload":state["payload"] if state["version"]!=after else None,
                "running":state["running"],"error":state["error"],"revision":state["revision"],"checked_at":state["checked_at"],"poll_seconds":FEED_INTERVALS[kind]}
        if not state["running"] and time.monotonic()-state["attempt"]>=interval:
            state.update(running=True,attempt=time.monotonic())
            def worker():
                try:
                    available=readers or {
                        "jinshi":lambda:intelligence.fetch_jinshi_flash(limit=100),
                        "ths_news":lambda:intelligence.fetch_ths_flash(limit=100),
                        "sina_news":lambda:intelligence.fetch_sina_flash(limit=100),
                        "eastmoney_news":lambda:intelligence.fetch_eastmoney_news(limit=100),
                        "radar":_fresh_radar,"popularity":_fresh_popularity,"sectors":_fresh_sectors,"limits":_fresh_limits}
                    selected={key:fn for key,fn in available.items() if key in (("jinshi","ths_news","sina_news","eastmoney_news") if kind=="news" else (kind,))}
                    values={};statuses={};stamp=now().isoformat()
                    with ThreadPoolExecutor(max_workers=max(1,len(selected))) as pool:
                        futures={pool.submit(fn):key for key,fn in selected.items()}
                        for future in as_completed(futures):
                            key=futures[future]
                            try:
                                value=future.result()
                                if not value:raise ValueError("接口暂未返回数据")
                                values[key]=value;statuses[key]={"key":key,"state":"已读取","fetched_at":stamp,"error":""}
                            except Exception:
                                statuses[key]={"key":key,"state":"保留缓存","attempted_at":stamp,"error":"来源暂不可用，保留上次数据"}
                    if not values:raise ValueError("来源暂不可用，保留上次数据")
                    # 仅发布阶段串行，慢网络请求从不持有发布锁。
                    with _publish_lock:
                        inputs=_local_sources();old_status={r["key"]:r for r in inputs.get("source_status",[])}
                        for key,status in statuses.items():old_status[key]={**old_status.get(key,{}),**status}
                        inputs.update(values,source_status=list(old_status.values()))
                        report=build_report(inputs,now().date().isoformat(),stamp)
                        kv_repo.set_(NAMESPACE,"inputs",inputs)
                        kv_repo.set_(NAMESPACE,"updated_at",stamp)
                        kv_repo.set_(NAMESPACE,"report:"+now().date().isoformat(),report)
                        revision=time.monotonic_ns()
                    payload={key:report.get(key) for key in FEED_FIELDS[kind]}
                    version=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]
                    payload.update(sources=report["sources"],updated_at=stamp)
                    with _feed_lock:state.update(payload=payload,version=version,revision=revision,checked_at=now().isoformat(),error="")
                except Exception:
                    with _feed_lock:state["error"]="来源暂不可用，保留上次数据"
                finally:
                    close_conn()
                    with _feed_lock:state["running"]=False
            threading.Thread(target=worker,name="short-term-feed-"+kind,daemon=True).start()
            result["running"]=True
        return result


def collect(intelligence, jobs=None, job_id=None, readers=None):
    old = _local_sources()
    result = {**old, "source_status": [], "notes": []}
    readers = readers or {
        "jinshi": lambda: intelligence.fetch_jinshi_flash(limit=100),
        "ths_news": lambda: intelligence.fetch_ths_flash(limit=100),
        "sina_news": lambda: intelligence.fetch_sina_flash(limit=100),
        "eastmoney_news": lambda: intelligence.fetch_eastmoney_news(limit=100),
        "radar": _fresh_radar, "sectors": _fresh_sectors, "limits": _fresh_limits,
        "popularity": _fresh_popularity,
    }
    old_status = {r["key"]: r for r in old.get("source_status", [])}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(reader): key for key, reader in readers.items()}
        for i, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            stamp = now().isoformat()
            try:
                value = future.result()
                if not value:
                    raise ValueError("接口暂未返回数据")
                result[key] = value
                status = {"key": key, "state": "已读取", "fetched_at": stamp, "error": ""}
            except Exception as exc:
                status = {"key": key, "state": "保留缓存" if old.get(key) else "未取得",
                          "fetched_at": old_status.get(key, {}).get("fetched_at"), "attempted_at": stamp,
                          "error": str(exc) if isinstance(exc, ValueError) else f"接口暂不可用（{type(exc).__name__}）"}
                if key == "calendar":
                    result[key] = old.get(key) or list(CONFERENCES)
                result["notes"].append(f"{key}：{status['state']}，{status['error']}")
            result["source_status"].append(status)
            if jobs and job_id:
                jobs.update(job_id, progress=int(i / len(readers) * 95))
                jobs.append_log(job_id, f"{key} · {status['state']} · {i}/{len(readers)}个来源")
    # 零发布时间的短讯保留在消息流，不拿抓取时间替换新闻发表时间。
    stamp = now().isoformat()
    report = build_report(result, now().date().isoformat(), stamp)
    kv_repo.set_(NAMESPACE, "inputs", result)
    kv_repo.set_(NAMESPACE, "updated_at", stamp)
    kv_repo.set_(NAMESPACE, "report:" + now().date().isoformat(), report)
    return report


def start_refresh(jobs, intelligence):
    with _lock:
        job_id = _read("latest_job")
        active = jobs.get(job_id) if job_id else None
        if active and active.get("status") in ("queued", "running"):
            return active

        def worker(job_id, params):
            jobs.update(job_id, status="running", started_at=now().isoformat(), progress=1)
            try:
                report = collect(intelligence, jobs, job_id)
                jobs.update(job_id, status="finished", progress=100, finished_at=now().isoformat(),
                            result={"summary": report["summary"], "notes": report["notes"]})
            except Exception as exc:
                jobs.update(job_id, status="failed", finished_at=now().isoformat(),
                            error=f"短线大屏更新失败（{type(exc).__name__}），已有快照继续保留")
            finally:
                close_conn()
        job = jobs.start("short_term_screen", {}, worker)
        kv_repo.set_(NAMESPACE, "latest_job", job["id"])
        return job
