"""实际限价、跨区间连续性、形态复用与后台缓存的离线回归。"""
import datetime as dt

import pytest

from analysis.market_limits import build_report, normalize_day, pattern_tags, PATTERNS
from webui.services import market_limits_service as service
from webui.services.stock_screener_service import PRESET_KEYS, preset_pass, tech_features


def day(date, kind="U", code="600001.SH"):
    close = 11 if kind == "U" else 9 if kind == "D" else 10
    quotes = [{"ts_code": code, "trade_date": date.replace("-", ""), "close": close,
               "pre_close": 10, "high": 11 if kind in ("U", "Z") else close,
               "low": 9 if kind in ("D", "O") else close, "open": close, "amount": 1500, "pct_chg": (close-10)*10}]
    limits = [{"ts_code": code, "trade_date": date.replace("-", ""), "up_limit": 11, "down_limit": 9, "pre_close": 10}]
    return normalize_day(date, quotes, limits)


def test_actual_limits_include_st_and_beijing_and_exclude_b_shares_and_funds():
    quotes, prices = [], []
    for code, up, close in (("000001.SZ", 10.5, 10.5), ("920001.BJ", 13, 13), ("300001.SZ", 12, 11),
                            ("900001.SH", 11, 11), ("510300.SH", 11, 11)):
        quotes.append({"ts_code": code, "trade_date": "20261030", "pre_close": 10, "close": close, "high": close, "low": close, "amount": 100})
        prices.append({"ts_code": code, "trade_date": "20261030", "pre_close": 10, "up_limit": up, "down_limit": 9})
    value = normalize_day("2026-10-30", quotes, prices, basics={"000001.SZ": {"name": "ST示例"}})
    assert {r["code"] for r in value["events"]} == {"000001", "920001"}
    assert value["events"][0]["amount"] == 100000
    assert value["events"][0]["name"] == "ST示例"
    assert value["covered"] == 3


def test_numeric_seal_times_preserve_leading_zero_and_missing_values():
    from analysis.market_limits import _seal_time
    assert _seal_time(93224.0) == "09:32:24"
    assert _seal_time("093000") == "09:30:00"
    assert _seal_time("14:55:00") == "14:55:00"
    assert _seal_time(0) == "" and _seal_time(None) == ""


def test_touch_events_round_to_cents_and_do_not_count_as_sealed_boards():
    a, b = day("2026-10-30", "Z"), day("2026-10-30", "O", "000001.SZ")
    assert a["events"][0]["kind"] == "Z" and b["events"][0]["kind"] == "O"
    quote = {"ts_code":"600001.SH","trade_date":"20261030","close":10.999999999,"high":11,"low":11,"pre_close":10}
    result = normalize_day("2026-10-30", [quote], [{"ts_code":"600001.SH","trade_date":"20261030","up_limit":11,"down_limit":9}])
    assert result["events"][0]["kind"] == "U" and result["events"][0]["one_price"]
    assert normalize_day("2026-10-29", [quote], []) == {"date":"2026-10-29","events":[],"covered":0,"unverified":0,"unverified_codes":[],"moves":{}}
    missing = normalize_day("2026-10-30", [quote], [])
    assert missing["unverified"] == 1 and not missing["events"]
    # 实际价格超出所报限价时可能是新股无涨跌幅限制日，不能误记炸板。
    no_limit = normalize_day("2026-10-30", [{**quote,"high":15,"close":14}],
                             [{"ts_code":"600001.SH","trade_date":"20261030","pre_close":10,"up_limit":11,"down_limit":9}])
    assert no_limit["unverified"] == 1 and not no_limit["events"]


def test_board_catalog_partitions_long_intervals_before_provider_row_cap(cache, monkeypatch):
    calls=[]
    monkeypatch.setattr(service,"_records",lambda method,**params:calls.append(params) or [])
    assert service._board_catalog("2026-08-25","2026-10-02") == {}
    assert len(calls)==6
    assert [(r["start_date"],r["end_date"]) for r in calls[::2]] == [
        ("20260825","20260831"),("20260901","20260930"),("20261001","20261002")]


