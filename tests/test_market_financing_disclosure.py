"""两融披露等待与来源失败分开，沪深完整汇总决定统计日期。"""
from copy import deepcopy
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from webui.services import market_capital_service as service

READ_DAY = service._day


def margin_rows(day, exchanges=("SSE", "SZSE")):
    return [{"trade_date": day, "exchange_id": exchange, "rzye": 100,
             "rzmre": 10, "rzche": 8, "rqye": 2, "rzrqye": 102}
            for exchange in exchanges]


@pytest.fixture
def feed(monkeypatch):
    values, calls, overrides = {}, [], {}
    current = {"value": dt.datetime(2026, 11, 25, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai"))}
    # 国庆后这一段没有节假日，周六、周日始终不是交易日。
    days = [dt.date(2026, 10, 12) + dt.timedelta(days=i) for i in range(50)]
    calendar = [{"cal_date": day.isoformat(), "is_open": int(day.weekday() < 5)} for day in days]
    stock = {"ts_code": "600000.SH", "name": "样本", "industry": "银行"}
    monkeypatch.setattr(service, "now", lambda: current["value"])
    monkeypatch.setattr(service, "_read", lambda key: deepcopy(values.get(key)))
    monkeypatch.setattr(service.kv_repo, "set_", lambda ns, key, value: values.update({key: deepcopy(value)}))
    monkeypatch.setattr(service, "_baseline", lambda: {})
    monkeypatch.setattr(service.calendar_repo, "open_days", lambda: [])

    def records(method, **params):
        if method == "trade_cal":
            assert params["exchange"] == "SSE"
            return calendar
        assert method == "stock_basic"
        return [stock]

    def day(method, date, fields=None):
        calls.append((method, date))
        if method == "margin":
            if date in overrides:
                value = overrides[date]
                if isinstance(value, Exception):
                    raise value
                return deepcopy(value)
            return margin_rows(date) if date < current["value"].date().isoformat() else []
        assert method in ("margin_detail", "daily", "adj_factor", "daily_basic")
        return [{**stock, "trade_date": date, "rzye": 100, "rzmre": 10, "rzche": 8,
                 "close": 10, "adj_factor": 1, "circ_mv": 100, "turnover_rate": 1}]

    monkeypatch.setattr(service, "_records", records)
    monkeypatch.setattr(service, "_day", day)
    return {"values": values, "calls": calls, "overrides": overrides, "current": current}


def source(result, key="margin"):
    return next(row for row in result["sources"] if row["key"] == key)


def test_evening_collection_stops_before_today_and_aligns_financing_inputs(feed):
    result = service.collect("financing")

    margin_dates = sorted(date for method, date in feed["calls"] if method == "margin")
    assert len(margin_dates) == 22 and margin_dates[-1] == "2026-11-24"
    assert all(dt.date.fromisoformat(day).weekday() < 5 for day in margin_dates)
    assert result["as_of"] == result["calendar"][-1] == "2026-11-24"
    assert result["errors"] == []
    assert source(result)["failures"] == 0
    assert {row["as_of"] for row in result["sources"]} == {"2026-11-24"}
    assert all(date <= "2026-11-24" for _, date in feed["calls"])
    assert any("2026-11-25" in note and "披露" in note for note in result["notes"])
    assert feed["values"]["financing"] == result


@pytest.mark.parametrize("clock, missing, previous", [
    ("2026-11-21T10:00:00", "2026-11-20", "2026-11-19"),
    ("2026-11-22T17:00:00", "2026-11-20", "2026-11-19"),
    ("2026-11-23T08:59:59", "2026-11-20", "2026-11-19"),
    ("2026-11-23T09:03:00", "2026-11-20", "2026-11-19"),
    ("2026-11-24T08:59:59", "2026-11-23", "2026-11-20"),
    ("2026-11-24T09:04:59", "2026-11-23", "2026-11-20"),
])
@pytest.mark.parametrize("exchanges", [(), ("SSE",)])
def test_recent_empty_or_single_exchange_waits_for_next_session_disclosure(
        feed, clock, missing, previous, exchanges):
    feed["current"]["value"] = dt.datetime.fromisoformat(clock).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    feed["overrides"][missing] = margin_rows(missing, exchanges)

    result = service.collect("financing")

    assert result["as_of"] == source(result)["as_of"] == previous
    assert source(result)["failures"] == 0
    assert source(result)["pending_dates"] == [missing]
    assert result["errors"] == []
    assert any(missing in note and "披露" in note for note in result["notes"])
    assert all(row["as_of"] == previous for row in result["sources"])


@pytest.mark.parametrize("clock, latest", [
    ("2026-11-21T10:00:00", "2026-11-20"),
    ("2026-11-23T08:59:59", "2026-11-20"),
    ("2026-11-24T08:59:59", "2026-11-23"),
])
def test_complete_summary_is_accepted_even_before_disclosure_deadline(feed, clock, latest):
    feed["current"]["value"] = dt.datetime.fromisoformat(clock).replace(tzinfo=ZoneInfo("Asia/Shanghai"))

    result = service.collect("financing")

    assert result["as_of"] == source(result)["as_of"] == latest
    assert source(result)["failures"] == 0
    assert not source(result).get("pending_dates")
    assert result["errors"] == []


@pytest.mark.parametrize("exchanges", [(), ("SSE",)])
@pytest.mark.parametrize("minute, second", [(5, 0), (5, 1), (10, 0)])
def test_incomplete_summary_at_or_after_next_session_disclosure_deadline_is_failure(feed, exchanges, minute, second):
    feed["current"]["value"] = dt.datetime(2026, 11, 23, 9, minute, second, tzinfo=ZoneInfo("Asia/Shanghai"))
    feed["overrides"]["2026-11-20"] = margin_rows("2026-11-20", exchanges)

    result = service.collect("financing")

    assert result["as_of"] == source(result)["as_of"] == "2026-11-19"
    assert source(result)["failures"] == 1
    assert not source(result).get("pending_dates")
    assert any(error.startswith("margin") for error in result["errors"])


@pytest.mark.parametrize("response", [[], margin_rows("2026-11-18", ("SSE",)), RuntimeError("private connection failure")])
def test_historical_empty_partial_or_exception_is_failure_and_preserves_saved_day(feed, response):
    saved = margin_rows("2026-11-18") + margin_rows("2026-11-24")
    feed["values"]["financing"] = {"as_of": "2026-11-24", "margin": deepcopy(saved)}
    feed["overrides"]["2026-11-18"] = response

    result = service.collect("financing")

    assert result["as_of"] == source(result)["as_of"] == "2026-11-24"
    assert source(result)["failures"] == 1
    assert not source(result).get("pending_dates")
    assert any(error.startswith("margin") for error in result["errors"])
    assert "private connection" not in str(result["errors"])
    assert [row for row in result["margin"] if row["trade_date"] == "2026-11-18"] == saved[:2]


def test_real_source_exception_is_failure_even_during_disclosure_window(feed):
    feed["current"]["value"] = dt.datetime(2026, 11, 23, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    feed["overrides"]["2026-11-20"] = RuntimeError("private connection failure")

    result = service.collect("financing")

    assert source(result)["failures"] == 1
    assert not source(result).get("pending_dates")
    assert any(error.startswith("margin") for error in result["errors"])
    assert "private connection" not in str(result["errors"])


def test_financing_query_exposes_disclosure_notes_separately_from_errors(feed):
    data = service.collect("financing")

    result = service.financing()

    assert result["as_of"] == "2026-11-24"
    assert result["notes"] == data["notes"]
    assert result["notes"] and result["errors"] == []


@pytest.mark.parametrize("failed", [False, True])
def test_financing_job_keeps_normal_notes_and_source_errors_separate(feed, monkeypatch, failed):
    class Jobs:
        def __init__(self):
            self.job = None

        def get(self, job_id):
            return self.job

        def start(self, kind, params, worker):
            self.job = {"id": "financing-job", "status": "queued"}
            worker(self.job["id"], params)
            return self.job

        def update(self, job_id, **fields):
            self.job.update(fields)

        def append_log(self, job_id, message):
            self.job.setdefault("logs", []).append(message)

    if failed:
        feed["overrides"]["2026-11-18"] = RuntimeError("private connection failure")
    monkeypatch.setattr(service, "close_conn", lambda: None)

    job = service.start_refresh("financing", Jobs())

    data = feed["values"]["financing"]
    assert job["status"] == "finished", job
    assert job["result"]["as_of"] == "2026-11-24"
    assert job["result"]["notes"] == data["notes"]
    assert job["result"]["errors"] == data["errors"]
    assert bool(job["result"]["errors"]) is failed
    assert job["result"]["notes"]
    assert not set(job["result"]["notes"]) & set(job["result"]["errors"])


def test_old_incomplete_margin_cache_is_retried_after_three_days(feed, monkeypatch):
    date = "2026-11-18"
    key = f"raw:margin:{date}"
    feed["values"][key] = margin_rows(date, ("SSE",))
    calls = []

    def records(method, **params):
        calls.append((method, params))
        return margin_rows(date)

    monkeypatch.setattr(service, "_records", records)
    monkeypatch.setattr(service, "close_conn", lambda: None)

    result = READ_DAY("margin", date)

    assert calls == [("margin", {"trade_date": "20261118"})]
    assert result == margin_rows(date)
    assert feed["values"][key] == margin_rows(date)


@pytest.mark.parametrize("response", [[], margin_rows("2026-11-24", ("SSE",))], ids=["empty", "single-exchange"])
@pytest.mark.parametrize("clock", ["09:03:00", "17:12:00"])
def test_recent_margin_regression_preserves_complete_cache_and_reports_failure(feed, monkeypatch, response, clock):
    date = "2026-11-24"
    key = f"raw:margin:{date}"
    saved = margin_rows(date)
    feed["current"]["value"] = dt.datetime.fromisoformat(f"2026-11-25T{clock}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    feed["values"][key] = deepcopy(saved)
    feed["values"]["financing"] = {"as_of": date, "margin": deepcopy(saved)}
    records, day = service._records, service._day

    def current_records(method, **params):
        return deepcopy(response) if method == "margin" else records(method, **params)

    def current_day(method, requested, fields=None):
        if method == "margin" and requested == date:
            return READ_DAY(method, requested, fields)
        return day(method, requested, fields)

    monkeypatch.setattr(service, "_records", current_records)
    monkeypatch.setattr(service, "_day", current_day)
    monkeypatch.setattr(service, "close_conn", lambda: None)

    result = service.collect("financing")

    assert feed["values"][key] == saved
    assert [row for row in result["margin"] if row["trade_date"] == date] == saved
    assert result["as_of"] == source(result)["as_of"] == date
    assert source(result)["failures"] == 1
    assert not source(result)["pending_dates"]
    assert any(error.startswith("margin") for error in result["errors"])
