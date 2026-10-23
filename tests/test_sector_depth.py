"""板块深度研究的交易日完整性、资金单位与成分覆盖契约。"""
import datetime as dt

import pandas as pd
import pytest

from analysis.sector_depth import normalize, build_report, analyze_members
from webui.services import sector_depth_service as service


def sample():
    dates=[(dt.date(2026,9,1)+dt.timedelta(days=i)).isoformat() for i in range(40) if (dt.date(2026,9,1)+dt.timedelta(days=i)).weekday()<5][-22:]
    rows=[]
    for i,date in enumerate(dates):
        for code,kind,net,pct in (("BK0001","行业",100+i,2),("BK0002","行业",50+i,1),("BK0003","概念",10000,3)):
            rows.append({"trade_date":date.replace("-",""),"ts_code":code+".DC","content_type":kind,"name":code,
                         "net_amount":net,"net_amount_rate":2,"pct_change":pct,"close":100+i})
    return dates,rows


def test_ranks_compare_same_type_and_compound_relative_return():
    dates,rows=sample()
    report=build_report(normalize(rows),dates,{d:1 for d in dates})
    row=next(r for r in report["rows"] if r["code"]=="BK0001")
    assert row["rank"]==1 and row["universe"]==2
    assert row["flow5"]==sum(100+i for i in range(17,22))
    assert row["excess5"]==pytest.approx(((1.02/1.01)**5-1)*100)
    assert row["streak"]==22 and row["positive5"]==5
    assert row["phase"]=="持续共振"


def test_missing_trade_day_breaks_streak_and_does_not_sum_sparse_window():
    dates,rows=sample()
    rows=[r for r in rows if not (r["ts_code"]=="BK0001.DC" and r["trade_date"]==dates[-3].replace("-",""))]
    row=next(r for r in build_report(normalize(rows),dates,{d:0 for d in dates})["rows"] if r["code"]=="BK0001")
    assert row["streak"]==2 and row["flow3"] is None and row["flow20"] is None
    assert row["persistence"] is None and row["score"] is None


def test_duplicate_refresh_does_not_add_days_and_regions_are_excluded():
    dates,rows=sample()
    rows+=rows[-3:]+[{**rows[-1],"ts_code":"BK9999.DC","content_type":"地域"}]
    result=build_report(normalize(rows),dates,{d:0 for d in dates})
    assert len(result["rows"])==3 and all(r["days"]==22 for r in result["rows"])


def test_no_benchmark_or_missing_rate_does_not_create_confident_score():
    dates,raw=sample();rows=normalize(raw)
    rows[-3]["rate"]=None
    report=build_report(rows,dates,{})
    assert all(r["excess5"] is None and r["score"] is None for r in report["rows"])
    assert all(r["phase"]=="价格证据不足" for r in report["rows"])


def test_member_units_concentration_denominator_and_same_date_filter():
    members=[{"con_code":f"{i:06d}.SZ","name":str(i)} for i in range(4)]
    flows=[{"ts_code":m["con_code"],"trade_date":"20261023","net_amount":n,"pct_change":p} for m,n,p in zip(members,[10,5,-20,None],[2,1,-3,0])]
    flows.append({"ts_code":"000004.SZ","trade_date":"20261024","net_amount":1000})
    data=analyze_members(members,flows,"2026-10-23","2026-10-23")
    assert data["member_net"]==-50000 and data["covered"]==3
    assert data["coverage"]==75 and data["flow_breadth"]==pytest.approx(2/3*100)
    assert data["top3_share"]==100 and data["stocks"][0]["net"]==100000
    assert any("低于90%" in s for s in data["risks"])


def test_pagination_fetches_remaining_rows_and_detects_ignored_offsets(monkeypatch):
    class Pro:
        def rows(self,**kwargs):
            return pd.DataFrame([{"id":i} for i in range(kwargs["offset"],min(kwargs["offset"]+kwargs["limit"],5))])
    monkeypatch.setattr(service,"_pro",lambda:Pro())
    assert len(service._records("rows",cap=2))==5
    monkeypatch.setattr(Pro,"rows",lambda self,**kwargs:pd.DataFrame([{"id":1},{"id":2}]))
    with pytest.raises(ValueError,match="分页重复"):service._records("rows",cap=2)


def test_query_is_read_only_and_detail_rejects_non_board_codes(monkeypatch):
    monkeypatch.setattr(service,"_read",lambda key:None)
    monkeypatch.setattr(service,"_pro",lambda:pytest.fail("read-only query must not call provider"))
    value=service.query()
    assert len(value["rows"])>100 and all("history" not in r for r in value["rows"])
    with pytest.raises(ValueError,match="真实BK"):service.detail("../../token")


def test_preloaded_detail_preserves_actual_dates_and_member_coverage(monkeypatch):
    monkeypatch.setattr(service,"_read",lambda key:None)
    monkeypatch.setattr(service,"_pro",lambda:pytest.fail("verified baseline detail should be available offline"))
    report=service.baseline();code=next(iter(report["details"]))
    detail=service.detail(code)
    assert detail["as_of"]==detail["member_as_of"]==report["as_of"]
    assert detail["covered"]==detail["members"] and detail["coverage"]==100


def test_failed_latest_fetch_retains_cached_day_without_rolling_report_back(monkeypatch):
    dates,raw=sample();rows=normalize(raw);day=dates[-1]
    monkeypatch.setattr(service,"now",lambda:dt.datetime.fromisoformat(day+"T20:00:00+08:00"))
    def records(method,**kwargs):
        if method=="trade_cal":return [{"cal_date":d.replace("-",""),"is_open":1} for d in dates]
        return [{"trade_date":d.replace("-",""),"pct_chg":0} for d in dates]
    def daily(d):
        if d==day:raise OSError("offline")
        return [r for r in rows if r["date"]==d]
    monkeypatch.setattr(service,"_records",records)
    monkeypatch.setattr(service,"_daily",daily)
    monkeypatch.setattr(service,"_read",lambda key:[r for r in rows if r["date"]==day] if key==f"flow:{day}" else None)
    monkeypatch.setattr(service,"baseline",lambda:{})
    monkeypatch.setattr(service,"fetch_live",lambda:{"rows":[]})
    writes=[]
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:writes.append(value))
    value=service.collect()
    assert value["as_of"]==day and all(r["flow20"] is not None for r in value["rows"])
    assert value["errors"] and writes[-1]["as_of"]==day
