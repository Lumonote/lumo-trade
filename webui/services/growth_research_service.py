"""桌面成长研究：持久快照、分页查询、板块统计及后台刷新。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import datetime as dt
import io
import math
import threading
from zoneinfo import ZoneInfo

from analysis.growth_research import GROUPS, CRITERIA, VERSION, build_snapshot, code6, number
from data_store import growth_research_repo as repo

_start_lock=threading.Lock()
SORTS={"default","price","revenue_growth","profit_growth","cagr","growth_next","pe","forecast_orgs","position120","support_distance"}


def now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai"))


def query_snapshot(snapshot: dict | None, params: dict) -> dict:
    snapshot=snapshot or {"stocks":[],"sectors":[],"summary":{"total":0,"groups":{}},"groups":GROUPS,"criteria":CRITERIA,"notes":["尚无研究快照，可先读取本地数据或更新财报与预期。"]}
    pool=str(params.get("pool") or "near")
    if pool not in {"near","all","actual","future","turnaround",*GROUPS}:raise ValueError("未知候选分组")
    sort=str(params.get("sort") or "default")
    if sort not in SORTS:raise ValueError("未知排序字段")
    direction=str(params.get("direction") or "desc")
    if direction not in ("asc","desc"):raise ValueError("未知排序方向")
    basis=str(params.get("basis") or "")
    if basis not in ("","qfq","close"):raise ValueError("未知技术依据")
    position=str(params.get("position") or "")
    if position not in ("","bottom","support","both"):raise ValueError("未知价格位置")
    search=str(params.get("search") or "").strip().casefold()[:150]
    industry=str(params.get("industry") or "");theme=str(params.get("theme") or "")
    exclude_st=str(params.get("exclude_st","1")).lower() not in ("0","false")
    industry_only=str(params.get("industry_only","0")).lower() in ("1","true")
    numeric={}
    for key in ("revenue_min","profit_min","cagr_min","pe_max","orgs_min"):
        value=params.get(key)
        if value not in (None,""):
            parsed=number(value)
            if parsed is None:raise ValueError("数值筛选条件无效")
            numeric[key]=parsed/100 if key in ("revenue_min","profit_min","cagr_min") else parsed
    rows=[]
    for r in snapshot["stocks"]:
        if exclude_st and r.get("risk_warning"):continue
        if pool=="near" and r["group"] not in ("A","B","C"):continue
        if pool in GROUPS and r["group"]!=pool:continue
        if pool=="actual" and not r["high_actual"]:continue
        if pool=="future" and not r["high_future"]:continue
        if pool=="turnaround" and not (r["actual_turnaround"] or r["future_turnaround"]):continue
        if industry and r["industry"]!=industry:continue
        if theme and theme not in r["themes"]:continue
        if basis and r["technical_basis"]!=basis:continue
        if position in ("bottom","both") and not r["bottom"]:continue
        if position in ("support","both") and r["support"] is None:continue
        if industry_only and r["theme_strength"]!="industry":continue
        if search and search not in " ".join([r["code"],r["name"],r["industry"],*r["themes"],*r["risks"]]).casefold():continue
        reject=False
        for parameter,key in (("revenue_min","revenue_growth"),("profit_min","profit_growth"),("cagr_min","cagr"),("pe_max","pe"),("orgs_min","forecast_orgs")):
            if parameter in numeric and (r.get(key) is None or (r[key]>numeric[parameter] if parameter=="pe_max" else r[key]<numeric[parameter])):reject=True;break
        if not reject:rows.append(r)
    if sort!="default":
        sign=-1 if direction=="desc" else 1
        rows.sort(key=lambda r:(r.get(sort) is None,sign*(r.get(sort) or 0),r["code"]))
    try:page=max(1,int(params.get("page") or 1));size=max(10,min(200,int(params.get("page_size") or 50)))
    except (ValueError,TypeError,OverflowError):raise ValueError("分页参数无效") from None
    pages=max(1,math.ceil(len(rows)/size));page=min(page,pages)
    metadata={key:value for key,value in snapshot.items() if key!="stocks"}
    age=(now().date()-dt.date.fromisoformat(snapshot["as_of"])).days if snapshot.get("as_of") else None
    metadata.update(ready=bool(snapshot.get("as_of")),stale=age is not None and age>4,age_days=age,
                    total_matched=len(rows),page=page,page_size=size,pages=pages,
                    industries=sorted({r["industry"] for r in snapshot["stocks"]}),themes=sorted({t for r in snapshot["stocks"] for t in r["themes"]}),
                    stocks=rows[(page-1)*size:page*size])
    return metadata


def query(params: dict) -> dict:
    return query_snapshot(repo.read("snapshot"),params)


def export_csv(params: dict) -> str:
    snapshot=repo.read("snapshot")
    view=str(params.get("view") or "stocks")
    if view not in ("stocks","sectors"):raise ValueError("未知导出视图")
    if view=="sectors":
        query_snapshot(snapshot,params)  # 与明细导出保持参数校验一致。
        industry=str(params.get("industry") or "")
        search=str(params.get("search") or "").strip().casefold()[:150]
        rows=[r for r in (snapshot or {}).get("sectors",[]) if (not industry or r["industry"]==industry)
              and (not search or search in " ".join([r["industry"],*[x["name"]+" "+x["code"] for x in r["examples"]]]).casefold())]
        out=io.StringIO();writer=csv.writer(out)
        writer.writerow(["行业","行情样本","可比预测样本","实际高增长","未来高增长","下一年EPS增速中位数(%)","两年CAGR中位数(%)","预期PE中位数","低位成长候选","列示高预期公司"])
        for r in rows:
            values=[r["industry"],r["stocks"],r["forecast_covered"],r["actual_high"],r["future_high"],
                    r["growth_next"]*100 if r["growth_next"] is not None else "",r["cagr"]*100 if r["cagr"] is not None else "",
                    r["pe"],r["near_growth"],"、".join(x["name"] for x in r["examples"])]
            writer.writerow(["'"+v if isinstance(v,str) and v.startswith(("=","+","-","@","\t","\r")) else v for v in values])
        return "\ufeff"+out.getvalue()
    # 同一过滤与排序覆盖所有页，不能只导出当前50行。
    first=query_snapshot(snapshot,{**params,"page":1,"page_size":200})
    rows=list(first["stocks"])
    for page in range(2,first["pages"]+1):rows.extend(query_snapshot(snapshot,{**params,"page":page,"page_size":200})["stocks"])
    out=io.StringIO();writer=csv.writer(out)
    writer.writerow(["代码","名称","行业","分组","行情日期","财报期","收盘(元)","营收同比(%)","归母同比(%)","未来EPS CAGR(%)","预期PE","评级机构数","参考支撑(元)","技术依据","风险"])
    def safe(value):
        if isinstance(value,str) and value.startswith(("=","+","-","@","\t","\r")):return "'"+value
        return value
    for r in rows:
        values=[r["code"],r["name"],r["industry"],r["group_label"],r["price_date"],r["report_date"],r["price"],
                r["revenue_growth"]*100 if r["revenue_growth"] is not None else "",r["profit_growth"]*100 if r["profit_growth"] is not None else "",
                r["cagr"]*100 if r["cagr"] is not None else "",r["pe"],r["forecast_orgs"],r.get("support"),"前复权已校验" if r["technical_basis"]=="qfq" else "收盘初筛", "；".join(r["risks"])]
        writer.writerow([safe(v) for v in values])
    return "\ufeff"+out.getvalue()


def rebuild() -> dict:
    inputs=repo.local_inputs()
    if not inputs["as_of"]:raise ValueError("本地没有全市场行情快照，请先在资金榜刷新全市场数据")
    financials=repo.read("financials",{})
    snapshot=build_snapshot(inputs["market"],inputs["histories"],financials,repo.read("qfq",{}),inputs["concepts"],inputs["as_of"],now().isoformat())
    repo.write("snapshot",snapshot)
    return snapshot


def latest_job(jobs):
    job_id=repo.read("latest_job")
    return jobs.get(job_id) if job_id else None


def merge_financials(previous: dict, incoming: dict, timestamp: str) -> dict:
    """部分数据源失败时保留原值及其采集时间。"""
    if not incoming["reports"] and not incoming["forecasts"]:
        raise ValueError("公开财报和预测均读取失败，已有快照保留")
    report_times = dict(previous.get("report_updated_at", {}))
    for date in incoming["reports"]:
        report_times[date] = timestamp
    return {
        "reports": {**previous.get("reports", {}), **incoming["reports"]},
        "forecasts": incoming["forecasts"] or previous.get("forecasts", {}),
        "report_updated_at": report_times,
        "forecasts_updated_at": timestamp if incoming["forecasts"] else previous.get("forecasts_updated_at", previous.get("updated_at")),
        "updated_at": timestamp,
        "errors": incoming["errors"],
    }


def start_refresh(mode: str, jobs) -> dict:
    if mode not in ("rebuild","financials","technical"):raise ValueError("未知更新模式")
    with _start_lock:
        active=latest_job(jobs)
        if active and active.get("status") in ("queued","running"):return active

        def worker(job_id, params):
            from analysis import growth_research_provider as provider
            from data_store.connection import close_conn
            jobs.update(job_id,status="running",started_at=now().isoformat(),progress=0)
            notes=[]
            try:
                if mode=="financials":
                    def progress(message):jobs.append_log(job_id,message)
                    incoming=provider.fetch_financials(now().date(),progress)
                    previous=repo.read("financials",{})
                    repo.write("financials",merge_financials(previous,incoming,now().isoformat()))
                    notes.extend(incoming["errors"])
                elif mode=="technical":
                    snapshot=repo.read("snapshot") or rebuild();cached=repo.read("qfq",{})
                    targets=[r for r in snapshot["stocks"] if r["group"]!="other" and r["technical_basis"]!="qfq"]
                    failures=0;success=0
                    pool=ThreadPoolExecutor(max_workers=3)
                    futures={pool.submit(provider.fetch_qfq,r["code"],snapshot["as_of"]):r["code"] for r in targets}
                    try:
                        for i,future in enumerate(as_completed(futures),1):
                            try:cached[futures[future]]=future.result();success+=1
                            except Exception:failures+=1
                            jobs.update(job_id,progress=round(100*i/max(1,len(targets)),1))
                            if i%20==0:jobs.append_log(job_id,f"K线校验 {i}/{len(targets)}，成功{success}，失败{failures}")
                            if failures>=20 and success==0:
                                for pending in futures:pending.cancel()
                                notes.append("K线数据源连续不可用，停止批量请求；未校验股票继续标为收盘初筛")
                                break
                    finally:pool.shutdown(wait=True,cancel_futures=True)
                    repo.write("qfq",cached)
                    if failures:notes.append(f"本轮K线请求失败{failures}次，已有校验数据保留")
                jobs.append_log(job_id,"重算全市场成长分组与板块统计")
                result=rebuild()
                if notes:
                    result["notes"].extend(notes);repo.write("snapshot",result)
                jobs.update(job_id,status="finished",progress=100,finished_at=now().isoformat(),result={"summary":result["summary"],"as_of":result["as_of"],"partial":bool(notes),"notes":notes})
            except Exception as exc:
                message=str(exc) if isinstance(exc,ValueError) else f"研究数据更新失败（{type(exc).__name__}），已有快照保留"
                jobs.update(job_id,status="failed",finished_at=now().isoformat(),error=message)
                jobs.append_log(job_id,message)
            finally:close_conn()

        job=jobs.start("growth_research",{"mode":mode},worker)
        repo.write("latest_job",job["id"])
        return job
