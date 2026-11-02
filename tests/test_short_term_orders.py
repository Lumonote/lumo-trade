"""大笔买卖监控：真实事件、独立失败降级、跨日边界和并发刷新。"""
import datetime as dt
import threading
import time

import pytest

from analysis.short_term_screen import build_report
from webui.services import short_term_orders as orders


@pytest.fixture
def cache(monkeypatch):
    values={}
    monkeypatch.setattr(orders.kv_repo,"get",lambda ns,key:(values[key],0) if key in values else None)
    monkeypatch.setattr(orders.kv_repo,"set_",lambda ns,key,value:values.__setitem__(key,value))
    monkeypatch.setattr(orders,"now",lambda:dt.datetime.fromisoformat("2026-11-02T10:00:00+08:00"))
    return values


def event(side="buy",code="600001",date="2026-11-02",stamp=93001):
    return orders.normalize([{"c":code,"n":"测试股票","t":orders.KINDS[side][0],"tm":stamp,"i":"1000,12.5,0.02"}],side,date)[0]


def test_normalize_preserves_real_event_time_and_never_invents_order_amount():
    raw={"c":"600001","n":"<b>股票</b>","t":8193,"tm":93001,"i":"1000,12.5,0.02"}
    rows=orders.normalize([raw,raw,{**raw,"t":8194},{**raw,"tm":250000},{**raw,"tm":150000},{**raw,"c":"invalid"}],"buy","2026-11-02","10:00:00")
    assert len(rows)==1 and rows[0]["time"]=="09:30:01" and rows[0]["name"]=="股票"
    assert rows[0]["occurred_at"]=="2026-11-02T09:30:01" and rows[0]["amount"] is None
    assert rows[0]["raw_info"]==raw["i"] and "时分秒" in rows[0]["date_basis"]


def test_merge_deduplicates_and_does_not_mix_trading_days():
    first=event();second=event(stamp=93100)
    old={"date":"2026-11-02","rows":[first]}
    result=orders.merge(old,{"date":"2026-11-02","rows":[first,second]})
    assert len(result["rows"])==2 and result["rows"][0]["time"]=="09:31:00"
    changed=orders.merge(old,{"date":"2026-11-03","rows":[event(date="2026-11-03")]})
    assert len(changed["rows"])==1 and changed["rows"][0]["date"]=="2026-11-03"


def test_amount_fields_require_four_values_and_quantity_price_validation():
    raw={"c":"300051","n":"股票","t":8193,"tm":101631,"i":"368600,6.10000,-0.001645,2248460.00"}
    row=orders.normalize([raw],"buy","2026-11-02")[0]
    assert row["amount"]==2248460 and row["volume"]==368600 and row["price"]==6.1
    assert row["change_pct"]==-.1645
    invalid=orders.normalize([{**raw,"i":"368600,6.1,-0.001645,300"}],"buy","2026-11-02")[0]
    assert invalid["amount"] is None and invalid["price"] is None


def test_failed_sell_source_preserves_its_actual_old_date_while_buy_updates(cache):
    old_sell=event("sell",date="2026-10-30")
    cache["orders"]={"sell":{"date":"2026-10-30","rows":[old_sell],"fetched_at":"2026-10-30T15:00:00+08:00"}}
    def fetch(side,date,clock):
        if side=="sell":raise OSError("offline")
        return {"date":date,"rows":[event()],"fetched_at":"2026-11-02T10:00:00+08:00","error":""}
    result=orders.collect(fetch,lambda:("2026-11-02","10:00:00"))
    assert result["buy"]["date"]=="2026-11-02"
    assert result["sell"]["date"]=="2026-10-30" and result["sell"]["fetched_at"].startswith("2026-10-30")
    assert result["sell"]["error"] and cache["orders:2026-11-02"]["buy"]["rows"]
    assert "orders:2026-10-30" not in cache


def test_quote_date_failure_retains_records_and_historical_queries_never_start_network(cache):
    cache["orders"]={"buy":{"date":"2026-10-30","rows":[event(date="2026-10-30")]}}
    def fail():raise OSError("offline")
    result=orders.collect(market_stamp=fail)
    assert result["error"] and result["buy"]["date"]=="2026-10-30"
    assert orders.snapshot(date="2026-10-29",auto=True)["big_buys"]==[]
    with pytest.raises(ValueError):orders.snapshot(date="bad")


def test_concurrent_polling_starts_only_one_background_fetch(cache,monkeypatch):
    entered=threading.Event();release=threading.Event();done=threading.Event();calls=[]
    monkeypatch.setattr(orders,"_running",False);monkeypatch.setattr(orders,"_attempt",-1000)
    monkeypatch.setattr(orders,"close_conn",lambda:done.set())
    def collect():calls.append(1);entered.set();release.wait(2)
    monkeypatch.setattr(orders,"collect",collect)
    orders.snapshot(auto=True);assert entered.wait(1)
    for _ in range(8):assert orders.snapshot(auto=True)["refreshing"]
    release.set();assert done.wait(1)
    assert len(calls)==1


def test_report_includes_new_order_stock_without_fabricating_price_or_industry():
    buy=event()
    value=build_report({"orders":{"big_buys":[buy],"big_sells":[],"order_meta":{"buy":{"date":"2026-11-02"}}}},"2026-11-02","2026-11-02T10:00:00+08:00")
    assert value["big_buys"]==[buy] and value["order_events"]==[buy]
    stock=value["stocks"][0]
    assert stock["code"]=="600001" and stock["price"] is None and stock["sector_ids"]==[]
    assert stock["order_events"]==[buy]
