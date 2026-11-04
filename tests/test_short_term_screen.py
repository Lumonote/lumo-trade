"""短线大屏聚合、来源日期、关联证据、日程边界与后台任务回归。"""
import datetime as dt

import pytest

from analysis.short_term_screen import build_report, normalize_calendar, news_rows, announced_events, safe_url, themes, sector_quote, rank_sectors
from webui.services import short_term_screen_service as service
from webui.services.short_term_calendar import CONFERENCES


def inputs():
    return {
        "radar":{"fallback_date":"2026-10-30","stocks":[
            {"code":"600001","name":"芯片示例","industry":"半导体","activity":80,"direction":"拉抬","change_pct":10,"changes_total":12,"changes_bull":10,"changes_bear":2},
            {"code":"600002","name":"航天示例","industry":"航空装备","activity":50,"direction":"砸盘","change_pct":-5,"changes_total":7}],
            "sectors":[{"industry":"半导体","avg_activity":80}],"changes_agg":{"600001":{"events":[["09:31","rise",10,3]]}}},
        "sectors":{"as_of":"2026-10-29","rows":[{"code":"BK0001","name":"半导体","type":"行业","net":1e9,"pct":3,"history":[]}]},
        "limits":{"as_of":"2026-10-30","events":[{"code":"600001","name":"芯片示例","kind":"U","date":"2026-10-30","streak":3,"sectors":["BK0001"]}],
            "sectors":[{"code":"BK0001","name":"半导体","type":"行业"}]},
        "jinshi":[{"title":"芯片示例600001公布半导体新产品","time":"2026-11-01 09:15:00","url":"https://example.com/1"},
                   {"title":"旧日半导体信息","time":"2026-10-30 09:15:00"}],
        "ths_news":[{"title":"芯片示例600001公布半导体新产品","time":"09:15","url":"https://example.com/2"}],
        "sina_news":[{"title":"人工智能大会将于2026年11月3日开幕","time":"2026-11-01 10:00:00","url":"https://example.com/3"}],
        "calendar":CONFERENCES,
    }


def report():
    return build_report(inputs(),"2026-11-01","2026-11-01T12:00:00+08:00")


def test_sector_heat_ranking_uses_average_activity_not_member_or_news_counts():
    data=inputs()
    data["radar"]["stocks"] += [{"code":f"{600010+i}","name":f"芯片{i}","industry":"半导体","activity":0} for i in range(20)]
    value=build_report(data,"2026-11-01","2026-11-01T12:00:00+08:00")
    assert value["sectors"][0]["name"]=="航空装备"
    cold=next(row for row in value["sectors"] if row["name"]=="半导体")
    assert cold["trading_heat"]==80/21 and cold["heat_coverage"]==21
    assert value["graph"]["sector_ids"][0]==value["sectors"][0]["id"]
    assert value["sectors"][0]["heat_date"]=="2026-10-30"
    # 新同日活跃度上来后，不必等待成分数量变化就能升到榜首。
    for row in data["radar"]["stocks"]:
        if row["industry"]=="半导体":row["activity"]=90
    updated=build_report(data,"2026-11-01","2026-11-01T12:01:00+08:00")
    assert updated["sectors"][0]["name"]=="半导体"


def test_sector_flow_ranking_uses_display_quote_and_orders_missing_and_stale_last():
    def row(name,net,date="2026-11-01"):
        return {"id":name,"name":name,"net":1e12,"trading_heat":10,"heat_date":date,"quote":{"net":net,"net_at":date}}
    rows=[row("缺失",None),row("净流出",-20),row("旧日大额",1e10,"2026-10-30"),row("零净额",0),row("最新流入",50)]
    assert [r["name"] for r in rank_sectors(rows,"flow")]==["最新流入","零净额","净流出","旧日大额","缺失"]


