"""实时/收盘隔离、极值曾触及、实际时点与刷新链路。"""
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path

import pytest

from analysis.market_health import INDEXES, build_health, build_intraday_health
from webui.services import market_health_service as service, market_health_cross_service as cross


def state():
    return json.loads((Path(__file__).resolve().parents[1]/"config/market_health_baseline.json").read_text())


def quotes(original):
    return {s:{"as_of":"2026-10-23","quote_at":"2026-10-23T14:00:00","price":bars[-1]["close"],
               "previous_close":bars[-1]["close"],"open":bars[-1]["close"],
               "low":bars[-1]["close"]*.99,"high":bars[-1]["close"]*1.01}
            for s,bars in original["bars"].items()}


def test_daily_low_touch_and_rebound_are_separate_from_current_and_close():
    original=state();before=deepcopy(original);q=quotes(original)
    old=build_health(original);ma=next(r for r in old["indices"] if r["symbol"]=="sz399006")["ma250"]
    q["sz399006"].update(price=ma*.76,low=ma*.73,high=ma*.77)
    value=build_intraday_health(original,{"as_of":"2026-10-23","quotes":q})
    rule=next(r for r in value["signals"]["bottom"]["rules"] if r["key"]=="cy_dev")
    assert rule["hit"] is False and rule["touched"] is True
    assert rule["extreme_value"] < -25 < rule["value"]
    row=next(r for r in value["indices"] if r["symbol"]=="sz399006")
    assert row["ma250"]==pytest.approx((sum(r["close"] for r in original["bars"]["sz399006"][-249:])+q["sz399006"]["price"])/250)
    assert row["volume250"] is None
    assert value["confirmation"]==old["confirmation"] and value["confirmation_as_of"]=="2026-10-22"
    assert original==before
    assert next(r for r in value["signals"]["bottom"]["rules"] if r["key"]=="up")["hit"] is None
    assert next(r for r in value["signals"]["bottom"]["rules"] if r["key"]=="breadth250")["touched"] is None


def test_top_uses_high_and_prior_close_mismatch_prevents_false_live_rules():
    original=state();q=quotes(original)
    ma=build_health(original)["indices"][0]["ma250"]
    q["sh000001"].update(price=ma*1.14,high=ma*1.18,low=ma*1.13)
    live=build_intraday_health(original,{"as_of":"2026-10-23","quotes":q})
    rule=next(r for r in live["signals"]["top"]["rules"] if r["key"]=="sh_dev")
    assert rule["hit"] is False and rule["touched"] is True
    q["sh000001"]["previous_close"]*=.8
    assert build_intraday_health(original,{"as_of":"2026-10-23","quotes":q}) is None


def test_closed_day_also_exposes_low_touch_without_loosening_threshold():
    original=state()
    last=original["bars"]["sz399006"][-1]
    last["low"]=last["close"]*.95
    rule=next(r for r in build_health(original)["signals"]["bottom"]["rules"] if r["key"]=="cy_dev")
    assert rule["hit"] is False and rule["near"] is True
    assert rule["touched"] is True
    assert rule["gap"]==pytest.approx(.2169487289,abs=.01)


def test_quotes_have_provider_time_and_lunch_is_not_a_stale_tick(monkeypatch):
    fields=[""]*32
    for i,v in {1:"2500",2:"2501",3:"2490",4:"2510",5:"2400",30:"2026-10-23",31:"11:30:00"}.items():fields[i]=v
    parsed=service.parse_live_quotes('var hq_str_sz399006="'+",".join(fields)+'";')
    assert parsed["sz399006"]["low"]==2400 and parsed["sz399006"]["quote_at"]=="2026-10-23T11:30:00"
    value={"quotes":{"sh000001":parsed["sz399006"]},"fetched_at":"2026-10-23T12:30:00+08:00"}
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,10,23,12,30))
    assert service.live_status(value,"2026-10-22")["available"] is True
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,10,23,14,0))
    assert service.live_status(value,"2026-10-22")["available"] is False
    assert "滞后" in service.live_status(value,"2026-10-22")["message"]
    value["quotes"]["sh000001"].update(as_of="2026-10-22",quote_at="2026-10-22T15:00:00")
    assert "尚无今日有效报价" in service.live_status(value,"2026-10-22")["message"]


def test_query_keeps_both_views_and_each_rule_actual_input_date(monkeypatch):
    original=state();q=quotes(original)
    data={"live_quotes":{"quotes":q,"fetched_at":"2026-10-23T14:00:05+08:00"}}
    monkeypatch.setattr(service,"read_state",lambda:original)
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,10,23,14,1))
    monkeypatch.setattr(service.kv_repo,"get",lambda ns,key:(data[key],None) if key in data else None)
    monkeypatch.setattr(cross,"read_cached",lambda date:original["cross"])
    value=service.query()
    assert value["as_of"]=="2026-10-22" and value["intraday"]["as_of"]=="2026-10-23"
    rules={r["key"]:r for r in value["intraday"]["signals"]["bottom"]["rules"]}
    assert rules["cy_dev"]["data_at"]=="2026-10-23T14:00:00"
    assert rules["pb"]["as_of"]=="2026-10-22" and rules["pb"]["hit"] is None
    assert all(r["as_of"] and r["data_at"] for r in value["timing"])
    assert any("席位" in r["label"] for r in value["intraday"]["timing"])


