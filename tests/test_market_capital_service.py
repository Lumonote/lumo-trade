from copy import deepcopy
import datetime as dt
import sqlite3
import time

import pytest

from webui.services import market_capital_service as service
from webui.services.background_jobs import BackgroundJobService
from webui.services.job_store import JobStore


@pytest.fixture
def store(monkeypatch):
    values = {}
    monkeypatch.setattr(service, "_read", lambda key: deepcopy(values.get(key)))
    monkeypatch.setattr(service.kv_repo, "set_", lambda namespace, key, value: values.update({key: deepcopy(value)}))
    monkeypatch.setattr(service.kv_repo, "range_keys", lambda ns, a, b: sorted(k for k in values if a <= k <= b))
    monkeypatch.setattr(service.kv_repo, "range_items", lambda ns, a, b: [(k, deepcopy(values[k])) for k in sorted(values) if a <= k <= b])
    monkeypatch.setattr(service.calendar_repo, "open_days", lambda: [])
    monkeypatch.setattr(service, "_baseline", lambda: {})
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 10, 31, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    return values


@pytest.mark.parametrize("params", [{"window": 7}, {"deposit": 0}, {"deposit": float("nan")}, {"warning": 130, "closeout": 150}, {"warning": 301}])
def test_invalid_model_parameters_rejected(store, params):
    with pytest.raises(ValueError): service.financing(**params)


@pytest.mark.parametrize("params", [{"participant": "invented"}, {"start": "bad"}, {"start": "2026-10-30", "end": "2026-10-20"}, {"end": "2099-01-01"}])
def test_query_validation(store, params):
    with pytest.raises(ValueError): service.participants(**params)


def test_each_participant_uses_actual_source_date(store):
    store["participants"] = {"as_of": "2026-10-30", "flow_as_of": "2026-10-29", "calendar": [], "sources": [{"key": "etf", "as_of": "2026-10-28"}]}
    assert service.participants(participant="etf")["as_of"] == "2026-10-28"
    assert service.participants(participant="main")["as_of"] == "2026-10-29"


def test_bundled_data_are_read_back_from_sqlite(tmp_path, monkeypatch):
    from data_store.connection import reset_for_testing

    reset_for_testing()
    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(tmp_path / "capital.sqlite"))
    date = "2026-10-29"
    finance = {"as_of": date, "margin": [{"trade_date": date, "exchange_id": "SSE"}]}
    flow = {"ts_code": "600000.SH", "trade_date": date, "net_amount": 7}
    participants = {"as_of": date, "calendar": [date], "flows": [flow], "institutions_days": [date]}
    monkeypatch.setattr(service, "_baseline", lambda: {"financing": finance, "participants": participants})
    try:
        assert service.bundle("financing") == service._read("financing") == finance
        saved = service.bundle("participants")
        assert saved == service._read("participants") == {"as_of": date, "calendar": [date]}
        assert service._read(f"seed:moneyflow_dc:{date}") == [flow]
        assert service._read(f"seed:top_inst:{date}") == []
        reset_for_testing()
        monkeypatch.setattr(service, "_baseline", lambda: pytest.fail("SQLite seed already installed"))
        assert service.bundle("financing") == finance
        export = service.bundle("participants", include_daily=True)
        assert export["flows"] == [flow] and export["institutions_days"] == [date]
        assert service.history(code="600000")["totals"]["net"] == 70000
    finally:
        reset_for_testing()


def test_no_global_fallback_invented_stock_or_identity_data(store, monkeypatch):
    monkeypatch.setattr(service, "bundle", lambda kind: {})
    report = service.financing()
    assert report["rows"] == [] and report["coverage"]["model"] == 0
    assert service.participants()["totals"]["net"] is None


def test_collection_rejects_incomplete_market_without_overwriting_success(store, monkeypatch):
    store["financing"] = {"as_of": "2026-10-29", "margin": [{"trade_date": "20261029", "exchange_id": "SSE"}]}
    monkeypatch.setattr(service, "_records", lambda method, **params: [{"cal_date": f"202610{d:02d}", "is_open": 1} for d in range(1, 23)] if method == "trade_cal" else [{"ts_code": "600000.SH", "name": "样本"}])
    monkeypatch.setattr(service, "_day", lambda *args: [])
    with pytest.raises(ValueError, match="同日完整"):
        service.collect("financing")
    assert store["financing"]["as_of"] == "2026-10-29"


