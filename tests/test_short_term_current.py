"""今日大屏不混用旧记录；盘口池日期、金额单位与实时资金独立发布。"""
import datetime as dt
from urllib.parse import parse_qs,urlsplit

import pytest

from analysis.short_term_screen import build_report
from webui.services import sector_depth_service as sectors, short_term_limits as limits


def test_today_view_excludes_old_prices_news_rankings_limits_and_orders():
    source={"radar":{"fallback_date":"2026-11-03","stocks":[{"code":"600001","name":"旧股票","industry":"行业甲","change_pct":8,"activity":80}]},
            "sectors":{"as_of":"2026-11-03","rows":[{"code":"BK1","name":"旧行业","net":1e12,"pct":8}]},
            "limits":{"as_of":"2026-11-03","events":[{"code":"600002","name":"旧涨停","kind":"U","date":"2026-11-03"}]},
            "popularity":{"fetched_at":"2026-11-03T15:00:00","rows":[{"code":"600003","name":"旧人气","rank":1}]},
            "orders":{"big_buys":[{"code":"600004","name":"旧大单","date":"2026-11-03"}]},
            "jinshi":[{"title":"昨日消息","time":"2026-11-03 10:00:00"},{"title":"无日期消息","time":"10:00"},{"title":"今日消息","time":"2026-11-04 10:00:00"}]}
    current=build_report(source,"2026-11-04","2026-11-04T10:01:00",current_only=True)
    assert not current["stocks"] and not current["sectors"] and not current["limits"] and not current["big_buys"]
    assert not current["hot_stocks"] and current["radar_as_of"] is None and current["limit_as_of"] is None
    assert [r["title"] for r in current["news"]]==["今日消息"]
    historical=build_report(source,"2026-11-03","2026-11-03T15:30:00")
    assert historical["stocks"] and historical["sectors"] and historical["limits"]


def test_today_live_quote_supersedes_old_close_without_hardcoded_board_priority():
    source={"sectors":{"as_of":"2026-11-03","rows":[
        {"code":"BK1","name":"行业甲","type":"行业","net":1e10,"pct":8,"live":{"code":"BK1","net":2e8,"pct":2,"quote_at":"2026-11-04T10:00:00+08:00"}},
        {"code":"BK2","name":"行业乙","type":"行业","net":2e10,"pct":9,"live":{"code":"BK2","net":3e8,"pct":3,"quote_at":"2026-11-04T10:00:00+08:00"}}]}}
    report=build_report(source,"2026-11-04","2026-11-04T10:01:00",current_only=True)
    assert len(report["sectors"])==2
    assert all(r["quote"]["net_at"].startswith("2026-11-04") for r in report["sectors"])
    assert {r["name"]:r["quote"]["net"] for r in report["sectors"]}=={"行业甲":2e8,"行业乙":3e8}
    assert {r["name"]:r["net"] for r in report["sectors"]}=={"行业甲":2e8,"行业乙":3e8}
    assert all(r["date"]=="2026-11-04" and not r["history"] for r in report["sectors"])
    assert all("2026-11-03" not in r["observation"] for r in report["sectors"])


def test_today_radar_never_relabels_old_price_or_cash_fields():
    rows=[{"code":"600001","industry":"行业甲","name":"旧字段","activity":80,"price":10,"change_pct":2,"main_net_inflow":100},
          {"code":"600002","industry":"行业甲","name":"今日字段","activity":70,"price":20,"change_pct":3,"main_net_inflow":200,"quote_at":"2026-11-04T10:00:00+08:00","flow_date":"2026-11-04"}]
    report=build_report({"radar":{"snapshot_date":"2026-11-04","stocks":rows}},"2026-11-04","2026-11-04T10:01:00",current_only=True)
    old,current=report["stocks"]
    assert old["price"] is None and old["change_pct"] is None and old["main_net_inflow"] is None
    assert current["price"]==20 and current["main_net_inflow"]==200
    assert report["sectors"][0]["quote"]["coverage"]["flow_count"]==1


def test_industry_quotes_survive_failed_concept_source_and_keep_actual_coverage(monkeypatch):
    moment=dt.datetime.fromisoformat("2026-11-04T10:00:00+08:00")
    monkeypatch.setattr(sectors,"now",lambda:moment)
    def fetch(url,**kwargs):
        query=parse_qs(urlsplit(url).query)
        if query["fs"]==["m:90+t:3"]:raise OSError("concept unavailable")
        return {"data":{"total":2,"diff":[{"f12":"BK1","f14":"行业甲","f62":1e8,"f3":2,"f124":moment.timestamp()},
                                             {"f12":"BK2","f14":"行业乙","f62":2e8,"f3":3,"f124":moment.timestamp()}]}}
    monkeypatch.setattr(sectors,"request_json",fetch)
    value=sectors.fetch_live()
    assert len(value["rows"])==2 and value["coverage"]["行业"]["complete"]
    assert not value["coverage"]["概念"]["complete"] and value["errors"]
    assert all(r["quote_at"].startswith("2026-11-04") for r in value["rows"])


def test_intraday_pool_validates_source_day_and_scales_prices_without_inventing_streak(monkeypatch):
    raw={"c":"688613","n":"示例","p":8720,"zdp":-20,"days":3,"oc":10,"fund":702561,"lbt":144059,"hybk":"医疗器械"}
    payload={"data":{"tc":1,"qdate":20261104,"pool":[raw]}}
    monkeypatch.setattr(limits,"request_json",lambda *args,**kwargs:payload)
    row=limits.pool("D","2026-11-04")[0]
    assert row["close"]==8.72 and row["provisional"] and row["open_times"]==10
    assert row["streak"] is None and row["last_time"]=="14:40:59"
    payload["data"]["qdate"]=20261103
    with pytest.raises(ValueError,match="今日交易日"):limits.pool("D","2026-11-04")


def test_intraday_collect_keeps_unknown_broken_count_unknown(monkeypatch):
    monkeypatch.setattr(limits,"now",lambda:dt.datetime(2026,11,4,10,0))
    monkeypatch.setattr(limits,"_market_stamp",lambda:("2026-11-04","10:00:00"))
    def pool(kind,date):
        if kind=="Z":raise OSError("unavailable")
        return [{"code":"600001" if kind=="U" else "600002","name":kind,"kind":kind,"date":date,"pct":10 if kind=="U" else -10}]
    monkeypatch.setattr(limits,"pool",pool)
    source=limits.collect()
    report=build_report({"limits":source},"2026-11-04","2026-11-04T10:00:00",current_only=True)
    assert report["limit_provisional"] and len(report["limit_up"])==len(report["limit_down"])==1
    assert report["summary"]["broken"] is None
