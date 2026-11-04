"""按真实BK代码采集成分；报价和成分的覆盖状态分别保留。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import re
from urllib.parse import urlencode

from analysis.market_health import number
from webui.services import sector_depth_service
from webui.services.http_client import request_json
from webui.services.market_health_service import now


def fetch(code, date):
    if not re.fullmatch(r"BK\d{4,6}", code):
        raise ValueError("成分查询需要真实BK代码")
    try:
        rows = sector_depth_service._records("dc_member", ts_code=code+".DC", trade_date=date.replace("-", ""))
        members = {}
        for row in rows:
            day = str(row.get("trade_date") or "").replace("-", "")
            if day != date.replace("-", "") or row.get("ts_code") != code+".DC":
                raise ValueError("成分接口返回了其他板块或其他日期")
            stock = str(row.get("con_code") or "").split(".")[0]
            if not re.fullmatch(r"\d{6}", stock):
                raise ValueError("成分接口返回无效股票代码")
            members[stock] = {"code":stock, "name":row.get("name") or stock}
        if members:
            return {"date":date, "members":list(members.values()), "complete":True,
                    "source":"TuShare dc_member 事件日成分", "fetched_at":now().isoformat()}
    except Exception:
        pass
    # 实时成分接口没有历史日期参数，只允许在今日读取当前成分快照。
    if date != now().date().isoformat():
        raise ValueError("未取得所选日真实成分")
    members, total, page_size = {}, None, 100
    for page in range(1, 101):
        params = {"pn":page, "pz":page_size, "po":1, "np":1, "fltt":2, "invt":2,
                  "fid":"f12", "fs":"b:"+code, "fields":"f12,f14"}
        url = "https://push2.eastmoney.com/api/qt/clist/get?"+urlencode(params)
        headers = {"User-Agent":"Mozilla/5.0", "Referer":"https://quote.eastmoney.com/"}
        try:
            value = request_json(url, headers=headers, timeout=5, retries=1, trust_env=False)
        except Exception:
            value = request_json(url.replace("push2.eastmoney.com", "push2delay.eastmoney.com"),
                                 headers=headers, timeout=5, retries=1, trust_env=False)
        data = (value or {}).get("data") or {}
        batch = data.get("diff") or []
        if isinstance(batch, dict):
            batch = list(batch.values())
        count = number(data.get("total"))
        if count is None or count <= 0 or int(count) != count or total is not None and total != count:
            raise ValueError("成分总数缺失或采集期间变化")
        total = int(count)
        for row in batch:
            stock = str(row.get("f12") or "")
            if not re.fullmatch(r"\d{6}", stock) or stock in members:
                raise ValueError("成分页重复或含无效股票")
            members[stock] = {"code":stock, "name":row.get("f14") or stock}
        if len(members) == total:
            return {"date":date, "members":list(members.values()), "complete":True,
                    "source":"东方财富当前成分快照（日期为采集日）", "fetched_at":now().isoformat()}
        if not batch or len(members) > total:
            break
        if page == 1:
            page_size = len(batch)
    raise ValueError("成分分页不完整，未作为完整成分发布")


def collect(sectors, previous=None, batch_size=24):
    """优先行业/概念资金前12，再逐批补齐；失败不覆盖已有真实关系。"""
    date = now().date().isoformat()
    saved = dict(previous or {})
    rows = [r for r in sectors.get("rows", []) if re.fullmatch(r"BK\d{4,6}", str(r.get("code") or ""))]
    def score(row):
        live = row.get("live") or {}
        value = number(live.get("net")) if str(live.get("quote_at") or "")[:10] == date else number(row.get("net"))
        return value if value is not None else -float("inf")
    ranked = sorted(rows, key=score, reverse=True)
    leaders = [r for kind in ("行业", "概念") for r in [x for x in ranked if x.get("type", "行业") == kind][:12]]
    pending, seen = [], set()
    for row in leaders+ranked:
        code = row["code"]
        if code in seen:
            continue
        seen.add(code)
        old = saved.get(code) or {}
        if old.get("date") == date and old.get("complete"):
            continue
        # 失败板块冷却10分钟，让后续批次仍能补齐其余板块。
        try:
            recent_failure = old.get("error") and (now()-dt.datetime.fromisoformat(old["attempted_at"])).total_seconds() < 600
        except (KeyError, TypeError, ValueError):
            recent_failure = False
        if not recent_failure:
            pending.append(code)
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = {workers.submit(fetch, code, date):code for code in pending[:batch_size]}
        for future in as_completed(futures):
            code = futures[future]
            try:
                saved[code] = future.result()
            except Exception:
                saved[code] = {**saved.get(code, {}), "error":"真实成分暂未取得，自动重试中",
                               "attempted_at":now().isoformat()}
    return saved