def test_unmatched_sector_uses_same_day_covered_members_with_explicit_basis():
    data=inputs()
    data["radar"]["stocks"] += [{"code":"600003","name":"航空乙","industry":"航空装备","change_pct":3,"main_net_inflow":200},
                                {"code":"600004","name":"旧日航空","industry":"航空装备","trade_date":"2026-10-29","change_pct":90,"main_net_inflow":9000}]
    value=build_report(data,"2026-11-01","2026-11-01T12:00:00+08:00")
    sector=next(r for r in value["sectors"] if r["name"]=="航空装备")
    assert sector["pct"] is None and sector["net"] is None
    quote=sector["quote"]
    assert quote["pct"]==-1 and quote["net"]==200
    assert quote["pct_label"]=="成分均涨" and quote["net_label"]=="成分净额"
    assert quote["coverage"]=={"date":"2026-10-30","stocks":2,"pct_count":2,"flow_count":1}
    assert "非板块指数或全行业资金" in quote["note"]
    exact=next(r for r in value["sectors"] if r["name"]=="半导体")
    assert exact["quote"]["pct"]==3 and exact["quote"]["net"]==1e9


def test_sector_live_quote_requires_matching_code_and_today_and_keeps_close():
    row={"code":"BK0001","pct":3,"net":100,"date":"2026-10-30",
         "live":{"code":"BK0001","quote_at":"2026-11-01T10:30:00+08:00","pct":0,"net":-20}}
    quote=sector_quote(row,[],None,"2026-11-01")
    assert quote["pct"]==0 and quote["net"]==-20 and quote["net_at"].startswith("2026-11-01")
    assert row["pct"]==3 and row["net"]==100
    for live in ({**row["live"],"code":"BK0002"},{**row["live"],"quote_at":"2026-10-30T15:00:00"}):
        assert sector_quote({**row,"live":live},[],None,"2026-11-01")["net"]==100
    missing=sector_quote({"pct":None,"net":None},[{"date":"2026-10-30","change_pct":None,"main_net_inflow":None}],"2026-10-30","2026-11-01")
    assert missing["pct"] is None and missing["net"] is None
    undated=sector_quote({"pct":None,"net":None},[{"date":None,"change_pct":5,"main_net_inflow":50}],None,"2026-11-01")
    assert undated["pct"] is None and undated["net"] is None


def test_fresh_sectors_keeps_new_live_boards_and_closing_values(monkeypatch):
    monkeypatch.setattr(service.short_term_memberships,"collect",lambda sectors,previous:previous or {})
    monkeypatch.setattr(service,"_read",lambda key:None)
    monkeypatch.setattr(service.sector_depth_service,"query",lambda:{"as_of":"2026-10-30","rows":[{"code":"BK0001","name":"半导体","type":"行业","pct":3,"net":100}]})
    monkeypatch.setattr(service.tushare_client,"recent_trade_dates",lambda count:[])
    live=[{"code":"BK0001","name":"半导体","type":"行业","pct":2,"net":50,"quote_at":"2026-11-01T10:30:00+08:00"},
          {"code":"BK0002","name":"新板块","type":"概念","pct":1,"net":30,"quote_at":"2026-11-01T10:30:00+08:00"}]
    monkeypatch.setattr(service.sector_depth_service,"fetch_live",lambda:{"rows":live})
    value=service._fresh_sectors()
    assert value["as_of"]=="2026-10-30" and len(value["rows"])==2
    assert value["rows"][0]["net"]==100 and value["rows"][0]["live"]["net"]==50
    assert value["rows"][1]["code"]=="BK0002" and "net" not in value["rows"][1]


def test_popularity_rises_preserve_rank_changes_and_prioritize_current_rank():
    data=inputs()
    data["popularity"]={"fetched_at":"2026-11-01T12:00:00+08:00","rows":[
        {"code":"600002","name":"航天示例","rank":7,"rank_change":12,"rank_only":True,"change_pct":0},
        {"code":"600003","name":"热榜示例","rank":2,"rank_change":40,"rank_only":True,"latest_price":0,"change_pct":0},
        {"code":"600001","name":"芯片示例","rank":400,"rank_change":2000,"on_hot_list":False},
        {"code":"600004","rank":3,"rank_change":-2},{"code":"600005","rank":5},
        {"code":"600003","rank":9},{"code":"bad","rank":1}]}
    data["radar"]["changes_agg"]={"600001":{"events":[["09:31","big_buy",10,3],["09:32","rocket",10.5,5],["09:34","rebound",10.7,7]]},
                                     "600002":{"events":[["09:35","big_buy",8,1]]}}
    value=build_report(data,"2026-11-01","2026-11-01T12:00:00+08:00")
    assert [r["hot_rank"] for r in value["hot_stocks"]]==[2,3,5,7]
    assert value["hot_stocks"][0]["price"] is None and value["hot_stocks"][0]["change_pct"] is None
    assert [r["hot_rank"] for r in value["popularity_rises"]]==[2,7,400]
    assert value["popularity_rises"][0]["hot_rank_before"]==42
    assert value["popularity_rises"][0]["hot_rank_change"]==40
    assert value["popularity_rises"][0]["hot_rank_period"]=="较昨日"
    assert "rapid_rises" not in value


