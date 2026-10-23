"""后台更新失败不损坏快照；跨市场接口复用和数据权限可诊断。"""
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from webui.services import market_health_service as service
from webui.services import market_health_cross_service as cross


class Jobs:
    def __init__(self):self.data={}
    def start(self,kind,params,worker):
        self.data["job"]={"id":"job","status":"queued"}
        worker("job",params)
        return self.data["job"]
    def get(self,key):return self.data.get(key)
    def update(self,key,**values):self.data[key].update(values)
    def append_log(self,key,message):self.data[key].setdefault("logs",[]).append(message)


def state():
    return json.loads((Path(__file__).resolve().parents[1]/"config/market_health_baseline.json").read_text())


def test_network_failure_keeps_original_snapshot(monkeypatch):
    original=state();writes=[]
    monkeypatch.setattr(service,"read_state",lambda:original)
    monkeypatch.setattr(service,"latest_job",lambda jobs:None)
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:writes.append((key,value)))
    jobs=Jobs()
    service.start_refresh("indices",jobs,lambda *a,**k:{})
    assert jobs.data["job"]["status"]=="failed"
    assert all(key!="snapshot" for key,_ in writes)


def test_full_market_failure_retains_original_market_and_breadth(monkeypatch):
    original=state();writes={}
    monkeypatch.setattr(service,"read_state",lambda:original)
    monkeypatch.setattr(service,"latest_job",lambda jobs:None)
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,10,22,20))
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:writes.update({key:value}))
    monkeypatch.setattr(cross,"collect",lambda *a,**k:{})
    monkeypatch.setattr(service,"quote_date",lambda:"2026-10-22")
    def fail(**kwargs):raise ValueError("模拟行情源不可用")
    monkeypatch.setattr(service,"fetch_market",fail)
    jobs=Jobs()
    service.start_refresh("full",jobs,lambda *a,**k:original["bars"])
    assert jobs.data["job"]["status"]=="finished"
    assert jobs.data["job"]["result"]["partial"] is True
    assert writes["snapshot"]["market"]==original["market"]
    assert writes["snapshot"]["breadth"]==original["breadth"]


def test_index_failure_does_not_block_independent_cross_sources(monkeypatch):
    original=state();writes={};calls=[]
    monkeypatch.setattr(service,"read_state",lambda:original)
    monkeypatch.setattr(service,"latest_job",lambda jobs:None)
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:writes.update({key:value}))
    monkeypatch.setattr(cross,"collect",lambda day,progress,**kwargs:calls.append((day,kwargs)) or {})
    def fail(*a,**k):raise OSError("index offline")
    jobs=Jobs();service.start_refresh("cross",jobs,fail)
    assert calls==[("2026-10-22",{"previous_date":"2026-10-21"})]
    assert jobs.data["job"]["status"]=="finished"
    assert jobs.data["job"]["result"]["partial"] is True
    assert writes["snapshot"]["bars"]==original["bars"]


def test_market_pagination_rejects_middle_gap(monkeypatch):
    def fetch(url):
        import re
        page=int(re.search(r"page=(\d+)",url).group(1))
        return [] if page==2 else [{"symbol":f"sh{page:02d}{i:04d}"} for i in range(100)]
    monkeypatch.setattr(service,"_fetch_json",fetch)
    import pytest
    with pytest.raises(ValueError,match="中间缺失"):
        service.fetch_market()


def test_market_pagination_rejects_truncated_universe(monkeypatch):
    def fetch(url):
        import re
        page=int(re.search(r"page=(\d+)",url).group(1))
        return [{"symbol":f"sh{page:02d}{i:04d}"} for i in range(100)] if page<=32 else []
    monkeypatch.setattr(service,"_fetch_json",fetch)
    import pytest
    with pytest.raises(ValueError,match="不足"):
        service.fetch_market(expected_count=5500)


def test_first_refresh_can_fetch_previous_trading_day_positions(monkeypatch):
    calls=[]
    monkeypatch.setattr(cross,"read_cached",lambda day:{})
    monkeypatch.setattr(cross,"_verified_futures",lambda:{"products":[]})
    monkeypatch.setattr(cross,"_basis_history",lambda *a:{})
    monkeypatch.setattr(cross,"_tushare_records",lambda *a:{"records":[]})
    monkeypatch.setattr(cross.quant_radar_service,"overview",lambda **kwargs:{})
    monkeypatch.setattr(cross,"compact_quant",lambda value:{})
    monkeypatch.setattr(cross,"_quant_history",lambda *a:{})
    monkeypatch.setattr(cross.kv_repo,"set_",lambda *a:None)
    def rank(variety,date):
        calls.append((variety,date))
        return {"ok":False} if date=="2026-10-22" else {"ok":True,"variety":variety,"date":date}
    monkeypatch.setattr(cross.futures_service,"position_rank",rank)
    result=cross.collect("2026-10-22",lambda message:None,previous_date="2026-10-21")
    assert len(result["futures"]["positions"])==4
    assert all(p["date"]=="2026-10-21" for p in result["futures"]["positions"])
    assert len(calls)==8


