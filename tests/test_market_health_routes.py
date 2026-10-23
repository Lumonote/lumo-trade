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