def test_popularity_reader_maps_provider_daily_change_and_keeps_current_list_authoritative(monkeypatch,tmp_path):
    from scripts.hot_stocks_fetcher import HotStocksFetcher
    from data_store import connection
    from webui.services import http_client
    monkeypatch.setattr(connection,"db_path",lambda:tmp_path/"test.sqlite")
    monkeypatch.setattr(HotStocksFetcher,"_get_stock_name",lambda self,code:code)
    def post(url,*args,**kwargs):
        if url.endswith("getAllCurrentList"):
            return {"code":0,"data":[{"sc":"SH600001","rk":5,"hisRc":20},{"sc":"SZ000001","rk":2,"hisRc":None}]}
        return {"code":0,"data":[{"sc":"SH600001","rk":6,"hrc":21},{"sc":"SH600003","rk":150,"hrc":600}]}
    monkeypatch.setattr(http_client,"request_json_post",post)
    rows={r["code"]:r for r in service._fresh_popularity()["rows"]}
    assert rows["600001"]["rank"]==5 and rows["600001"]["rank_change"]==20 and rows["600001"]["on_hot_list"]
    assert rows["600003"]["rank_change"]==600 and not rows["600003"]["on_hot_list"]
    assert rows["000001"]["rank_change"] is None


def test_limit_panels_only_include_verified_closing_events_of_the_limit_date():
    data=inputs()
    data["limits"]["events"] += [
        {"code":"600002","name":"航天示例","kind":"D","date":"2026-10-30","streak":2},
        {"code":"600003","name":"旧日涨停","kind":"U","date":"2026-10-29","streak":9},
        {"code":"600004","name":"炸板示例","kind":"Z","date":"2026-10-30","streak":1}]
    value=build_report(data,"2026-11-01","2026-11-01T12:00:00+08:00")
    assert [r["code"] for r in value["limit_up"]]==["600001"]
    assert [r["code"] for r in value["limit_down"]]==["600002"]
    assert value["limit_as_of"]=="2026-10-30" and value["today"]=="2026-11-01"


def test_aggregation_links_keep_original_dates_and_separate_news_from_price_causes():
    value=report()
    assert value["sector_as_of"]=="2026-10-29" and value["radar_as_of"]==value["limit_as_of"]=="2026-10-30"
    sector=next(r for r in value["sectors"] if r["code"]=="BK0001")
    assert sector["stock_codes"]==["600001"]
    assert sector["signals"]["拉抬"]==1 and sector["signals"]["涨停"]==1
    assert "不等于因果" in value["method"] or "不推断" in value["method"]
    assert all("evidence" in edge and edge["kind"]=="membership" for edge in value["graph"]["edges"])
    stock=next(r for r in value["stocks"] if r["code"]=="600001")
    assert len(stock["news_ids"])==1 and len(stock["timeline"])==1


def test_news_dedup_preserves_sources_and_date_unknown_is_not_today():
    rows=news_rows({"jinshi":[{"title":"同一条消息","time":"12:20"}],
                    "ths_news":[{"title":"同一条消息","time":"2026-11-01 12:20:00","url":"https://example.com/a"}]},"2026-11-01")
    assert len(rows)==1 and rows[0]["is_today"] and len(rows[0]["sources"])==2
    missing=news_rows({"jinshi":[{"title":"只有时分","time":"12:20"}]},"2026-11-01")[0]
    assert not missing["is_today"] and missing["date"]=="" and "未归入" in missing["time_basis"]


