"""公开财报、盈利预测和前复权日线。分页完整后才交付一份输入快照。"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import requests

DATA_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def completed_quarters(today: dt.date) -> list[str]:
    dates=[dt.date(y,m,d) for y in range(today.year-2,today.year+1) for m,d in ((3,31),(6,30),(9,30),(12,31))]
    return [d.isoformat() for d in sorted((d for d in dates if d<today),reverse=True)[:5]]


def _download(params: dict, allow_empty=False, session=None) -> list[dict]:
    get = session.get if session else requests.get
    rows=[];expected=None
    for page in range(1,201):
        response=get(DATA_URL,params={**params,"pageSize":500,"pageNumber":page},timeout=(8,25))
        response.raise_for_status()
        payload=response.json();result=payload.get("result")
        if not result:
            if allow_empty and payload.get("code")==9201:return []
            raise ValueError("公开数据源未返回完整数据")
        if expected is None:expected=int(result["count"])
        rows.extend(result.get("data") or [])
        if page>=int(result["pages"]):break
    if expected is None or len(rows)!=expected:
        raise ValueError("公开数据分页不完整，保留上一份缓存")
    return rows


def fetch_financials(today: dt.date, progress: Callable[[str],None]=lambda message:None) -> dict:
    dates=completed_quarters(today)
    # 最近三季对应的去年同期，用于检验正利润基数；不从同比百分数反推亏损基数。
    dates=sorted(set(dates+[str(int(d[:4])-1)+d[4:] for d in dates[:3]]),reverse=True)
    reports={};errors=[]
    for date in dates:
        progress("读取财报 "+date)
        try:
            rows=_download({"reportName":"RPT_LICO_FN_CPD","columns":"ALL",
                            "filter":f'(REPORTDATE=\'{date}\')(SECURITY_TYPE="A股")',
                            "sortColumns":"SECURITY_CODE","sortTypes":"1","source":"WEB","client":"WEB"},allow_empty=True)
            by_code={}
            for row in sorted(rows,key=lambda r:(r.get("UPDATE_DATE") or "",r.get("NOTICE_DATE") or "")):
                notice=(row.get("NOTICE_DATE") or "")[:10]
                if notice and notice>today.isoformat():continue
                by_code[str(row["SECURITY_CODE"])]=row
            if by_code:reports[date]=by_code
        except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
            errors.append(f"{date}财报读取失败（{type(exc).__name__}），保留已有数据")
    progress("读取全市场盈利预测")
    forecasts={}
    try:
        rows=_download({"reportName":"RPT_WEB_RESPREDICT","columns":"WEB_RESPREDICT","sortColumns":"SECURITY_CODE","sortTypes":"1"})
        forecasts={str(r["SECURITY_CODE"]):r for r in rows}
    except (requests.RequestException,ValueError,KeyError,TypeError) as exc:
        errors.append(f"盈利预测读取失败（{type(exc).__name__}），保留已有数据")
    return {"reports":reports,"forecasts":forecasts,"errors":errors}


def fetch_qfq(code: str, as_of: str) -> list[dict]:
    end=dt.date.fromisoformat(as_of);start=end-dt.timedelta(days=400)
    response=requests.get(KLINE_URL,params={"secid":f"{1 if code.startswith('6') else 0}.{code}",
        "fields1":"f1,f2,f3,f4,f5,f6","fields2":"f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt":101,"fqt":1,"beg":start.strftime("%Y%m%d"),"end":end.strftime("%Y%m%d")},timeout=(6,15))
    response.raise_for_status()
    data=response.json().get("data") or {};bars=[]
    for raw in data.get("klines") or []:
        k=raw.split(",")
        bars.append({"date":k[0],"open":float(k[1]),"close":float(k[2]),"high":float(k[3]),"low":float(k[4]),"volume":float(k[5]),"pct_change":float(k[8])})
    if not bars:raise ValueError("未取得前复权日线")
    return bars