@pytest.mark.parametrize("error_type,expected", [(RuntimeError, "网络"), (sqlite3.OperationalError, "本地数据库")])
def test_refresh_reuses_running_job_and_failure_preserves_cache(store, monkeypatch, error_type, expected):
    class Jobs:
        def __init__(self): self.job = None; self.worker = None
        def get(self, job_id): return self.job
        def start(self, kind, params, worker):
            self.worker = worker
            self.job = {"id": "job1", "status": "queued"}
            return self.job
        def update(self, job_id, **fields): self.job.update(fields)
        def append_log(self, *args): pass
    store["financing"] = {"as_of": "2026-10-29"}
    jobs = Jobs()
    first = service.start_refresh("financing", jobs)
    assert service.start_refresh("financing", jobs) == first
    monkeypatch.setattr(service, "collect", lambda *args: (_ for _ in ()).throw(error_type("secret response body")))
    jobs.worker("job1", {})
    assert jobs.job["status"] == "failed"
    assert expected in jobs.job["error"]
    assert "secret" not in jobs.job["error"]
    assert store["financing"]["as_of"] == "2026-10-29"


def test_financing_refresh_finishes_after_progress_write_failure(store, tmp_path, monkeypatch):
    jobs = BackgroundJobService(JobStore(tmp_path / "jobs.sqlite"))
    store["financing"] = {"as_of": "2026-09-30"}
    calendar = [{"cal_date": f"202610{d:02d}", "is_open": 1} for d in range(1, 23)]
    stock = {"ts_code": "600000.SH", "name": "样本"}
    monkeypatch.setattr(service, "_records", lambda method, **kw: calendar if method == "trade_cal" else [stock])
    def day(method, date, fields):
        if method == "margin":
            return [{"trade_date": date, "exchange_id": ex, "rzye": 1} for ex in ("SSE", "SZSE")]
        return [{**stock, "trade_date": date}]
    monkeypatch.setattr(service, "_day", day)
    append = jobs.append_log
    def append_log(job_id, message):
        if message == "margin：1/22个交易日":
            raise sqlite3.OperationalError("unable to open database file")
        return append(job_id, message)
    monkeypatch.setattr(jobs, "append_log", append_log)
    update = jobs.update
    def update_job(job_id, **fields):
        if fields.get("progress") == 1:
            raise sqlite3.OperationalError("database temporarily unavailable")
        return update(job_id, **fields)
    monkeypatch.setattr(jobs, "update", update_job)
    job = service.start_refresh("financing", jobs)
    deadline = time.monotonic() + 10
    while (job := jobs.get(job["id"]))["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job["status"] == "finished" and job["progress"] == 100
    assert job["result"]["as_of"] == store["financing"]["as_of"] == "2026-10-22"
    assert "daily_basic" in job["logs"][-1]


def test_etf_fallback_uses_whitelist_same_day_nav_and_disclosure_date(store, monkeypatch):
    def day(method, date, fields):
        if method == "etf_share_size": raise RuntimeError("permission unavailable")
        return [{"ts_code":"510300.SH","fd_share":100},{"ts_code":"160000.SZ","fd_share":999}]
    monkeypatch.setattr(service,"_day",day)
    monkeypatch.setattr(service,"_records",lambda *args,**kwargs:[{"ts_code":"510300.SH","nav_date":"20261030","ann_date":"20261031","unit_nav":2}])
    rows=service._etf_day("2026-10-30",{"510300.SH":{"extname":"样本ETF"}})
    assert len(rows)==1 and rows[0]["total_share"]==100 and rows[0]["nav"]==2
    assert rows[0]["total_size"]==200  # 万份×元=万元


@pytest.mark.parametrize("source", ["fund_share + fund_nav", "fund_share + fund_nav（ETF白名单，同日净值）"])
def test_etf_merge_tracks_size_provenance_separately(source):
    fallback = {"trade_date": "20261030", "ts_code": "510300.SH", "total_share": 100, "nav": 2, "total_size": 200, "source": source}
    primary = {"trade_date": "20261030", "ts_code": "510300.SH", "total_share": 110, "nav": 3, "total_size": 350}
    row = service._merge_etf([fallback], [primary])[0]
    assert row["total_size"] == 350 and row["size_estimated"] is False
    assert service._merge_etf([row], [{**fallback, "nav": 4}])[0]["total_size"] == 350
    revised = service._merge_etf([fallback], [{**primary, "total_size": None}])[0]
    assert revised["total_size"] == 330 and revised["size_estimated"] is True


@pytest.mark.parametrize("failure", ["shares", "nav"])
def test_etf_fallback_keeps_independent_observations(store, monkeypatch, failure):
    def day(method, date, fields):
        if method == "etf_share_size" or failure == "shares":
            raise RuntimeError("unavailable")
        return [{"ts_code": "510300.SH", "fd_share": 0}]
    def records(*args, **kwargs):
        if failure == "nav":
            raise RuntimeError("unavailable")
        return [{"ts_code": "510300.SH", "nav_date": "20261030", "ann_date": "20261031", "unit_nav": 2}]
    monkeypatch.setattr(service, "_day", day)
    monkeypatch.setattr(service, "_records", records)
    row = service._etf_day("2026-10-30", {"510300.SH": {}})[0]
    assert row["total_share"] == (None if failure == "shares" else 0)
    assert row["nav"] == (2 if failure == "shares" else None)
    assert row["total_size"] is None


def test_collect_etf_includes_predecessor_for_first_window_day(store, monkeypatch):
    store["participants"] = {"calendar": []}
    def records(method, **params):
        if method == "trade_cal":
            return [{"cal_date": f"202610{d:02d}", "is_open": 1} for d in range(1, 31)]
        if method == "stock_basic":
            return [{"ts_code": "600000.SH", "name": "样本"}]
        return []
    monkeypatch.setattr(service, "_records", records)
    monkeypatch.setattr(service, "_day", lambda *args: [])
    monkeypatch.setattr(service.quant_radar_repo, "list_dates", lambda **kwargs: [])
    monkeypatch.setattr(service, "read_baseline", lambda: {})
    monkeypatch.setattr(service, "_etf_day", lambda date, meta: [{"trade_date": date, "ts_code": "510300.SH", "total_share": int(date[-2:]), "nav": 2}])
    result = service.collect("participants")
    assert result["calendar"][0] == "2026-10-09" and result["etf_predecessors"]["2026-10-09"] == "2026-10-08"
    assert service.participants(participant="etf", start="2026-10-09", end="2026-10-09")["rows"][0]["net"] == 20000


def test_etf_refresh_backfills_more_than_default_window_and_keeps_history(store, monkeypatch, tmp_path):
    code = "510300.SH"
    start, end = "2026-08-29", "2026-10-10"
    monday, friday = "2026-08-31", "2026-10-09"
    previous = "2026-08-28"
    missing = "2026-09-21"
    days = [dt.date(2026, 8, 27) + dt.timedelta(days=i) for i in range(66)]
    calendar = [{"cal_date": d.isoformat(), "is_open": int(d.weekday() < 5 and not dt.date(2026, 10, 1) <= d <= dt.date(2026, 10, 7))}
                for d in days]
    stock = {"ts_code": "600000.SH", "name": "样本"}
    reads, methods = [], []

    def records(method, **params):
        methods.append(method)
        if method == "trade_cal":
            assert params["exchange"] == "SSE" and params["is_open"] == "1"
            return calendar  # 即使源返回休市日或范围外日期，也不得误采。
        return {"stock_basic": [stock], "etf_basic": [{"ts_code": code, "extname": "样本ETF"}]}.get(method, [])

    def etf_day(date, _):
        reads.append(date)
        return [] if date == missing else [{"trade_date": date, "ts_code": code,
                                            "total_share": dt.date.fromisoformat(date).toordinal(), "nav": 2}]

    latest = "2026-10-30"
    store["participants"] = {"as_of": latest, "calendar": [latest], "etf_previous_date": "2026-10-29",
                             "etf": etf_day(latest, {}), "flows": [{**stock, "trade_date": latest, "net_amount": 1}],
                             "sources": [{"key": "flows", "as_of": latest}]}
    initial = deepcopy(store["participants"])
    reads.clear()
    monkeypatch.setattr(service, "_records", records)
    monkeypatch.setattr(service, "_etf_day", etf_day)
    monkeypatch.setattr(service.calendar_repo, "open_days", lambda: [])
    jobs = BackgroundJobService(JobStore(tmp_path / "jobs.sqlite"))
    params = {"participant": "etf", "start": start, "end": end}
    job = service.start_refresh("participants", jobs, **params)
    deadline = time.monotonic() + 10
    while (job := jobs.get(job["id"]))["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.02)

    assert job["status"] == "finished" and job["progress"] == 100, job
    assert job["params"] == params
    assert job["logs"][-1].endswith("25/25 个区间交易日")
    assert job["result"] == {"start": start, "end": end, "as_of": friday,
                             "notes": ["ETF有1个区间交易日读取失败或未披露；缺失不补零，保留已有记录"],
                             "processed_days": 25, "amount_days": 23, "remaining_days": 0}
    assert methods == ["trade_cal", "etf_basic"]
    assert sorted(reads) == [previous] + [r["cal_date"] for r in calendar if r["is_open"] and start <= r["cal_date"] <= end]
    saved = store["participants"]
    assert saved["as_of"] == initial["as_of"] and saved["calendar"] == initial["calendar"] and "flows" not in saved
    assert store[f"raw:moneyflow_dc:{latest}"] == initial["flows"]
    assert saved["etf_predecessors"][monday] == previous and saved["etf_calendar_ends"][friday] == end
    report = service.participants(participant="etf", start=start, end=end)
    history = service.history(participant="etf", code=code, start=start, end=end)
    assert report["data_as_of"] == latest  # 源的最近日期不随回补历史而倒退。
    assert report["rows"][0]["observed_net"] == history["totals"]["net"]
    assert history["rows"][0]["date"] == monday and history["rows"][0]["net"] == 60000
    assert history["coverage"]["days"] == 25 and history["coverage"]["amount_days"] == 23

    monkeypatch.setattr(service, "_day", lambda *args: [])
    monkeypatch.setattr(service.quant_radar_repo, "list_dates", lambda **kwargs: [])
    monkeypatch.setattr(service, "read_baseline", lambda: {})
    refreshed = service.collect("participants")
    assert len(refreshed["calendar"]) == 22 and monday not in refreshed["calendar"]
    assert refreshed["etf_predecessors"][monday] == previous
    assert service.participants(participant="etf", start=monday, end=monday)["rows"][0]["net"] == 60000


@pytest.mark.parametrize("minute", [29, 30])
def test_single_day_etf_refresh_obeys_beijing_cutoff(store, monkeypatch, minute):
    date = "2026-10-30"
    previous = "2026-10-29"
    monkeypatch.setattr(service, "now", lambda: dt.datetime(2026, 10, 30, 15, minute, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    monkeypatch.setattr(service, "_records", lambda method, **params: [{"cal_date": d, "is_open": 1} for d in (previous, date)]
                        if method == "trade_cal" else [])
    monkeypatch.setattr(service, "_etf_day", lambda d, _: [{"trade_date": d, "ts_code": "510300.SH", "total_share": 100, "nav": 1}])
    store["participants"] = {"calendar": [], "as_of": previous}
    progress = []
    log = lambda *args: progress.append(args)
    if minute == 29:
        with pytest.raises(ValueError, match="15:30"):
            service.collect_etf_range(date, date, log)
        assert not progress and store["participants"] == {"calendar": [], "as_of": previous}
        result = service.collect_etf_range(previous, date, log)
        assert result["as_of"] is None and result["processed_days"] == 1
        assert result["notes"][-1] == f"{date} 未到北京时间15:30，未计入采集交易日"
        assert date not in store["participants"]["etf_predecessors"]
    else:
        result = service.collect_etf_range(date, date, log)
        assert result["as_of"] == date and result["processed_days"] == result["amount_days"] == 1
        assert [event[1:] for event in progress] == [(0, 1), (1, 1)]
        assert service.participants(participant="etf", start=date, end=date)["rows"][0]["net"] == 0


def test_history_is_read_only_and_actual_cutoff_is_not_query_end(store, monkeypatch):
    store["participants"] = {"calendar": ["2026-10-29"], "stocks": [{"ts_code": "600000.SH", "name": "样本"}],
                             "flows": [{"ts_code": "600000.SH", "trade_date": "20261029", "net_amount": 0}],
                             "sources": [{"key": "flows", "as_of": "2026-10-29"}]}
    monkeypatch.setattr(service.calendar_repo, "open_days", lambda: [])
    monkeypatch.setattr(service, "_records", lambda *a, **k: pytest.fail("只读查询不能采集"))
    report = service.participants(start="2026-10-29", end="2026-10-31")
    assert report["data_as_of"] == "2026-10-29" and report["as_of"] == "2026-10-31"
    history = service.history(code="600000", start="2026-10-29", end="2026-10-31")
    assert history["totals"]["net"] == 0 and history["data_as_of"] == "2026-10-29"


@pytest.mark.parametrize("params", [{"code": "bad"}, {"code": "600000", "participant": "fake"}, {"code": "600000", "start": "bad"},
                                    {"code": "600000", "start": "2026-10-31", "end": "2026-10-20"}, {"code": "600000", "end": "2099-01-01"}])
def test_history_rejects_invalid_query(store, params):
    with pytest.raises(ValueError):
        service.history(**params)


def test_older_participant_windows_report_provider_limits_and_offer_exact_backfill(store):
    store['participants'] = {'as_of': '2026-10-29', 'calendar': ['2026-10-29']}
    window = {'start': '2008-01-01', 'end': '2008-02-29'}
    assert service.participants(**window)['backfill'] is None
    assert '2023-09-11' in service.history(code='600000', **window)['coverage_note']
    for participant in ('institution', 'etf'):
        report = service.participants(participant=participant, **window)
        assert report['backfill'] == {'participant': participant, **window}
        chart = service.history(code='600000', participant=participant, start='2008-01-01', end='2026-10-29')
        assert chart['start'] == '2008-01-01' and chart['backfill']['participant'] == participant
    assert service.participants(start='2000-01-01', end='2026-10-29')['backfill']['start'] == '2023-09-11'


def test_old_institution_batch_fills_only_selected_days_and_resumes(store, monkeypatch):
    dates = ['2008-01-02', '2008-01-03', '2008-01-04', '2008-01-07']
    seed = {'as_of': '2026-10-29', 'calendar': ['2026-10-29'],
            'institutions': [{'trade_date': '2026-10-29', 'ts_code': '600000.SH', 'buy': 100}]}
    store['participants'] = seed
    reads = []
    monkeypatch.setattr(service, 'RANGE_LIMIT', 2)
    monkeypatch.setattr(service, '_historical_sessions', lambda a, b: dates)

    def source(method, **params):
        day = service.iso(params['trade_date'])
        reads.append((method, day))
        return [{'trade_date': day, 'ts_code': '600000.SH', 'exalter': '机构专用', 'reason': '日涨幅偏离值达7%', 'buy': 10, 'sell': 1, 'net_buy': 9}]

    monkeypatch.setattr(service, '_records', source)
    monkeypatch.setattr(service, 'close_conn', lambda: None)
    args = ('institution', dates[0], dates[-1], lambda *args: None)
    result = service.collect_orders_range(*args)
    assert result['processed_days'] == 2 and result['remaining_days'] == 2
    result = service.collect_orders_range(*args)
    assert result['processed_days'] == 2 and result['remaining_days'] == 0
    assert reads == [('top_inst', d) for d in dates]
    report = service.history(code='600000', participant='institution', start=dates[0], end=dates[-1])
    assert report['coverage']['observed'] == 4 and report['totals']['net'] == 36
    assert report['backfill'] is None
    assert service.participants(participant='institution')['as_of'] == seed['as_of']


def test_quant_participant_list_uses_actual_archived_observations(store, isolated_price_db):
    store['participants'] = {'as_of': '2026-10-29', 'calendar': ['2026-10-29']}
    for date, activity in [('2021-01-04', 20), ('2021-01-05', 30), ('2026-10-29', 40)]:
        service.quant_radar_repo.upsert_day(date, [{'code': '600000', 'name': '样本', 'activity': activity}])
    report = service.participants(participant='quant', start='2021-01-01', end='2021-12-31')
    assert [(r['as_of'], r['activity'], r['net']) for r in report['rows']] == [('2021-01-05', 30, None)]
    assert report['backfill'] is None and '2021-01-04' in report['coverage_note']


def test_historical_ranking_and_candles_survive_refresh(store, monkeypatch):
    dates = [f"2026-07-{d:02}" for d in (7, 8, 9, 10, 13)]
    stock = {'ts_code': '600000.SH', 'name': '样本'}
    flow = lambda d, n: {**stock, 'trade_date': d, 'net_amount': n}
    # Baseline, old header and partition overlap. The last two copies of day 3 are duplicates.
    store.update({f'seed:moneyflow_dc:{d}': [flow(d, 1)] for d in dates})
    store['participants'] = {'as_of': dates[-1], 'calendar': dates, 'flows': [flow(dates[0], 0), flow(dates[2], 99)]}
    store['raw:moneyflow_dc:' + dates[2]] = [flow(dates[2], 2)] * 2
    def check():
        args = {'start': dates[0], 'end': dates[-1]}
        lst = service.participants(**args)['rows'][0]
        chart = service.history(code='600000', **args)
        assert lst['days'] == chart['coverage']['amount_days'] == 5
        assert lst['net'] == chart['totals']['net'] == chart['rows'][-1]['close'] == 50_000
        assert chart['rows'][0]['net'] == 0
        return chart
    check()
    recent = [f'2026-10-{n:02}' for n in range(1, 23)]
    monkeypatch.setattr(service, '_sse_open_days', lambda *_: recent)
    monkeypatch.setattr(service, '_records', lambda method, **kw: [stock] if method == 'stock_basic' else
                        [flow(kw['trade_date'], 3)] if method == 'moneyflow_dc' else [])
    monkeypatch.setattr(service, '_collect_etf', lambda *a: None)
    monkeypatch.setattr(service.quant_radar_repo, 'list_dates', lambda **kw: [])
    monkeypatch.setattr(service, 'read_baseline', lambda: {})
    result = service.collect('participants')
    assert next(s for s in result['sources'] if s['key'] == 'institutions')['failures'] == 0
    assert not set(service.DAILY_SOURCES).intersection(store['participants'])
    chart = check()
    assert chart['data_as_of'] == dates[-1] and chart['available_end'] == recent[-1]


def test_partial_refresh_keeps_old_seed_as_latest_available_flow(store, monkeypatch):
    date = "2026-09-30"
    stock = {"ts_code": "600000.SH", "name": "样本"}
    flow = {**stock, "trade_date": date, "net_amount": 7}
    store["seed:moneyflow_dc:" + date] = [flow]
    store["participants"] = {"as_of": date, "calendar": [date], "stocks": [stock], "etf": [{"trade_date": date}]}
    recent = [f"2026-10-{n:02}" for n in range(1, 23)]
    monkeypatch.setattr(service, "_sse_open_days", lambda *_: recent)
    monkeypatch.setattr(service, "_records", lambda method, **kwargs: [stock] if method == "stock_basic" else [])
    monkeypatch.setattr(service, "_collect_etf", lambda *args: None)
    monkeypatch.setattr(service.quant_radar_repo, "list_dates", lambda **kwargs: [])
    monkeypatch.setattr(service, "read_baseline", lambda: {})
    service.collect("participants")
    report = service.participants()
    assert report["as_of"] == report["data_as_of"] == date
    assert report["totals"]["net"] == 70000


def test_seats_distinguish_observed_empty_and_uncollected(store, monkeypatch):
    a, b, c = '2026-10-28', '2026-10-29', '2026-10-30'
    seat = {'trade_date': a, 'ts_code': '600000.SH', 'reason': '日涨幅偏离', 'exalter': '机构专用', 'buy': 100, 'sell': 20}
    store.update({'participants': {'as_of': c, 'calendar': [a, b, c]}, 'raw:top_inst:' + a: [seat, seat, {**seat, 'buy': 40}],
                  'raw:top_inst:' + c: []})
    q = {'participant': 'institution', 'start': a, 'end': c}
    report = service.participants(**q)
    chart = service.history(code='600000', **q)
    assert report['range_note'] and report['rows'][0]['net'] == chart['totals']['net'] == 100
    assert [r['status'] for r in chart['rows']] == ['observed', 'unrecorded', 'no_public_seat']
    assert chart['data_as_of'] == a and chart['available_end'] == c
    monkeypatch.setattr(service, '_records', lambda *a, **kw: [])
    service._day('top_inst', '2026-10-31')
    assert 'raw:top_inst:2026-10-31' not in store
    assert not service.participants(start='2026-10-31')['rows']
