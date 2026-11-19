"""桌面菜单与体检API契约，隔离SQLite，不联网。"""
import importlib
import sys

import pytest


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    from unittest.mock import patch
    tmp=tmp_path_factory.mktemp("market_health")
    with patch.dict("os.environ",{"KRONOS_USER_DIR":str(tmp),"KRONOS_SQLITE_PATH":str(tmp/"health.sqlite"),"KRONOS_DISABLE_TORCH":"1"}):
        from data_store.connection import reset_for_testing
        sys.modules.pop("webui.robyn_app",None);sys.modules.pop("webui.core",None)
        reset_for_testing()
        app=importlib.import_module("webui.robyn_app")
        yield app
        sys.modules.pop("webui.robyn_app",None);sys.modules.pop("webui.core",None)
        reset_for_testing()


def test_desktop_page_and_packaged_assets(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        response=client.get("/desktop/market_health")
        assert response.status_code==200
        assert 'id="marketHealthRoot"' in response.text and "跨市场研判" in response.text
        assert "lumo_market_health.js" in response.text
        for url in ("/static/lumo_market_health.js","/static/lumo_market_health.css","/static/research/ashare_bottom_20261022.html"):
            assert client.get(url).status_code==200
        assert "/desktop/market_health" in client.get("/desktop/features").text


def test_query_contains_all_dimensions_without_invented_cross_data(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        response=client.get("/api/market-health")
    assert response.status_code==200
    p=response.json()
    assert p["as_of"]=="2026-10-22" and len(p["indices"])==7
    assert len(p["fusion"]["dimensions"])==7
    assert p["fusion"]["complete"] is True and p["fusion"]["missing"] == []
    assert p["fusion"]["pressure"] is not None
    future=next(d for d in p["fusion"]["dimensions"] if d["key"]=="futures")
    assert len(future["details"]["basis"])==4
    assert all(r["basis_change"] is not None for r in future["details"]["basis"])


def test_refresh_uses_shared_jobs_and_rejects_bad_modes(runtime,monkeypatch):
    from robyn.testing import TestClient
    calls=[]
    monkeypatch.setattr(runtime.market_health_service,"start_refresh",lambda mode,jobs,loader:calls.append((mode,jobs,loader)) or {"id":"health-test","status":"queued"})
    with TestClient(runtime.app) as client:
        response=client.post("/api/market-health/refresh",json_data={"mode":"cross"})
    assert response.status_code==202
    assert calls==[("cross",runtime.webui_core.JOB_SERVICE,runtime.webui_core._market_pulse_index_bars)]


def test_invalid_refresh_body_is_400(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        response=client.post("/api/market-health/refresh",json_data={"mode":"delete"})
    assert response.status_code==400


def test_sector_research_routes_include_real_dated_baseline_and_constituents(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        response=client.get("/api/market-health/sectors")
        assert response.status_code==200
        value=response.json()
        assert len(value["rows"])>100 and len(value["calendar"])>=20
        code=next(iter(runtime.sector_depth_service.baseline()["details"]))
        detail=client.get("/api/market-health/sectors/detail",query_params={"code":code}).json()
        assert detail["as_of"]==detail["member_as_of"]==value["as_of"]
        assert detail["covered"]==detail["members"]
        assert client.get("/api/market-health/sectors/detail",query_params={"code":"invalid"}).status_code==400
        page=client.get("/desktop/market_health").text
        assert 'id="mhView-sectors"' in page and "lumo_sector_depth.js" in page
        assert client.get("/static/lumo_sector_depth.js").status_code==200


def test_financing_and_participants_native_routes_and_assets(runtime, monkeypatch):
    from robyn.testing import TestClient
    calls=[]
    monkeypatch.setattr(runtime.market_capital_service,"financing",lambda jobs,**params: calls.append(params) or {"as_of":"2026-10-29","rows":[]})
    monkeypatch.setattr(runtime.market_capital_service,"participants",lambda jobs,**params: {"participant":params["participant"],"start":params["start"]})
    monkeypatch.setattr(runtime.market_capital_service,"start_refresh",lambda kind,jobs: {"id":kind,"status":"queued"})
    with TestClient(runtime.app) as client:
        assert client.get("/api/market-health/financing",query_params={"window":"5"}).json()["as_of"]=="2026-10-29"
        assert calls[0]["window"]=="5"
        data=client.get("/api/market-health/participants",query_params={"participant":"etf","start":"2026-10-21"}).json()
        assert data=={"participant":"etf","start":"2026-10-21"}
        for kind in ("financing","participants"):
            response=client.post(f"/api/market-health/{kind}/refresh",json_data={})
            assert response.status_code==202 and response.json()["job"]["id"]==kind
        page=client.get("/desktop/market_health").text
        assert 'id="mhView-financing"' in page and 'id="mhView-participants"' in page
        for name in ("lumo_market_capital.js","lumo_market_capital.css"):
            assert name in page and client.get("/static/"+name).status_code==200


def test_capital_routes_reject_invalid_queries(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        assert client.get("/api/market-health/financing",query_params={"deposit":"nan"}).status_code==400
        assert client.get("/api/market-health/financing",query_params={"window":"wrong"}).status_code==400
        assert client.get("/api/market-health/participants",query_params={"participant":"unverified"}).status_code==400
        assert client.get("/api/market-health/participants",query_params={"start":"2026-10-30","end":"2026-10-20"}).status_code==400


def test_participant_refresh_validates_range_and_matches_running_job(runtime, monkeypatch):
    from robyn.testing import TestClient
    path = "/api/market-health/participants/refresh"
    params = {"participant": "etf", "start": "2026-01-01", "end": "2026-10-30"}
    active = {"id": "etf-range", "status": "running", "params": params}
    service = runtime.market_capital_service
    monkeypatch.setattr(service, "_status", lambda kind, jobs: active)

    with TestClient(runtime.app) as client:
        for invalid in (["etf"], {**params, "keyword": "ETF"}, {**params, "start": None}, {**params, "end": ""},
                        {**params, "participant": "main"}, {**params, "end": "20261030"},
                        {**params, "end": "2026-02-30"}, {**params, "end": "2025-01-01"},
                        {**params, "end": "2099-01-01"}, {**params, "start": "2000-01-01"}):
            assert client.post(path, json_data=invalid).status_code == 400, invalid

        response = client.post(path, json_data=params)
        assert response.status_code == 202 and response.json()["job"] == active
        for conflicting in ({}, {**params, "start": "2026-01-02"}):
            response = client.post(path, json_data=conflicting)
            assert response.status_code == 409 and "正在执行" in response.json()["error"]


def test_participant_history_native_route_and_validation(runtime, monkeypatch):
    from robyn.testing import TestClient
    data = {"calendar": ["2026-11-17", "2026-11-18"], "etf": [
        {"trade_date": d, "ts_code": "158000.SZ", "total_share": 2292.5835, "nav": 1.0326} for d in ("20261117", "20261118")]}
    monkeypatch.setattr(runtime.market_capital_service, "bundle", lambda kind: data)
    monkeypatch.setattr(runtime.market_capital_service.calendar_repo, "open_days", lambda: [])
    with TestClient(runtime.app) as client:
        path = "/api/market-health/participants/history"
        response = client.get(path, query_params={"participant": "etf", "code": "158000.SZ", "start": "2026-11-18", "end": "2026-11-18"})
        assert response.status_code == 200
        result = response.json()
        assert result["totals"]["net"] == 0 and result["rows"][0]["share_change"] == 0
        for params in ({"code": "invalid"}, {"code": "600000", "participant": "invalid"}, {"code": "600000", "start": "bad"}):
            assert client.get(path, query_params=params).status_code == 400


def test_macro_native_query_refresh_and_packaged_assets(runtime, monkeypatch):
    from analysis.market_macro import INDEXES, SPECS, SPREAD
    from robyn.testing import TestClient
    monkeypatch.setattr(runtime.market_macro_service, "start_refresh", lambda jobs: {"id": "macro", "status": "queued"})
    with TestClient(runtime.app) as client:
        response = client.get("/api/market-health/macro", query_params={"factor": "gold"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["factor"] == "gold" and {r["key"] for r in payload["metrics"]} == SPECS.keys()
        assert len(payload["sector_sensitivity"]) == 31 and payload["relationship"]["n"] > 200
        assert payload["valuation"]["available"] and payload["events"]["total"] == len(payload["events"]["rows"])
        assert {r["key"] for r in payload["valuation_comparison"]["rows"]} == INDEXES.keys()
        assert any(r["key"] == SPREAD for r in payload["factors"])
        response = client.get("/api/market-health/macro", query_params={"factor": SPREAD, "equity": "csi500", "frequency": "monthly", "window": "60"})
        assert response.status_code == 200
        spread = response.json()
        assert spread["equity"] == "csi500" and spread["chart"]["factor_history"][-1]["value"] == pytest.approx(100 / 26.7601 - 1.65)
        job = client.post("/api/market-health/macro/refresh", json_data={})
        assert job.status_code == 202 and job.json()["job"]["id"] == "macro"
        page = client.get("/desktop/market_health").text
        assert 'id="mhView-macro"' in page and "宏观经济" in page
        assert 'id="mmValuationComparison"' in page and 'id="mmCandleQuery"' in page
        for name in ("lumo_market_macro.js", "lumo_market_macro.css"):
            assert name in page and client.get("/static/" + name).status_code == 200


def test_macro_rejects_invalid_frequency_window_and_nonfinite_scenario(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        for params in ({"factor": "m2"}, {"window": "bad"}, {"earnings": "nan"}, {"duration": "100"}, {"frequency": "monthly", "lag": "5"}):
            assert client.get("/api/market-health/macro", query_params=params).status_code == 400


def test_limits_query_detail_refresh_and_packaged_assets(runtime, monkeypatch):
    from robyn.testing import TestClient
    calls=[]
    monkeypatch.setattr(runtime.market_limits_service,"query",lambda jobs,**kw: {"available":False,**kw})
    monkeypatch.setattr(runtime.market_limits_service,"detail",lambda kind,code,jobs,**kw: {"kind":kind,"code":code,**kw})
    monkeypatch.setattr(runtime.market_limits_service,"start_refresh",lambda jobs,**kw: calls.append(kw) or {"id":"limits-test","status":"queued"})
    params={"start":"2026-10-01","end":"2026-10-30"}
    with TestClient(runtime.app) as client:
        assert client.get("/api/market-health/limits",query_params=params).json()["start"] == params["start"]
        assert client.get("/api/market-health/limits/detail",query_params={**params,"kind":"sector","code":"BK0001"}).json()["code"] == "BK0001"
        result=client.post("/api/market-health/limits/refresh",json_data=params)
        assert result.status_code==202 and calls==[params]
        page=client.get("/desktop/market_health").text
        assert 'id="mhView-limits"' in page and 'id="mlStart"' in page
        for name in ("lumo_market_limits.js","lumo_market_limits.css"):
            assert name in page and client.get("/static/"+name).status_code==200


def test_limits_routes_reject_invalid_date_and_detail_codes(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        for path in ("/api/market-health/limits","/api/market-health/limits/detail"):
            assert client.get(path,query_params={"start":"bad","kind":"stock","code":"../../"}).status_code==400
        assert client.post("/api/market-health/limits/refresh",json_data={"start":"bad"}).status_code==400
