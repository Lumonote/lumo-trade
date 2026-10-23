"""大盘体检的板块深度研究：全板块日资金回补、同日成分穿透、真实时点。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
from functools import lru_cache
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlencode

from analysis.sector_depth import normalize, build_report, analyze_members
from analysis.market_health import number
from analysis.market_health_fusion import iso
from data_store import kv_repo, tushare_client
from data_store.connection import close_conn
from webui.services.http_client import request_json
from webui.services.market_health_service import now

NAMESPACE = "sector_depth"
BASELINE = Path(__file__).resolve().parents[2]/"config/sector_depth_baseline.json"
_lock = threading.Lock()
_detail_lock = threading.Lock()


@lru_cache(maxsize=1)
def baseline():
    try:
        return json.loads(BASELINE.read_text())
    except (OSError,ValueError):
        return {"rows":[],"rotation":[],"calendar":[],"as_of":None}


def _read(key):
    hit = kv_repo.get(NAMESPACE,key)
    return hit[0] if hit else None


def _pro():
    pro = tushare_client.get_pro()
    if pro is None:
        raise ValueError("板块历史需要现有TuShare配置，请在后台配置检查Token")
    return pro


def _records(method, cap=5000, **params):
    result, seen = [], set()
    for offset in range(0,100000,cap):
        frame = getattr(_pro(),method)(**params,limit=cap,offset=offset)
        rows = [] if frame is None else frame.to_dict("records")
        if not rows:
            break
        keys = [json.dumps(r,sort_keys=True,default=str) for r in rows]
        if any(k in seen for k in keys):
            raise ValueError(f"{method}分页重复，拒绝将截断数据作为全量")
        seen.update(keys);result.extend(rows)
        if len(rows)<cap:
            break
    else:
        raise ValueError(f"{method}超出分页上限，原缓存保留")
    return result


def _daily(date):
    try:
        old = _read(f"flow:{date}")
        if old and date < now().date().isoformat():
            return old
        rows = normalize(_records("moneyflow_ind_dc",trade_date=date.replace("-","")))
        rows = [r for r in rows if r["date"] == date]
        if rows:
            kv_repo.set_(NAMESPACE,f"flow:{date}",rows)
        return rows or old or []
    finally:
        close_conn()


def fetch_live():
    """东财f124为源报价时刻；无日期/不完整翻页时不展示成实时榜。"""
    result = []
    try:
        for kind,code in (("行业",2),("概念",3)):
            collected, total = [], None
            for page in range(1,31):
                params={"pn":page,"pz":100,"po":1,"np":1,"fltt":2,"invt":2,"fid":"f62",
                        "fs":f"m:90+t:{code}","fields":"f12,f14,f2,f3,f62,f184,f124"}
                data=(request_json("https://push2.eastmoney.com/api/qt/clist/get?"+urlencode(params),timeout=4,retries=1,trust_env=False) or {}).get("data") or {}
                batch=data.get("diff") or []
                if isinstance(batch,dict):batch=list(batch.values())
                total=int(data.get("total") or 0)
                collected.extend(batch)
                if len(collected)>=total:break
                if not batch:raise ValueError("实时板块中间页缺失")
            unique={r.get("f12"):r for r in collected}
            if not total or len(unique)!=total:raise ValueError("实时板块覆盖不完整")
            for raw in unique.values():
                stamp=number(raw.get("f124"))
                if not stamp:continue
                tick=dt.datetime.fromtimestamp(stamp,tz=now().tzinfo)
                if tick.date()!=now().date() or (now()-tick).total_seconds() < -60:continue
                age=(now()-tick).total_seconds()
                result.append({"code":raw["f12"],"name":raw.get("f14"),"type":kind,"net":number(raw.get("f62")),
                               "rate":number(raw.get("f184")),"pct":number(raw.get("f3")),"quote_at":tick.isoformat(),
                               "timing":"今日盘后报价" if now().time()>=dt.time(15,30) else "盘中报价" if age<=300 else "今日滞后报价"})
        return {"rows":result,"source":"东方财富板块实时资金估算","note":"盘中资金会变化；连续性与历史排名仍采用独立收盘序列"}
    except Exception:
        return {"rows":[],"source":"东方财富实时资金接口","note":"本轮实时接口未返回完整且可校验时点的数据，以下采用实际已披露收盘日；刷新时间不替代数据日"}


def collect(progress=lambda message:None):
    current = now()
    start=(current.date()-dt.timedelta(days=110)).strftime("%Y%m%d")
    end=current.strftime("%Y%m%d")
    raw=_records("trade_cal",exchange="SSE",start_date=start,end_date=end,is_open="1")
    dates=sorted({iso(r.get("cal_date")) for r in raw if str(r.get("is_open")) in ("1","1.0")})
    dates=[d for d in dates if d and (d<current.date().isoformat() or current.time()>=dt.time(15,30))][-41:]
    if len(dates)<20:raise ValueError("交易日历不足20日，无法计算完整持续性窗口")
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures={pool.submit(_daily,d):d for d in dates}
        for i,future in enumerate(as_completed(futures),1):
            try:rows.extend(future.result())
            except Exception:
                date=futures[future]
                errors.append(date)
                rows.extend(_read(f"flow:{date}") or [])
            progress(f"板块历史已读取{i}/{len(dates)}个交易日")
    if not rows:raise ValueError("板块资金接口未返回历史数据，保留上次成功快照")
    latest=max(r["date"] for r in rows)
    previous=_read("report") or baseline()
    if previous.get("as_of") and latest < previous["as_of"]:
        raise ValueError(f"接口最新日期{latest}早于已有{previous['as_of']}，原成功研究保留")
    benchmark_rows=_records("index_daily",ts_code="000300.SH",start_date=start,end_date=end)
    benchmark={iso(r.get("trade_date")):number(r.get("pct_chg")) for r in benchmark_rows if number(r.get("pct_chg")) is not None}
    report=build_report(rows,dates,benchmark)
    missing=sorted(set(d for d in dates if d<=latest)-{r["date"] for r in rows})
    errors=sorted(set(errors+missing))
    report.update(updated_at=now().isoformat(),source="TuShare / 东方财富 moneyflow_ind_dc（元）",benchmark="沪深300",
                  errors=[f"以下交易日刷新失败：{'、'.join(errors)}；已缓存日保留真实日值，无缓存的缺日窗口不计算连续性"] if errors else [],
                  disclosure="板块历史资金为盘后披露；主力指按订单规模估算的大单/超大单净额，不代表已识别的机构账户",
                  live=fetch_live())
    if latest<dates[-1]:report["errors"].append(f"最新已披露日为{latest}；{dates[-1]}尚未取得板块资金")
    kv_repo.set_(NAMESPACE,"report",report)
    return report


def query(jobs=None):
    stored=_read("report") or baseline()
    result={k:v for k,v in stored.items() if k not in ("rows","details")}
    result["rows"]=[{k:v for k,v in r.items() if k!="history"} for r in stored.get("rows",[])]
    job_id=_read("latest_job")
    result["job"]=jobs.get(job_id) if jobs and job_id else None
    result["today"]=now().date().isoformat()
    return result


def detail(code, refresh=False):
    if not re.fullmatch(r"BK\d{4,6}",code):raise ValueError("板块代码必须为真实BK代码")
    report=_read("report") or baseline()
    row=next((r for r in report.get("rows",[]) if r["code"]==code),None)
    if not row:raise ValueError("该板块未在本轮完整资金榜中，请先更新板块研究")
    date=report["as_of"]
    key=f"detail:{code}:{date}"
    cached=_read(key) or (report.get("details") or {}).get(code)
    if cached and not refresh:return {**cached,"sector":row}
    with _detail_lock:
        try:
            members=_records("dc_member",ts_code=f"{code}.DC",trade_date=date.replace("-",""))
            members=[r for r in members if iso(r.get("trade_date"))==date]
            if not members:raise ValueError(f"成分接口尚无{date}的板块成分，未用其他日期冒充同日成分")
            flows=_read(f"stocks:{date}")
            if not flows:
                flows=_records("moneyflow_dc",cap=6000,trade_date=date.replace("-",""))
                flows=[r for r in flows if iso(r.get("trade_date"))==date]
                if not flows:raise ValueError(f"个股资金接口尚无{date}数据")
                kv_repo.set_(NAMESPACE,f"stocks:{date}",flows)
            analysis=analyze_members(members,flows,date,date)
            prices=_records("dc_daily",cap=2000,ts_code=f"{code}.DC",start_date=report["calendar"][0].replace("-",""),end_date=date.replace("-",""))
            prices=sorted([r for r in prices if iso(r.get("trade_date"))<=date],key=lambda r:str(r["trade_date"]))
            technical={"as_of":iso(prices[-1]["trade_date"]) if prices else None,"source":"TuShare dc_daily 板块实际成交额/成交量"}
            expected=report["calendar"][-6:]
            if len(prices)>=6 and [iso(r["trade_date"]) for r in prices[-6:]]==expected and all(number(r.get("vol")) is not None for r in prices[-6:]):
                previous=sum(number(r["vol"]) for r in prices[-6:-1])/5
                technical["volume5"]=number(prices[-1]["vol"])/previous if previous else None
            technical.update(amount=number(prices[-1].get("amount")) if prices else None,turnover=number(prices[-1].get("turnover_rate")) if prices else None)
            result={**analysis,"technical":technical,"updated_at":now().isoformat()}
            kv_repo.set_(NAMESPACE,key,result)
            return {**result,"sector":row}
        except Exception as exc:
            if cached:return {**cached,"sector":row,"error":"本轮穿透读取失败，保留上次成功数据及日期"}
            raise ValueError(str(exc) if isinstance(exc,ValueError) else f"成分穿透接口暂不可用（{type(exc).__name__}），板块资金与历史研究仍可查看") from None


def start_refresh(jobs):
    with _lock:
        job_id=_read("latest_job")
        active=jobs.get(job_id) if job_id else None
        if active and active.get("status") in ("queued","running"):return active
        def worker(job_id,params):
            jobs.update(job_id,status="running",started_at=now().isoformat(),progress=1)
            try:
                completed=[0]
                def progress(message):
                    completed[0]+=1
                    jobs.update(job_id,progress=min(85,completed[0]*2))
                    jobs.append_log(job_id,message)
                report=collect(progress)
                # 首屏最强行业与概念自动完成成分穿透，其余点选时按真实接口读取。
                leaders=[next((r for r in report["rows"] if r["type"]==kind and r["net"]>0),None) for kind in ("行业","概念")]
                for row in leaders:
                    if row:
                        try:detail(row["code"])
                        except ValueError as exc:jobs.append_log(job_id,str(exc))
                jobs.update(job_id,status="finished",finished_at=now().isoformat(),progress=100,
                            result={"as_of":report["as_of"],"boards":len(report["rows"]),"notes":report["errors"]})
            except Exception as exc:
                message=str(exc) if isinstance(exc,ValueError) else f"板块研究更新失败（{type(exc).__name__}），原成功快照保留"
                jobs.update(job_id,status="failed",finished_at=now().isoformat(),error=message)
            finally:close_conn()
        job=jobs.start("sector_depth",{},worker)
        kv_repo.set_(NAMESPACE,"latest_job",job["id"])
        return job