def test_core_news_never_uses_stale_or_undated_messages_and_retains_all_feed_rows():
    value=report()
    assert len(value["news"])==3 and len(value["core_news"])==0
    assert all(r["date"]=="2026-11-01" for r in value["core_news"])
    assert any(r["date"]=="2026-10-30" for r in value["news"])


def test_core_and_short_news_are_disjoint_and_major_events_explain_selection():
    data=inputs()
    data["jinshi"] += [
        {"title":"工信部发布半导体产业支持政策","time":"2026-11-01 10:30:00"},
        {"title":"芯片示例600001公告中标5亿元合同","time":"2026-11-01 11:00:00"},
        {"title":"分析师预计半导体或将迎来重大突破","time":"2026-11-01 11:10:00"},
        {"title":"日本食品涨价预计增加","time":"2026-11-01 11:20:00"}]
    value=build_report(data,"2026-11-01","2026-11-01T12:00:00+08:00")
    core_ids={r["id"] for r in value["core_news"]}
    assert len(core_ids)==2 and not core_ids.intersection(r["id"] for r in value["short_news"])
    assert len(value["core_news"])+len(value["short_news"])==len(value["news"])
    assert all(r["core_reasons"] and r["watch_points"] for r in value["core_news"])


def test_future_calendar_removes_expired_events_keeps_ongoing_and_checks_evidence_url():
    rows=normalize_calendar([
        {"date":"2026-10-29","end_date":"2026-11-02","title":"进行中","verified":True,"url":"https://example.com/a"},
        {"date":"2026-10-30","title":"已结束"},
        {"date":"2026-11-02","title":"伪链接","verified":True,"url":"javascript:alert(1)"},
        {"date":"2027-01-01","title":"超出45日"},
        {"date":"2026-02-30","title":"非法日"}],"2026-11-01")
    assert {r["title"] for r in rows}=={"进行中","伪链接"}
    assert rows[0]["state"]=="进行中" and rows[0]["days_until"]==0
    assert next(r for r in rows if r["title"]=="伪链接")["verified"] is False


def test_media_future_meeting_requires_unambiguous_year_or_dated_publication_and_url():
    rows=news_rows({"jinshi":[
        {"title":"人工智能大会将于11月3日举办","time":"2026-11-01 09:00","url":"https://example.com/a"},
        {"title":"机器人大会将于11月8日举办","time":"09:00","url":"https://example.com/b"},
        {"title":"航天峰会2026年11月10日举办","time":"09:00"}]},"2026-11-01")
    events=announced_events(rows,"2026-11-01")
    assert len(events)==1 and events[0]["date"]=="2026-11-03" and not events[0]["verified"]


def test_unsafe_urls_and_english_substrings_are_not_treated_as_ai():
    for url in ("javascript:alert(1)","data:text/html,x","https://username:secret@example.com/", "//example.com/x"):
        assert safe_url(url)==""
    assert "人工智能" not in themes("chair and mail")
    assert "人工智能" in themes("AI算力")


def test_calendar_baseline_expiry_does_not_create_fake_recurring_events():
    value=normalize_calendar(CONFERENCES,"2027-01-01")
    assert value==[]


def test_provider_calendar_accepts_iso_and_compact_dates(monkeypatch):
    import pandas as pd
    from webui.services import short_term_calendar as calendar
    class Pro:
        def eco_cal(self,**params):
            return pd.DataFrame([{"date":"2026-11-02","event":"经济数据甲"},{"date":"20261103","event":"经济数据乙"}])
    monkeypatch.setattr(calendar.tushare_client,"get_pro",lambda:Pro())
    monkeypatch.setattr(calendar,"now",lambda:dt.datetime.fromisoformat("2026-11-01T12:00:00+08:00"))
    rows=calendar.calendar_rows()
    assert len(rows)==4 and {r["date"] for r in rows}=={"2026-11-02","2026-11-03"}


def test_calendar_splits_full_windows_and_deduplicates_provider_rows(monkeypatch):
    import pandas as pd
    from webui.services import short_term_calendar as calendar
    calls=[]
    class Pro:
        def eco_cal(self,**params):
            calls.append(params)
            first,last=params["start_date"],params["end_date"]
            row={"date":first,"event":"数据发布","time":"09:30","country":params["country"]}
            return pd.DataFrame([row]*100 if first<last else [row,row])
    monkeypatch.setattr(calendar.tushare_client,"get_pro",lambda:Pro())
    monkeypatch.setattr(calendar,"now",lambda:dt.datetime.fromisoformat("2026-11-01T12:00:00+08:00"))
    rows=calendar.calendar_rows()
    assert len(rows)==92
    assert all("offset" not in r for r in calls)


