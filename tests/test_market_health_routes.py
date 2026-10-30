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
