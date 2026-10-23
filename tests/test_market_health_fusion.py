"""跨市场口径：两所完整、金额单位、滞后降权、对冲持仓与量化方向。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from analysis.market_health import build_health
from analysis.market_health_fusion import build_fusion, freshness, margin_metrics, quant_eod_proxy


def baseline():
    s=json.loads((Path(__file__).resolve().parents[1]/"config/market_health_baseline.json").read_text())
    return s,build_health(s)


def cross_fixture(health, days):
    positions=[{"variety":v,"date":health["as_of"],"aggregate":{"summary":{
        "long_total":10000,"short_total":12000,"net":-2000,"net_chg":-100,"signal":"偏空"}}} for v in ("IF","IM")]
    leverage=[{"trade_date":d.replace("-",""),"exchange_id":e,"rzye":100000000000+i*100000000,
               "rzmre":1000000000,"rzche":1200000000,"rqye":1000000000,"rzrqye":101000000000+i*100000000}
              for i,d in enumerate(days[-8:]) for e in ("SSE","SZSE")]
    return {"futures":{"positions":positions},"leverage":{"records":leverage},
            "quant":{"gauge":{"date":health["as_of"],"bull":20,"bear":80},"snapshot_date":health["as_of"],
                     "stocks":[{"code":str(i),"direction":"砸盘" if i<80 else "拉锯","level_rank":3} for i in range(250)]},
            "capital":{"records":[{"trade_date":d,"net_amount":-1000000000,"net_amount_rate":-10} for d in days[-5:]]}}


def test_two_exchanges_must_be_complete_and_funds_are_yuan():
    rows=[{"trade_date":"20261021","exchange_id":"SSE","rzye":100,"rzmre":20,"rzche":30,"rqye":2,"rzrqye":102},
          {"trade_date":"20261021","exchange_id":"SZSE","rzye":200,"rzmre":40,"rzche":20,"rqye":3,"rzrqye":203},
          {"trade_date":"20261022","exchange_id":"SSE","rzye":120,"rzmre":20,"rzche":10,"rqye":2}]
    out=margin_metrics(rows)
    assert len(out)==1 and out[0]["date"]=="2026-10-21"
    assert out[0]["rzye"]==300 and out[0]["net_buy"]==10
    assert out[0]["rqye"]==5  # 元，不误乘万元或混加融券股数。


def test_disclosure_lag_uses_real_previous_trading_day():
    cal=["2026-10-16","2026-10-19","2026-10-20"]
    assert freshness("20261019","2026-10-20",cal,True)==("lagged",.5)
    assert freshness("2026-10-16","2026-10-20",cal,True)==("stale",0)
    assert freshness("2026-10-21","2026-10-20",cal,True)==("stale",0)
    assert freshness("2026-10-19","2026-10-20",cal,False)==("stale",0)


def test_technical_only_cannot_be_reported_as_complete_analysis():
    state,health=baseline()
    out=build_fusion(health,{},state["bars"]["sh000001"])
    assert out["pressure"] is None and out["complete"] is False
    assert out["cross_count"]==0 and len(out["missing"])==4
    assert "未齐" in out["title"]


def test_eod_proxy_is_dated_separate_and_downweighted():
    rows=[{"pct_chg":-3,"volume_ratio":2,"net_amount_rate":-10,"close":10,"low":10,"high":12} for _ in range(250)]
    proxy=quant_eod_proxy(rows)
    assert proxy["score"]==100 and proxy["sample"]==250
    assert quant_eod_proxy(rows[:199]) is None
    assert quant_eod_proxy([{**r,"volume_ratio":1} for r in rows]) is None
    state,health=baseline()
    cross={"quant":{"snapshot_date":health["as_of"],"proxy":proxy}}
    quant=next(d for d in build_fusion(health,cross,state["bars"]["sh000001"])["dimensions"] if d["key"]=="quant")
    assert quant["score"]==100 and quant["effective_weight"]==.05
    assert quant["details"]["bull"] is None and quant["details"]["bear"] is None
    assert "收盘行为代理" in quant["source"]
    cross["quant"]["snapshot_date"]="2026-10-21"
    assert next(d for d in build_fusion(health,cross,state["bars"]["sh000001"])["dimensions"] if d["key"]=="quant")["score"] is None


def test_futures_and_quant_cannot_replace_missing_margin_requirement():
    state,health=baseline();bars=state["bars"]["sh000001"]
    cross=cross_fixture(health,[b["date"] for b in bars]);cross.pop("leverage")
    out=build_fusion(health,cross,bars)
    assert out["pressure"] is None and out["complete"] is False
    assert "融资融券" in out["missing"] and out["sufficient"] is False


def test_seven_dimensions_generate_traceable_result_and_lag_discounts():
    state,health=baseline();bars=state["bars"]["sh000001"]
    days=[b["date"] for b in bars]
    cross=cross_fixture(health,days)
    out=build_fusion(health,cross,bars)
    assert out["complete"] is True and out["pressure"] is not None
    assert len(out["dimensions"])==7 and out["cross_count"]==4
    assert next(d for d in out["dimensions"] if d["key"]=="capital")["details"]["net5"]==-5000000000
    cross["leverage"]["records"]=[r for r in cross["leverage"]["records"] if r["trade_date"]!=health["as_of"].replace("-","")]
    lagged=next(d for d in build_fusion(health,cross,bars)["dimensions"] if d["key"]=="leverage")
    assert lagged["status"]=="lagged" and lagged["effective_weight"]==.05


def test_absolute_net_short_and_static_discount_do_not_imply_panic():
    state,health=baseline();bars=state["bars"]["sh000001"]
    cross=cross_fixture(health,[b["date"] for b in bars])
    for p in cross["futures"]["positions"]:
        p["aggregate"]["summary"].update(long_total=100,short_total=1000000,net=-999900,net_chg=0)
    cross["futures"]["quotes"]={"products":[{"variety":"IF","main":{"symbol":"IF2612","tradedate":health["as_of"],"verified_basis_pct":-8}}]}
    future=next(d for d in build_fusion(health,cross,bars)["dimensions"] if d["key"]=="futures")
    assert future["score"]==50


def test_quant_hft_activity_is_not_used_as_direction_and_old_day_is_excluded():
    state,health=baseline();bars=state["bars"]["sh000001"]
    cross=cross_fixture(health,[b["date"] for b in bars])
    for r in cross["quant"]["stocks"]:r["activity"]=100
    cross["quant"]["gauge"].update(bull=100,bear=0)
    for r in cross["quant"]["stocks"]:r["direction"]="拉抬"
    q=next(d for d in build_fusion(health,cross,bars)["dimensions"] if d["key"]=="quant")
    assert q["score"]==0
    cross["quant"]["fallback_date"]=bars[-2]["date"]
    q=next(d for d in build_fusion(health,cross,bars)["dimensions"] if d["key"]=="quant")
    assert q["score"] is None and q["status"]=="stale"


def test_contract_roll_does_not_create_basis_change():
    state,health=baseline();bars=state["bars"]["sh000001"]
    cross=cross_fixture(health,[b["date"] for b in bars])
    cross["futures"]["quotes"]={"products":[{"variety":"IF","main":{"symbol":"IF2611","tradedate":health["as_of"],"verified_basis_pct":-2}}]}
    cross["futures"]["previous_quotes"]={"products":[{"variety":"IF","main":{"symbol":"IF2610","tradedate":bars[-2]["date"],"verified_basis_pct":-.1}}]}
    f=next(d for d in build_fusion(health,cross,bars)["dimensions"] if d["key"]=="futures")
    assert f["details"]["basis"][0]["basis_change"] is None