def test_streak_crosses_weekend_and_query_boundary_but_not_normal_day():
    dates = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]
    days = [day(d, "U" if i in (1,2,4) else "") for i,d in enumerate(dates)]
    value = build_report(days, dates, dates[2], dates[-1])
    records = {r["date"]:r for r in value["events"]}
    assert records[dates[2]]["streak"] == 2 and not records[dates[2]]["streak_lower_bound"]
    assert records[dates[4]]["streak"] == 1
    assert value["summary"]["U"] == 2


def test_missing_day_is_unknown_and_duplicate_events_do_not_add_boards():
    dates = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
    days = [day(d, "D") for d in (dates[0], dates[2], dates[3])]
    days[-1]["events"] *= 2
    value = build_report(days, dates, dates[0], dates[-1])
    assert value["missing_dates"] == [dates[1]]
    assert value["daily"][1]["D"] is None
    last = value["events"][0]
    assert last["streak"] == 2 and last["streak_lower_bound"]
    assert last["fall_days"] == 2 and last["fall_lower_bound"]
    assert value["summary"]["D"] == 3 and value["summary"]["down_stocks"] == 1


def test_one_unknown_stock_limit_marks_next_streak_as_lower_bound():
    a, b = day("2026-10-29", ""), day("2026-10-30", "D")
    a["unverified_codes"] = ["600001"]
    value = build_report([a,b], [a["date"],b["date"]], a["date"], b["date"])
    assert value["events"][0]["streak_lower_bound"]


def test_all_board_memberships_use_event_date_and_counts_deduplicate_stocks():
    dates = ["2026-10-29", "2026-10-30"]
    days = [day(d) for d in dates]
    boards = {"BK0001":{"name":"行业甲","type":"行业"},"BK0002":{"name":"概念乙","type":"概念"}}
    mappings = {f"{dates[0]}:600001":[{"code":"BK0001"},{"code":"BK0001"}],
                f"{dates[1]}:600001":[{"code":"BK0001"},{"code":"BK0002"}]}
    value = build_report(days, dates, dates[0], dates[1], mappings, boards=boards)
    assert value["summary"]["U"] == 2 and len(value["stocks"]) == 1
    assert {r["code"]:r["U"] for r in value["sectors"]} == {"BK0001":2,"BK0002":1}
    assert value["stocks"][0]["sectors"] == ["BK0001","BK0002"]
    assert value["unmapped_events"] == 0


def test_tags_reuse_every_screener_preset_and_never_read_future_bars():
    bars = [{"date": (dt.date(2026,5,1)+dt.timedelta(days=i)).isoformat(),"close":10+i/10,
             "open":10+i/10-.05,"high":10+i/10+.1,"low":10+i/10-.1,"volume":100+i,"amount":1e8} for i in range(121)]
    cutoff = bars[-2]["date"]
    feat = tech_features(bars[:-1], "600001")
    expected = {key for key in PRESET_KEYS if preset_pass(feat,{"labels":[key],"mode":"any"})[0]}
    value = pattern_tags(bars,"600001",cutoff)
    assert set(PATTERNS) == set(PRESET_KEYS)
    assert {t["key"] for t in value["tags"]} == expected
    assert value["as_of"] == cutoff
    assert pattern_tags([],"600001",cutoff)["status"] == "日K不足21根"


@pytest.fixture
def cache(monkeypatch):
    values = {}
    monkeypatch.setattr(service, "_read", values.get)
    monkeypatch.setattr(service.kv_repo, "set_", lambda ns,key,value: values.__setitem__(key,value))
    monkeypatch.setattr(service, "now", lambda: dt.datetime.fromisoformat("2026-11-01T12:00:00+08:00"))
    return values


