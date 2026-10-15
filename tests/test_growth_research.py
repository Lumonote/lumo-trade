"""成长统计的财务口径、缺数据、复权证据、分页与完整导出回归。"""
import copy
import csv
import datetime as dt
import io
import math

import pytest

from analysis.growth_research import build_snapshot, code6, technical_position
from analysis.growth_research_provider import completed_quarters, _download
from webui.services import growth_research_service as service

AS_OF="2026-10-14"


def history(price=10):
    start=dt.date.fromisoformat(AS_OF)-dt.timedelta(days=139)
    closes=[30-i*20/139 for i in range(140)]
    return [{"date":(start+dt.timedelta(days=i)).isoformat(),"close":v,"pct_change":(v/closes[i-1]-1)*100 if i else 0} for i,v in enumerate(closes)]


@pytest.fixture
def inputs():
    codes=["600001","688001","300001","000001","002001","600002"]
    market=[{"ts_code":c,"name":"ST样本" if i==4 else "研究样本"+str(i),"close":10} for i,c in enumerate(codes)]
    actual={c:{"PUBLISHNAME":"食品加工" if i==5 else "半导体","PARENT_NETPROFIT":1e8,"TOTAL_OPERATE_INCOME":1e9,"YSTZ":30,"SJLTZ":100,"DEDUCT_BASIC_EPS":.5,"BASIC_EPS":.55,"MGJYXJJE":1,"NOTICE_DATE":"2026-08-20"} for i,c in enumerate(codes)}
    prior={c:{"PARENT_NETPROFIT":5e7} for c in codes}
    forecasts={c:{"YEAR1":2025,"YEAR_MARK1":"A","EPS1":.8,"YEAR2":2026,"YEAR_MARK2":"E","EPS2":1,"YEAR3":2027,"YEAR_MARK3":"E","EPS3":1.5,"YEAR4":2028,"YEAR_MARK4":"E","EPS4":2,"RATING_ORG_NUM":5} for c in codes}
    financials={"reports":{"2026-06-30":actual,"2025-06-30":prior},"forecasts":forecasts}
    return market,{c:history() for c in codes},financials


def snapshot(inputs):
    market,hist,financials=inputs
    return build_snapshot(market,hist,financials,{}, {},AS_OF,"2026-10-15T10:00:00+08:00")


def test_positive_base_growth_and_industry_buckets(inputs):
    result=snapshot(inputs);rows={r["code"]:r for r in result["stocks"]}
    assert rows["600001"]["group"]=="A"
    assert rows["600002"]["group"]=="E"
    assert rows["002001"]["risk_warning"] and rows["002001"]["group"]=="other"
    assert rows["600001"]["cagr"]==pytest.approx(math.sqrt(2)-1)
    assert sum(result["summary"]["groups"].values())==result["summary"]["total"]==6


def test_negative_profit_base_cannot_be_actual_high_growth(inputs):
    inputs[2]["reports"]["2025-06-30"]["600001"]["PARENT_NETPROFIT"]=-1e7
    row=next(r for r in snapshot(inputs)["stocks"] if r["code"]=="600001")
    assert row["actual_turnaround"] and not row["high_actual"]
    assert row["group"]=="C"


def test_forecast_turnaround_has_no_cagr_or_pe(inputs):
    f=inputs[2]["forecasts"]["600001"];f["EPS2"]=-.2
    inputs[2]["reports"]["2026-06-30"]["600001"]["SJLTZ"]=10
    row=next(r for r in snapshot(inputs)["stocks"] if r["code"]=="600001")
    assert row["future_turnaround"] and row["group"]=="G"
    assert row["cagr"] is None and row["pe"] is None


def test_early_report_does_not_replace_broad_period(inputs):
    inputs[2]["reports"]["2026-09-30"]={"600001":{"PARENT_NETPROFIT":2e8}}
    assert snapshot(inputs)["report_date"]=="2026-06-30"


def test_missing_forecasts_and_prior_profit_remain_unavailable(inputs):
    inputs[2]["forecasts"]={}
    inputs[2]["reports"].pop("2025-06-30")
    result=snapshot(inputs)
    assert result["summary"]["future_high"]==0
    assert result["summary"]["actual_high"]==0
    assert all(r["cagr"] is None for r in result["stocks"])
    assert any("覆盖不足" in note for note in result["notes"])


def test_future_year_selection_ignores_previous_year_estimate(inputs):
    for row in inputs[2]["forecasts"].values():row["YEAR_MARK1"]="E"
    assert snapshot(inputs)["forecast_years"]==[2026,2027,2028]


def test_adjustment_chain_removes_split_false_crash():
    rows=history()
    for row in rows:row["close"]=20;row["pct_change"]=0
    for row in rows[-20:]:row["close"]=10
    result=technical_position(rows,10,AS_OF)
    assert result["high120"]==pytest.approx(10)
    assert not result["bottom"]


def test_stale_or_mismatching_qfq_cannot_be_verified():
    rows=history();bars=[{**r,"high":r["close"]+1,"low":r["close"]-1} for r in rows]
    assert technical_position(rows,10,AS_OF,bars)["technical_basis"]=="qfq"
    assert technical_position(rows,10,AS_OF,bars[:-1])["technical_basis"]=="close"
    bars[-1]["close"]=20
    assert technical_position(rows,10,AS_OF,bars)["technical_basis"]=="close"


def test_missing_history_cannot_match_position():
    result=technical_position(history()[:30],10,AS_OF)
    assert not result["near_position"] and result["support"] is None


def test_flat_prices_are_not_rising_ma_support():
    rows=[{**row,"close":10,"pct_change":0} for row in history()]
    assert not technical_position(rows,10,AS_OF)["near_position"]


