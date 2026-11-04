"""今日盘中涨跌停池；源交易日和全量计数校验，不把盘中封板当收盘核验。"""
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from analysis.market_health import number
from webui.services.http_client import request_json
from webui.services.market_health_service import now
from webui.services.short_term_orders import _market_stamp


def clock(value):
    if not value:return None
    text=str(value or "").zfill(6)
    if not re.fullmatch(r"\d{6}",text):return None
    try:return dt.time(int(text[:2]),int(text[2:4]),int(text[4:])).isoformat()
    except ValueError:return None


def pool(kind, date):
    params={"ut":"7eea3edcaed734bea9cbfc24409ed989","dpt":"wz.ztzt","Pageindex":0,"pagesize":10000,
            "sort":"fbt:asc" if kind=="U" else "fund:asc","date":date.replace("-","")}
    value=request_json("https://push2ex.eastmoney.com/getTopic"+{"U":"ZT","D":"DT","Z":"ZB"}[kind]+"Pool?"+urlencode(params),
                       headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"},timeout=6,retries=2,trust_env=False)
    data=value.get("data") or {}
    if str(data.get("qdate"))!=date.replace("-","") or not isinstance(data.get("pool"),list):
        raise ValueError("涨跌停池未返回今日交易日")
    rows=data["pool"]
    if data.get("tc")!=len(rows) or len({r.get("c") for r in rows})!=len(rows):
        raise ValueError("今日涨跌停池覆盖不完整")
    output=[]
    for raw in rows:
        code=str(raw.get("c") or "");price=number(raw.get("p"));pct=number(raw.get("zdp"))
        if not re.fullmatch(r"\d{6}",code) or price is None or price<=0 or pct is None:
            raise ValueError("涨跌停池存在无效行情记录")
        streak=number(raw.get("lbc")) if kind=="U" else None
        output.append({"code":code,"name":raw.get("n") or code,"kind":kind,"date":date,"close":price/1000,
            "pct":pct,"streak":int(streak) if streak and streak>=1 else None,"fall_days":None,
            "first_time":clock(raw.get("fbt")),"last_time":clock(raw.get("lbt")),
            "open_times":number(raw.get("zbc" if kind=="U" else "oc")),"fd_amount":number(raw.get("fund")),
            "one_price":None,"sectors":[],"industry":raw.get("hybk") or "","provisional":True,
            "source":"东方财富今日"+{"U":"涨停","D":"跌停","Z":"炸板"}[kind]+"池",
            "verification":"盘中动态封板池，不代表收盘封板；跌停连续次数未取得则留空"})
    return output


def collect():
    date,quote_time=_market_stamp()
    if date!=now().date().isoformat():raise ValueError("今日尚无交易报价，不展示旧日涨跌停池")
    with ThreadPoolExecutor(max_workers=3) as workers:
        futures={kind:workers.submit(pool,kind,date) for kind in ("U","D","Z")}
        up,down=futures["U"].result(),futures["D"].result()
        try:broken=futures["Z"].result();broken_available=True
        except Exception:broken=[];broken_available=False
    return {"as_of":date,"events":up+down+broken,"sectors":[],"provisional":True,"broken_available":broken_available,
            "fetched_at":now().isoformat(),"market_quote_at":date+"T"+quote_time,
            "source":"东方财富当日盘中涨跌停池"}