def test_live_failure_preserves_success_time_and_rejects_future_quotes(monkeypatch):
    original=state();q=quotes(original)
    old={"quotes":q,"fetched_at":"2026-10-23T13:00:00+08:00"}
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,10,23,14,1))
    monkeypatch.setattr(service.kv_repo,"get",lambda *a:(old,None))
    writes=[]
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:writes.append(value))
    def fail(*args,**kwargs):raise OSError("offline")
    monkeypatch.setattr(service,"request_text",fail)
    value=service.fetch_live_quotes(force=True)
    assert value["error"] and value["quotes"]==q
    assert value["quotes"]["sh000001"]["quote_at"]=="2026-10-23T14:00:00"
    assert writes
    monkeypatch.setattr(service,"request_text",lambda *a,**k:"")
    monkeypatch.setattr(service,"parse_live_quotes",lambda *a:{"sh000001":{**q["sh000001"],"quote_at":"2026-10-23T15:00:00"}})
    assert service.fetch_live_quotes(force=True)["quotes"]==q


def test_closed_view_restores_baseline_quant_when_only_newer_cache_exists(monkeypatch):
    baseline={"quant":{"snapshot_date":"2026-10-22","gauge":{"date":"2026-10-22","bull":4,"bear":2}},"futures":{}}
    newer={"quant":{"snapshot_date":"2026-10-23","gauge":{"date":"2026-10-23","bull":40,"bear":20}},"futures":{}}
    monkeypatch.setattr(service,"read_baseline",lambda:{"cross":baseline})
    monkeypatch.setattr(cross.kv_repo,"get",lambda ns,key:(newer,None) if key=="cross" else None)
    monkeypatch.setattr(cross,"cached_positions",lambda _:[])
    monkeypatch.setattr(cross.quant_radar_repo,"list_dates",lambda **kwargs:["2026-10-23"])
    monkeypatch.setattr(cross.futures_service,"_overview_cache",{"payload":None})
    assert cross.read_cached("2026-10-22")["quant"]==baseline["quant"]


def test_market_retries_transient_middle_page_but_keeps_full_universe(monkeypatch):
    import re
    attempts={}
    def fetch(url):
        page=int(re.search(r"page=(\d+)",url).group(1))
        attempts[page]=attempts.get(page,0)+1
        if page==2 and attempts[page]==1:return []
        return [{"symbol":f"sh{page:02d}{i:04d}"} for i in range(100)] if page<=32 else []
    monkeypatch.setattr(service,"_fetch_json",fetch)
    assert len(service.fetch_market())==3200
    assert attempts[2]==2


def test_intraday_breadth_uses_current_quotes_and_preserves_historical_window(monkeypatch):
    day="2026-10-23"
    bars=[{"date":(dt.date(2026,10,22)-dt.timedelta(days=299-i)).isoformat(),"close":100+i*.1} for i in range(300)]
    rows=[{"symbol":f"sh{600000+i}","trade":50,"open":100,"high":110,"low":49,"volume":200} for i in range(200)]
    monkeypatch.setattr(service,"_fetch_json",lambda url:bars)
    calendar=[r["date"] for r in bars[-4:]]+[day]
    result=service.fetch_breadth(rows,day,lambda *a:None,[r["symbol"] for r in rows],calendar,intraday=True)
    assert result["above_ma250"]==0 and result["newlow60"]==100
    assert result["observations"][-2]["above_ma250"]==100
    assert len({o["cohort"] for o in result["observations"]})==1
    assert bars[-1]["close"]==129.9


def test_full_refresh_before_close_updates_today_sampling_without_overwriting_close(monkeypatch):
    original=state();before=deepcopy(original);q=quotes(original);written={};calls=[]
    monkeypatch.setattr(service,"read_state",lambda:original)
    monkeypatch.setattr(service,"latest_job",lambda jobs:None)
    monkeypatch.setattr(service,"now",lambda:dt.datetime(2026,10,23,14,1))
    monkeypatch.setattr(service,"fetch_live_quotes",lambda **kwargs:{"quotes":q})
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:written.update({key:deepcopy(value)}))
    monkeypatch.setattr(service,"quote_date",lambda:"2026-10-23")
    monkeypatch.setattr(service,"fetch_market",lambda **kwargs:[{"symbol":f"sh{i:06d}","trade":10,"changepercent":-1,"pb":2} for i in range(3200)])
    def breadth(rows,day,progress,symbols,calendar,**kwargs):
        calls.append((day,calendar[-1],kwargs))
        return {**before["breadth"],"as_of":day,"observations":[{**o,"as_of":day} for o in before["observations"][-1:]]}
    monkeypatch.setattr(service,"fetch_breadth",breadth)
    monkeypatch.setattr(cross,"collect",lambda day,*a,**k:calls.append(day) or before["cross"])
    class Jobs:
        data={"id":"job","status":"queued"}
        def start(self,kind,params,worker):worker("job",params);return self.data
        def update(self,id,**kwargs):self.data.update(kwargs)
        def append_log(self,*args):pass
    job=service.start_refresh("full",Jobs(),lambda *a,**kwargs:before["bars"])
    assert job["status"]=="finished"
    assert calls[0]=="2026-10-23" and calls[1]==("2026-10-23","2026-10-23",{"intraday":True})
    assert written["snapshot"]["market"]==before["market"]
    assert written["snapshot"]["observations"]==before["observations"]
    assert written["intraday"]["market"]["as_of"]=="2026-10-23"
    assert written["intraday"]["breadth"]["provisional"] is True