def test_invalid_ohlc_does_not_upgrade_technical_evidence():
    bars=[{**row,"low":0,"high":row["close"]+1} for row in history()]
    assert technical_position(history(),10,AS_OF,bars)["technical_basis"]=="close"


def test_search_numeric_filters_and_pagination(inputs):
    data=snapshot(inputs)
    result=service.query_snapshot(data,{"pool":"all","search":"000001","cagr_min":30})
    assert [r["code"] for r in result["stocks"]]==["000001"]
    assert service.query_snapshot(data,{"pool":"all","pe_max":5})["total_matched"]==0
    result=service.query_snapshot(data,{"pool":"all","page":999})
    assert result["page"]==1 and len(result["stocks"])==5
    assert service.query_snapshot(data,{"position":"bottom"})["total_matched"]>0
    assert service.query_snapshot(data,{"position":"support"})["total_matched"]==0


@pytest.mark.parametrize("params",[{"sort":"price; DROP"},{"pool":"invalid"},{"basis":"unknown"},{"position":"unknown"},{"cagr_min":"nan"},{"page":"oops"}])
def test_invalid_queries_rejected(inputs,params):
    with pytest.raises(ValueError):service.query_snapshot(snapshot(inputs),params)


def test_sort_missing_values_last_in_both_directions(inputs):
    data=snapshot(inputs);data["stocks"][0]["cagr"]=None
    for direction in ("asc","desc"):
        result=service.query_snapshot(data,{"pool":"all","sort":"cagr","direction":direction,"exclude_st":0})
        assert result["stocks"][-1]["cagr"] is None


def test_csv_exports_all_pages_and_neutralizes_formulas(inputs,monkeypatch):
    data=snapshot(inputs);template=data["stocks"][0]
    data["stocks"]=[{**copy.deepcopy(template),"code":f"{i:06d}","name":"=danger"} for i in range(215)]
    monkeypatch.setattr(service.repo,"read",lambda key:data)
    rows=list(csv.reader(io.StringIO(service.export_csv({"pool":"all","page":3}))))
    assert len(rows)==216
    assert rows[1][0]=="000000" and rows[-1][0]=="000214"
    assert rows[1][1]=="'=danger"


def test_quarters_are_dynamic_and_never_future():
    dates=completed_quarters(dt.date(2027,1,15))
    assert dates[0]=="2026-12-31" and "2027-03-31" not in dates
    assert code6("sz000001")=="000001" and code6("920001.BJ")=="920001"
    assert code6("900001.SH")==""


def test_incomplete_provider_pages_never_return_partial_snapshot():
    class Session:
        def get(self,*args,**kwargs):return self
        def raise_for_status(self):pass
        def json(self):return {"result":{"count":2,"pages":1,"data":[{}]}}
    with pytest.raises(ValueError,match="分页不完整"):_download({},session=Session())


def test_partial_financial_refresh_preserves_forecast_timestamp():
    previous={"reports":{"2025-06-30":{"600001":{}}},"forecasts":{"600001":{"EPS2":1}},"updated_at":"2026-10-01"}
    incoming={"reports":{"2026-06-30":{"600001":{}}},"forecasts":{},"errors":["预测暂不可用"]}
    merged=service.merge_financials(previous,incoming,"2026-10-15")
    assert merged["forecasts"]==previous["forecasts"]
    assert merged["forecasts_updated_at"]=="2026-10-01"
    assert merged["report_updated_at"]["2026-06-30"]=="2026-10-15"
    assert "2025-06-30" in merged["reports"]
    assert previous["updated_at"]=="2026-10-01"


def test_total_refresh_failure_does_not_replace_cache():
    with pytest.raises(ValueError,match="已有快照保留"):
        service.merge_financials({"forecasts":{"600001":{}}},{"reports":{},"forecasts":{},"errors":["离线"]},"2026-10-15")


def test_snapshot_exposes_separate_source_times(inputs):
    inputs[2].update(report_updated_at={"2026-06-30":"2026-10-15"},forecasts_updated_at="2026-10-01")
    result=snapshot(inputs)
    assert result["financial_updated_at"]=="2026-10-15"
    assert result["forecasts_updated_at"]=="2026-10-01"


def test_sector_export_matches_industry_view(inputs,monkeypatch):
    data=snapshot(inputs)
    monkeypatch.setattr(service.repo,"read",lambda key:data)
    rows=list(csv.reader(io.StringIO(service.export_csv({"view":"sectors","industry":"半导体"}))))
    assert len(rows)==2 and rows[1][0]=="半导体"
    assert float(rows[1][6])==pytest.approx((math.sqrt(2)-1)*100)
    with pytest.raises(ValueError):service.export_csv({"view":"invalid"})


def test_refresh_uses_desktop_finished_status(monkeypatch):
    cache={}
    class Jobs:
        def __init__(self):self.job={"id":"growth-job","status":"queued"}
        def get(self,key):return self.job
        def update(self,key,**values):self.job.update(values)
        def append_log(self,*args):pass
        def start(self,kind,params,worker):
            assert kind=="growth_research"
            worker(self.job["id"],params)
            return self.job
    monkeypatch.setattr(service.repo,"read",lambda key:cache.get(key))
    monkeypatch.setattr(service.repo,"write",lambda key,value:cache.update({key:value}))
    monkeypatch.setattr(service,"rebuild",lambda:{"summary":{"total":5564},"as_of":AS_OF})
    result=service.start_refresh("rebuild",Jobs())
    assert result["status"]=="finished" and result["progress"]==100
    assert cache["latest_job"]=="growth-job"