def test_historical_basis_backfills_same_current_contract_across_roll(monkeypatch):
    calls=[]
    class Pro:
        def fut_daily(self,**kwargs):
            calls.append(kwargs)
            return pd.DataFrame([{"ts_code":"IF2612.CFX","trade_date":kwargs["trade_date"],"close":98 if kwargs["trade_date"]=="20261021" else 99,"vol":200,"oi":300},
                                 {"ts_code":"IF2611.CFX","trade_date":kwargs["trade_date"],"close":101,"vol":1000 if kwargs["trade_date"]=="20261021" else 100,"oi":300}])
        def index_daily(self,**kwargs):
            return pd.DataFrame([{"trade_date":"20261021","close":100},{"trade_date":"20261022","close":100}])
    monkeypatch.setattr(cross.tushare_client,"get_pro",lambda:Pro())
    monkeypatch.setattr(cross.kv_repo,"get",lambda *a:None)
    monkeypatch.setattr(cross.kv_repo,"set_",lambda *a:None)
    monkeypatch.setattr(cross,"close_conn",lambda:None)
    result=cross._basis_history({},"2026-10-22","2026-10-21")
    current=result["quotes"]["products"][0]["main"]
    old=result["previous_quotes"]["products"][0]["main"]
    assert current["symbol"]==old["symbol"]=="IF2612"
    assert old["verified_basis_pct"]==-2.0000000000000018
    assert current["verified_basis_pct"]-old["verified_basis_pct"] > .99
    assert len(calls)==2 and all(c["exchange"]=="CFFEX" for c in calls)


def test_tushare_adapter_uses_existing_client_and_official_fields(monkeypatch):
    calls=[]
    class Pro:
        def margin(self,**kwargs):
            calls.append(("margin",kwargs))
            return pd.DataFrame([{"trade_date":"20261021","exchange_id":"SSE","rzye":100,"rzmre":10,"rzche":8,"rqye":2,"rzrqye":102}])
        def moneyflow_mkt_dc(self,**kwargs):
            calls.append(("capital",kwargs))
            return pd.DataFrame([{"trade_date":"20261022","net_amount":100000000,"net_amount_rate":2}])
    monkeypatch.setattr(cross.tushare_client,"get_pro",lambda:Pro())
    margin=cross._tushare_records("leverage","2026-10-22")
    capital=cross._tushare_records("capital","2026-10-22")
    assert margin["records"][0]["rzye"]==100
    assert capital["records"][0]["net_amount"]==100000000
    assert calls[0][0]=="margin" and calls[0][1]["end_date"]=="20261022"
    monkeypatch.setattr(cross.tushare_client,"get_pro",lambda:None)
    assert "Token" in cross._tushare_records("leverage","2026-10-22")["error"]


def test_quant_history_joins_actual_day_and_does_not_fabricate_events(monkeypatch):
    calls=[]
    class Pro:
        def daily(self,**kwargs):
            calls.append(("daily",kwargs))
            return pd.DataFrame([{"ts_code":str(i),"trade_date":"20261022","close":10,"low":10,"high":12,"pct_chg":-3} for i in range(250)])
        def daily_basic(self,**kwargs):
            return pd.DataFrame([{"ts_code":str(i),"trade_date":"20261022","volume_ratio":2} for i in range(250)])
        def moneyflow_dc(self,**kwargs):
            return pd.DataFrame([{"ts_code":str(i),"trade_date":"20261021" if i==0 else "20261022","net_amount_rate":-10} for i in range(250)])
    monkeypatch.setattr(cross.tushare_client,"get_pro",lambda:Pro())
    monkeypatch.setattr(cross.kv_repo,"get",lambda *a:None)
    writes=[]
    monkeypatch.setattr(cross.kv_repo,"set_",lambda *a:writes.append(a))
    monkeypatch.setattr(cross,"close_conn",lambda:None)
    value=cross._quant_history("2026-10-22")
    assert value["proxy"]["sample"]==249 and value["proxy"]["score"]==100
    assert value["stocks"]==[] and "gauge" not in value
    assert calls[0][1]["trade_date"]=="20261022"
    assert writes[0][1]=="quant_eod:2026-10-22"


def test_empty_quant_reply_keeps_verified_same_day_event_evidence(monkeypatch):
    saved={"snapshot_date":"2026-10-22","gauge":{"bull":30,"bear":20},
           "stocks":[{"code":"000001","direction":"砸盘"}]}
    monkeypatch.setattr(cross,"read_cached",lambda day:{"quant":saved})
    monkeypatch.setattr(cross,"_verified_futures",lambda:{"products":[]})
    monkeypatch.setattr(cross,"_basis_history",lambda *a:{})
    monkeypatch.setattr(cross,"_tushare_records",lambda *a:{})
    monkeypatch.setattr(cross.futures_service,"position_rank",lambda *a,**k:{"ok":False})
    monkeypatch.setattr(cross.quant_radar_service,"overview",lambda **kwargs:{})
    monkeypatch.setattr(cross,"compact_quant",lambda value:{})
    def unexpected(*args):raise AssertionError("真实同日盘口证据已齐，无需回退代理")
    monkeypatch.setattr(cross,"_quant_history",unexpected)
    monkeypatch.setattr(cross.kv_repo,"set_",lambda *a:None)
    result=cross.collect("2026-10-22",lambda message:None,previous_date="2026-10-21")
    assert result["quant"]==saved and "proxy" not in result["quant"]
