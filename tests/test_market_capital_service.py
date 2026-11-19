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