def test_read_only_empty_query_validates_ranges_without_calling_provider(cache, monkeypatch):
    monkeypatch.setattr(service,"_records",lambda *a,**kw:pytest.fail("query must be read-only"))
    value = service.query(start="2026-10-01",end="2026-10-30")
    assert not value["available"] and value["events"] == []
    for start,end in (("bad",""),("2026-10-30","2026-10-01"),("2025-01-01","2026-10-01"),("","2026-12-01")):
        with pytest.raises(ValueError): service.query(start=start,end=end)
    with pytest.raises(ValueError): service.detail("stock","../../token")


def test_collect_joins_prices_members_and_tags_and_keeps_cache_on_failure(cache, monkeypatch):
    dates = ["2026-10-28","2026-10-29","2026-10-30"]
    def records(method, **params):
        if method == "trade_cal": return [{"cal_date":d.replace("-",""),"is_open":1} for d in dates]
        if method == "stock_basic": return [{"ts_code":"600001.SH","name":"示例股"}]
        if method == "daily": return [{"ts_code":"600001.SH","trade_date":params.get("trade_date","20261030"),"pre_close":10,"open":11,"high":11,"low":11,"close":11,"pct_chg":10,"vol":100,"amount":1000}]
        if method == "stk_limit": return [{"ts_code":"600001.SH","trade_date":params["trade_date"],"pre_close":10,"up_limit":11,"down_limit":9}]
        if method == "dc_index": return [{"ts_code":"BK0001.DC","trade_date":"20261030","name":"示例板块"}] if params["idx_type"]=="行业板块" else []
        if method == "dc_member": return [{"con_code":"600001.SH","ts_code":"BK0001.DC","trade_date":d.replace("-","")} for d in dates]
        if method == "adj_factor": return [{"trade_date":"20261030","adj_factor":1}]
        return []
    monkeypatch.setattr(service,"_records",records)
    value = service.collect(dates[1],dates[2])
    assert value["summary"]["U"] == 2 and value["events"][0]["streak"] == 3
    assert value["complete"] and len(value["sectors"]) == 1
    assert service.query()["available"]
    detail = service.detail("sector","BK0001")
    assert len(detail["events"]) == 2 and len(detail["stocks"]) == 1
    monkeypatch.setattr(service,"_records",lambda *a,**kw:[])
    with pytest.raises(ValueError): service.collect(dates[1],dates[2])
    assert service.query()["as_of"] == dates[-1]


def test_adjustment_factors_prevent_corporate_action_from_changing_tags(cache, monkeypatch):
    bars=[{"trade_date":(dt.date(2026,9,1)+dt.timedelta(days=i)).strftime("%Y%m%d"),
           "close":20 if i<30 else 10,"open":20 if i<30 else 10,"high":20 if i<30 else 10,"low":20 if i<30 else 10,"vol":100,"amount":1000} for i in range(60)]
    def records(method,**params):
        return bars if method=="daily" else [{"trade_date":b["trade_date"],"adj_factor":1 if i<30 else 2} for i,b in enumerate(bars)]
    monkeypatch.setattr(service,"_records",records)
    seen=[]
    monkeypatch.setattr(service,"pattern_tags",lambda rows,code,end:seen.extend(rows) or {"tags":[],"as_of":end,"status":"已计算"})
    service._patterns("600001.SH","2026-10-30")
    assert {r["close"] for r in seen} == {10}
    assert seen[0]["amount"] == 1000000


def test_refresh_deduplicates_running_job_and_persists_requested_interval(cache):
    class Jobs:
        value = None
        worker = None
        def get(self,key): return self.value
        def start(self,kind,params,worker):
            self.worker=worker
            self.value={"id":"limits-1","status":"queued","params":params,"type":kind}
            return self.value
    jobs=Jobs()
    value=service.start_refresh(jobs,"2026-10-01","2026-10-30")
    assert value["params"] == {"start":"2026-10-01","end":"2026-10-30"}
    assert service.start_refresh(jobs,"2026-10-02","2026-10-30")["id"] == value["id"]
