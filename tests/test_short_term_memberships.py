"""成分与矩阵的完整性、日期、去重及分区更新回归。"""
import datetime as dt
from urllib.parse import parse_qs, urlsplit

import pytest

from analysis.short_term_screen import build_report
from webui.services import short_term_memberships as provider, short_term_screen_service as service

DAY = "2026-11-04"


def source():
    return {"radar":{"snapshot_date":DAY,"stocks":[
        {"code":"600001","name":"示例甲","industry":"原雷达行业","activity":70,"direction":"拉抬","changes_total":4},
        {"code":"600002","name":"示例乙","industry":"半导体","activity":50,"direction":"砸盘","changes_total":2}]},
        "sectors":{"as_of":DAY,"rows":[{"code":"BK0001","name":"半导体","type":"行业","net":2.5e8,"pct":2},
                                         {"code":"BK0002","name":"未覆盖板块","type":"行业","net":-3e8,"pct":-1}],
                   "memberships":{"BK0001":{"date":DAY,"complete":True,"source":"真实成分测试",
                       "members":[{"code":"600001","name":"示例甲"},{"code":"600003","name":"示例丙"}]}}},
        "limits":{"as_of":DAY,"provisional":True,"events":[
            {"code":"600001","name":"示例甲","date":DAY,"kind":"U"},
            {"code":"600001","name":"示例甲","date":DAY,"kind":"U"}]},
        "jinshi":[{"title":"示例甲600001发布公告","time":DAY+" 10:00"},
                  {"title":"芯片行业泛题材信息","time":DAY+" 10:01"},
                  {"title":"半导体昨日消息","time":"2026-11-03 10:00"}]}


def test_true_board_members_override_name_matches_and_supply_graph_only_stocks():
    value=build_report(source(),DAY,DAY+"T10:02",current_only=True)
    board=next(r for r in value["sectors"] if r["code"]=="BK0001")
    assert board["stock_codes"]==["600001","600003"]
    assert board["membership"]["total"]==2 and board["membership"]["complete"]
    placeholder=next(r for r in value["stocks"] if r["code"]=="600003")
    assert placeholder["membership_only"] and placeholder["price"] is None
    assert any(e["source"]==board["id"] for e in value["graph"]["edges"])
    assert next(r for r in value["stocks"] if r["code"]=="600002")["sector_ids"]==[]


def test_signal_units_unknown_zero_lower_bounds_and_exact_evidence():
    value=build_report(source(),DAY,DAY+"T10:02",current_only=True)
    board=next(r for r in value["sectors"] if r["code"]=="BK0001")
    signals=board["signal_details"]
    assert signals["拉抬"]["value"]==1 and signals["拉抬"]["stock_codes"]==["600001"]
    assert signals["拉抬"]["covered"]==1 and signals["拉抬"]["total"]==2 and not signals["拉抬"]["complete"]
    assert signals["涨停"]["value"]==1 and signals["涨停"]["complete"]
    assert signals["跌停"]["value"]==0 and signals["跌停"]["complete"]
    assert signals["资金流入"]["value"]==2.5e8 and signals["资金流入"]["unit"]=="元"
    assert signals["消息提及"]["value"]==1 and len(signals["消息提及"]["news_ids"])==1
    unknown=next(r for r in value["sectors"] if r["code"]=="BK0002")
    assert unknown["signals"]["涨停"] is None and unknown["signals"]["拉抬"] is None
    assert unknown["signals"]["资金流入"]==-3e8


def test_old_membership_is_not_reported_as_current_and_old_radar_is_not_counted():
    data=source()
    data["sectors"]["memberships"]["BK0001"]["date"]="2026-11-03"
    data["radar"]["stocks"][1]["trade_date"]="2026-11-03"
    value=build_report(data,DAY,DAY+"T10:02",current_only=True)
    board=next(r for r in value["sectors"] if r["code"]=="BK0001")
    assert not board["membership"]["complete"] and board["membership"]["date"]=="2026-11-03"
    assert not board["stock_codes"] and board["signals"]["拉抬"] is None


def test_member_fallback_validates_pagination_and_rejects_duplicate_pages(monkeypatch):
    monkeypatch.setattr(provider,"now",lambda:dt.datetime(2026,11,4,10,0))
    monkeypatch.setattr(provider.sector_depth_service,"_records",lambda *a,**kw:[])
    def request(url,**kwargs):
        page=int(parse_qs(urlsplit(url).query)["pn"][0])
        return {"data":{"total":3,"diff":[{"f12":"600001","f14":"甲"},{"f12":"600002","f14":"乙"}] if page==1 else [{"f12":"600003","f14":"丙"}]}}
    monkeypatch.setattr(provider,"request_json",request)
    value=provider.fetch("BK0001",DAY)
    assert value["complete"] and len(value["members"])==3
    monkeypatch.setattr(provider,"request_json",lambda *a,**kw:{"data":{"total":3,"diff":[{"f12":"600001"}]}})
    with pytest.raises(ValueError,match="重复"):provider.fetch("BK0001",DAY)
    with pytest.raises(ValueError,match="所选日"):provider.fetch("BK0001","2026-11-03")


def test_provider_rejects_wrong_board_or_day_from_daily_source(monkeypatch):
    monkeypatch.setattr(provider,"now",lambda:dt.datetime(2026,11,4,10,0))
    monkeypatch.setattr(provider.sector_depth_service,"_records",lambda *a,**kw:[
        {"ts_code":"BK0002.DC","trade_date":"20261103","con_code":"600001.SH"}])
    with pytest.raises(ValueError,match="所选日"):provider.fetch("BK0001","2026-11-03")


def test_batch_advances_past_success_and_recent_failures_and_keeps_good_cache(monkeypatch):
    moment=dt.datetime(2026,11,4,10,0)
    monkeypatch.setattr(provider,"now",lambda:moment)
    called=[]
    def fetch(code,date):
        called.append(code)
        return {"date":date,"complete":True,"members":[{"code":"600001"}]}
    monkeypatch.setattr(provider,"fetch",fetch)
    previous={"BK0001":{"date":DAY,"complete":True,"members":[{"code":"600002"}]},
              "BK0002":{"error":"unavailable","attempted_at":moment.isoformat()}}
    rows=[{"code":f"BK000{i}","type":"行业","net":100-i} for i in range(1,5)]
    result=provider.collect({"rows":rows},previous,batch_size=1)
    assert called==["BK0003"] and result["BK0001"]==previous["BK0001"]
    assert "stocks" in service.FEED_FIELDS["sectors"]