def test_sector_refresh_updates_old_baseline_without_relabeling_old_window_scores(monkeypatch):
    monkeypatch.setattr(service,"_read",lambda key:None)
    monkeypatch.setattr(service.sector_depth_service,"query",lambda:{"as_of":"2026-10-23","rows":[{"code":"BK0001","flow5":123}]})
    monkeypatch.setattr(service.tushare_client,"recent_trade_dates",lambda n:["20261030"])
    monkeypatch.setattr(service.sector_depth_service,"_daily",lambda date:[{"code":"BK0001","date":date,"name":"行业甲","type":"行业","net":5,"pct":2}])
    monkeypatch.setattr(service.sector_depth_service,"fetch_live",lambda:{"rows":[]})
    monkeypatch.setattr(service,"now",lambda:dt.datetime.fromisoformat("2026-10-30T18:00:00+08:00"))
    result=service._fresh_sectors()
    assert result["as_of"]=="2026-10-30" and result["rows"][0]["date"]=="2026-10-30"
    assert "flow5" not in result["rows"][0]


@pytest.fixture
def cache(monkeypatch):
    values={}
    monkeypatch.setattr(service,"_read",values.get)
    monkeypatch.setattr(service.kv_repo,"set_",lambda ns,key,value:values.__setitem__(key,value))
    monkeypatch.setattr(service,"_local_sources",lambda:values.get("inputs") or inputs())
    monkeypatch.setattr(service,"now",lambda:dt.datetime.fromisoformat("2026-11-01T12:00:00+08:00"))
    return values


def test_read_only_query_supports_all_sections_and_detail_without_network(cache,monkeypatch):
    source=inputs();source["radar"]["fallback_date"]="2026-11-01";source["sectors"]["as_of"]="2026-11-01";source["limits"]["as_of"]="2026-11-01"
    for row in source["limits"]["events"]:row["date"]="2026-11-01"
    cache["inputs"]=source
    monkeypatch.setattr(service,"_fresh_radar",lambda:pytest.fail("query must not fetch"))
    value=service.query()
    assert len(value["stocks"])==2
    section=service.section("stocks",keyword="600001")
    assert section["total"]==1 and section["rows"][0]["code"]=="600001"
    detail=service.detail("stock","600001")
    assert len(detail["sectors"])==1 and len(detail["news"])==1 and len(detail["limits"])==1
    for kind in ("sectors","stocks","news","core_news","calendar","limits"):
        assert service.section(kind)["kind"]==kind


def test_invalid_or_missing_historical_snapshot_never_relabels_current_data(cache):
    missing=service.query(date="2026-10-01")
    assert not missing["available"] and missing["stocks"]==[] and missing["sectors"]==[]
    for date in ("bad","2026-11-02"):
        with pytest.raises(ValueError):service.query(date=date)
    with pytest.raises(ValueError):service.section("unknown")
    with pytest.raises(ValueError):service.detail("stock","../../token")


def test_background_refresh_preserves_failed_source_and_writes_replay_snapshot(cache):
    def failed():raise OSError("offline")
    value=service.collect(None,readers={"jinshi":failed,"calendar":lambda:CONFERENCES,"radar":lambda:inputs()["radar"]})
    assert value["news"] and cache["inputs"]["jinshi"]==inputs()["jinshi"]
    source=next(r for r in value["sources"] if r["key"]=="jinshi")
    assert source["state"]=="保留缓存" and source["fetched_at"] is None
    assert service.query(date="2026-11-01")["historical"]


def test_background_job_is_deduplicated(cache):
    class Jobs:
        job=None
        def get(self,key):return self.job
        def start(self,kind,params,worker):self.job={"id":"short-1","type":kind,"status":"running"};return self.job
    jobs=Jobs()
    assert service.start_refresh(jobs,None)["type"]=="short_term_screen"
    assert service.start_refresh(jobs,None)["id"]=="short-1"
