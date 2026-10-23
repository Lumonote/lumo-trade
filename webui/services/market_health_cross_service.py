"""复用期货、量化和TuShare通道，读取现有缓存并按需刷新跨市场证据。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
import datetime as dt
import json
import re

from analysis.market_health import number
from analysis.market_health_fusion import iso, quant_eod_proxy
from data_store import kv_repo, quant_radar_repo, tushare_client
from data_store.connection import get_conn, close_conn
from webui.services import futures_service, quant_radar_service
from webui.services.http_client import request_text

NAMESPACE = "market_health"


def cached_positions(as_of):
    result = []
    for variety in ("IF","IH","IC","IM"):
        row = get_conn().execute("SELECT key,payload FROM kv_cache WHERE namespace='futures_rank' "
                                 "AND key LIKE ? AND SUBSTR(key,4)<=? ORDER BY key DESC LIMIT 1",
                                 (f"{variety}:%",str(as_of).replace("-",""))).fetchone()
        if row:
            try:
                payload = json.loads(row["payload"])
                result.append({**payload,"variety":variety,"date":iso(row["key"].split(":")[-1])})
            except (ValueError, TypeError):
                pass
    return result


def compact_quant(payload):
    keep = ("gauge","snapshot_date","fallback_date","sectors","note","error","proxy")
    result = {k:payload.get(k) for k in keep}
    # 完整按日榜覆盖优先于kv仅保存的前300只，避免候选样本分母被截断。
    date = iso(payload.get("fallback_date") or payload.get("snapshot_date") or (payload.get("gauge") or {}).get("date"))
    rows = quant_radar_repo.get_day(date,limit=0) if date else []
    rows = rows or payload.get("stocks") or []
    result["stocks"] = [{k:r.get(k) for k in ("code","direction","level_rank","activity","changes_bull","changes_bear")} for r in rows]
    return result


def read_cached(as_of):
    from webui.services.market_health_service import read_baseline
    hit = kv_repo.get(NAMESPACE,"cross")
    result = {**read_baseline().get("cross",{}), **(hit[0] if hit else {})}
    futures = dict(result.get("futures", {}))
    positions = cached_positions(as_of)
    if positions:
        merged = {p["variety"]:p for p in futures.get("positions",[])}
        for p in positions:
            if p.get("date", "") >= merged.get(p["variety"],{}).get("date", ""):
                merged[p["variety"]] = p
        futures["positions"] = list(merged.values())
    memory = futures_service._overview_cache.get("payload")
    if memory and not futures.get("quotes"):
        # 既有行情没有同日现货校验：可展示合约，不直接参与基差判断。
        futures["quotes"] = memory
    result["futures"] = futures
    dates = [d for d in quant_radar_repo.list_dates(limit=120) if iso(d) and iso(d) <= str(as_of)]
    if dates:
        date = dates[0]
        old_date = iso((result.get("quant") or {}).get("fallback_date") or (result.get("quant") or {}).get("snapshot_date") or ((result.get("quant") or {}).get("gauge") or {}).get("date"))
        if not old_date or iso(date) >= old_date:
            cached = kv_repo.get("quant_radar",f"day:{str(date).replace('-','')}")
            rows = quant_radar_repo.get_day(date,limit=0)
            payload = cached[0] if cached else {"gauge":{"date":iso(date),"bull":sum(r.get("changes_bull") or 0 for r in rows),"bear":sum(r.get("changes_bear") or 0 for r in rows)},"snapshot_date":iso(date),"stocks":rows}
            result["quant"] = compact_quant(payload)
    return result


def _deadline(fn, seconds=65):
    def worker():
        try:
            return fn()
        finally:
            close_conn()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(worker).result(timeout=seconds)
    except TimeoutError:
        return {"error":"接口超时，保留已有数据"}
    except Exception as exc:
        return {"error":f"接口暂不可用（{type(exc).__name__}），保留已有数据"}
    finally:
        pool.shutdown(wait=False)


def _verified_futures():
    from copy import deepcopy
    payload = deepcopy(futures_service.overview(force=True))
    try:
        raw = request_text("https://hq.sinajs.cn/list=sh000300,sh000016,sh000905,sh000852",
                           headers={"Referer":"https://finance.sina.com.cn/","User-Agent":"Mozilla/5.0"},
                           encoding="gbk",timeout=8,trust_env=False)
    except Exception:
        raw = ""
        payload["validation_error"] = "现货日期校验不可用，保留期货报价但不计算升贴水"
    spots = {}
    for symbol, body in re.findall(r'hq_str_(sh\d{6})="([^"]+)"',raw):
        fields = body.split(",")
        if len(fields) > 31:
            spots[symbol] = {"price":number(fields[3]),"date":iso(fields[30])}
    for product in payload.get("products", []):
        spot = spots.get((product.get("index") or {}).get("code"),{})
        for contract in product.get("contracts", []):
            contract["verified_basis_pct"] = None
            if spot.get("date") and iso(contract.get("tradedate")) == spot["date"] and spot.get("price") and number(contract.get("price")) is not None:
                contract["verified_basis_pct"] = (contract["price"]/spot["price"]-1)*100
        main = product.get("main") or {}
        product["main"] = next((c for c in product.get("contracts",[]) if c.get("symbol") == main.get("symbol")),main)
    return payload


def _tushare_records(kind, as_of):
    pro = tushare_client.get_pro()
    if pro is None:
        return {"error":"TuShare未配置或不可用，请在后台配置检查现有Token"}
    start = (dt.date.fromisoformat(as_of)-dt.timedelta(days=75)).strftime("%Y%m%d")
    end = as_of.replace("-","")
    if kind == "leverage":
        df = pro.margin(start_date=start,end_date=end,fields="trade_date,exchange_id,rzye,rzmre,rzche,rqye,rzrqye")
    else:
        df = pro.moneyflow_mkt_dc(start_date=start,end_date=end)
    if df is None or getattr(df,"empty",True):
        return {"error":"接口未返回数据，请检查数据权限或披露时间"}
    fields = ("trade_date","exchange_id","rzye","rzmre","rzche","rqye","rzrqye") if kind == "leverage" else ("trade_date","net_amount","net_amount_rate","buy_elg_amount","buy_sm_amount")
    records = [{k:(str(r.get(k)) if k in ("trade_date","exchange_id") else number(r.get(k))) for k in fields} for r in df.to_dict("records")]
    return {"records":records,"source":kind}


def _basis_history(quote, as_of, previous_date):
    """补取两天真实收盘，按本日主力的同一合约回看上一交易日。"""
    from copy import deepcopy
    pro = tushare_client.get_pro()
    if pro is None or not previous_date:
        return {"error":"历史基差需要现有TuShare客户端及前一交易日"}
    days = (previous_date, as_of)
    def daily(day):
        hit = kv_repo.get(NAMESPACE,f"futures_daily:{day}")
        if hit:
            return hit[0]
        df = pro.fut_daily(trade_date=day.replace("-",""), exchange="CFFEX",
                           fields="ts_code,trade_date,close,vol,oi")
        rows = [] if df is None else df.to_dict("records")
        records = [{"symbol":str(r.get("ts_code", "")).split(".")[0],
                    "tradedate":iso(r.get("trade_date")),"price":number(r.get("close")),
                    "volume":number(r.get("vol")),"position":number(r.get("oi"))} for r in rows
                   if re.fullmatch(r"I[HFMC]\d{4}\.CFX",str(r.get("ts_code", "")))
                   and iso(r.get("trade_date")) == day and (number(r.get("close")) or 0) > 0]
        if records:
            kv_repo.set_(NAMESPACE,f"futures_daily:{day}",records)
        return records
    def spot(product):
        variety, code = product["variety"], product["ts_index"]
        hit = kv_repo.get(NAMESPACE,f"basis_spot:{code}:{previous_date}:{as_of}")
        if hit:
            return variety, hit[0]
        df = pro.index_daily(ts_code=code,start_date=previous_date.replace("-",""),
                             end_date=as_of.replace("-",""),fields="ts_code,trade_date,close")
        prices = {iso(r.get("trade_date")):number(r.get("close")) for r in ([] if df is None else df.to_dict("records"))
                  if iso(r.get("trade_date")) in days and (number(r.get("close")) or 0) > 0}
        if len(prices) == 2:
            kv_repo.set_(NAMESPACE,f"basis_spot:{code}:{previous_date}:{as_of}",prices)
        return variety, prices
    def safe(fn, arg):
        try:
            return fn(arg)
        finally:
            close_conn()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(safe,daily,d) for d in days]
        spots = [pool.submit(safe,spot,p) for p in futures_service.PRODUCTS]
        prior, current = [f.result() for f in futures]
        prices = dict(f.result() for f in spots)
    payload = deepcopy(quote)
    products = {p["variety"]:p for p in payload.get("products",[])}
    previous_products, errors = [], []
    for spec in futures_service.PRODUCTS:
        variety = spec["variety"]
        product = products.get(variety, {"variety":variety,"index":{"code":spec["index_code"]}})
        for day, rows in ((as_of,current),(previous_date,prior)):
            price = prices.get(variety,{}).get(day)
            for row in rows:
                if row["symbol"].startswith(variety) and price:
                    row["verified_basis_pct"] = (row["price"]/price-1)*100
                    row["spot_price"] = price
                    row["source"] = "TuShare fut_daily + index_daily · 收盘"
        contracts = [r for r in current if r["symbol"].startswith(variety) and r.get("verified_basis_pct") is not None]
        if contracts:
            product.update(contracts=contracts,main=max(contracts,key=lambda r:r.get("volume") or 0))
            products[variety] = product
        main = product.get("main") or {}
        old = next((r for r in prior if r["symbol"] == main.get("symbol") and r.get("verified_basis_pct") is not None),None)
        if old:
            previous_products.append({"variety":variety,"main":old})
        else:
            errors.append(f"{variety}缺少{previous_date}同合约期现收盘")
    payload["products"] = list(products.values())
    return {"quotes":payload,"previous_quotes":{"products":previous_products},"errors":errors}


def _quant_history(as_of):
    """历史事件无法重放时，接入有日期的收盘行为证据，不编造盘口事件。"""
    hit = kv_repo.get(NAMESPACE,f"quant_eod:{as_of}")
    if hit:
        return hit[0]
    pro = tushare_client.get_pro()
    if pro is None:
        return {"error":"量化历史回补需要现有TuShare客户端"}
    day = as_of.replace("-", "")
    fields = {"daily":"ts_code,trade_date,close,high,low,pct_chg",
              "daily_basic":"ts_code,trade_date,volume_ratio",
              "moneyflow_dc":"ts_code,trade_date,net_amount_rate"}
    def fetch(kind):
        try:
            rows = []
            # 显式分页防止未来全市场超过6000只时截断分母。
            for offset in range(0,18000,6000):
                df = getattr(pro,kind)(trade_date=day,fields=fields[kind],limit=6000,offset=offset)
                batch = [] if df is None else df.to_dict("records")
                rows.extend(batch)
                if len(batch) < 6000:
                    break
            else:
                raise ValueError("收盘代理分页覆盖超过上限")
            return {str(r["ts_code"]):r for r in rows if iso(r.get("trade_date")) == as_of}
        finally:
            close_conn()
    with ThreadPoolExecutor(max_workers=3) as pool:
        daily, basic, flows = list(pool.map(fetch,fields))
    rows = [{**{k:number(v) for k,v in r.items() if k not in ("ts_code","trade_date")},
             "volume_ratio":number(basic.get(code,{}).get("volume_ratio")),
             "net_amount_rate":number(flows.get(code,{}).get("net_amount_rate"))} for code,r in daily.items()]
    proxy = quant_eod_proxy(rows)
    if not proxy:
        return {"error":"历史价量资金交集不足200只或放量组不足20只，保留已有量化证据"}
    payload = {"snapshot_date":as_of,"proxy":proxy,"stocks":[],
               "note":"历史收盘行为回补；未将价量资金代理冒充盘口异动或真实量化成交"}
    kv_repo.set_(NAMESPACE,f"quant_eod:{as_of}",payload)
    return payload


def collect(as_of, progress, previous_date=None):
    previous = read_cached(as_of)
    result = {**previous}

    def futures():
        data = dict(previous.get("futures",{}))
        old = data.get("quotes")
        positions = {p.get("variety"):p for p in data.get("positions",[])}
        def rank(variety):
            current = _deadline(lambda:futures_service.position_rank(variety,date=as_of),40)
            if current.get("ok"):
                return current
            cached = positions.get(variety) or {}
            if previous_date and cached.get("date") not in (as_of,previous_date):
                return _deadline(lambda:futures_service.position_rank(variety,date=previous_date),40)
            return current
        # 行情与席位独立读取，避免先等行情再等四品种席位。
        with ThreadPoolExecutor(max_workers=5) as pool:
            quote_future = pool.submit(_deadline,_verified_futures,80)
            fetched = list(pool.map(rank,("IF","IH","IC","IM")))
            quote = quote_future.result()
        if quote.get("products"):
            # 同日重复更新不能覆盖真正的上一交易日参照。
            old_dates = {iso((p.get("main") or {}).get("tradedate")) for p in (old or {}).get("products",[])}
            if any(d and d < as_of for d in old_dates):
                data["previous_quotes"] = old
            data["quotes"] = quote
        data["errors"] = [quote[k] for k in ("error","validation_error") if quote.get(k)]
        history = _deadline(lambda:_basis_history(data.get("quotes",{}),as_of,previous_date),45)
        if history.get("quotes"):
            data.update(quotes=history["quotes"],previous_quotes=history["previous_quotes"])
        data["errors"].extend(history.get("errors",[]))
        if history.get("error"):
            data["errors"].append(history["error"])
        for p in fetched:
            if p.get("ok"):
                positions[p["variety"]] = p
        data["positions"] = list(positions.values())
        return data

    def quant():
        value = compact_quant(quant_radar_service.overview(force=True,date=as_of))
        gauge = value.get("gauge") or {}
        if value.get("stocks") and (number(gauge.get("bull")) or 0)+(number(gauge.get("bear")) or 0) > 0:
            return value
        old = previous.get("quant") or {}
        old_gauge = old.get("gauge") or {}
        if (iso(old.get("fallback_date") or old.get("snapshot_date") or old_gauge.get("date")) == as_of
            and old.get("stocks") and (number(old_gauge.get("bull")) or 0)+(number(old_gauge.get("bear")) or 0) > 0):
            return old
        return _quant_history(as_of)

    actions = {"futures":futures,"leverage":lambda:_tushare_records("leverage",as_of),
               "quant":quant,"capital":lambda:_tushare_records("capital",as_of)}
    def run(key):
        try:
            return _deadline(actions[key],130 if key == "futures" else 85)
        finally:
            close_conn()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures_by_key = {pool.submit(run,key):key for key in actions}
        for i,future in enumerate(as_completed(futures_by_key),1):
            key = futures_by_key[future]
            value = future.result()
            if value.get("error") and previous.get(key):
                result[key] = {**previous[key],"error":value["error"]}
            else:
                result[key] = value
            progress(f"{LABELS[key]}证据已读取（{i}/4）")
    result["updated_at"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
    kv_repo.set_(NAMESPACE,"cross",result)
    return result


LABELS = {"futures":"股指期货","leverage":"融资融券","quant":"量化雷达","capital":"大盘资金"}
