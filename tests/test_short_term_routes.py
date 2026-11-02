"""短线大屏独立页面、全局入口与原生API契约。"""
import importlib
import sys
from unittest.mock import patch

import pytest


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root=tmp_path_factory.mktemp("short_term_routes")
    with patch.dict("os.environ",{"KRONOS_USER_DIR":str(root),"KRONOS_SQLITE_PATH":str(root/"test.sqlite"),"KRONOS_DISABLE_TORCH":"1"}):
        from data_store.connection import reset_for_testing
        reset_for_testing();sys.modules.pop("webui.robyn_app",None);sys.modules.pop("webui.core",None)
        app=importlib.import_module("webui.robyn_app")
        yield app
        reset_for_testing();sys.modules.pop("webui.robyn_app",None);sys.modules.pop("webui.core",None)


def test_global_entry_and_standalone_screen_assets(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        for page in ("features","market_health","watchlist"):
            response=client.get("/desktop/"+page)
            assert response.status_code==200 and 'id="shortTermScreenBtn"' in response.text
            assert "lumo_short_term_launcher.js" in response.text
        response=client.get("/desktop/short_term")
        assert response.status_code==200 and 'id="stScreen"' in response.text and 'id="stReturn"' in response.text
        assert 'class="sidebar"' not in response.text
        for removed in ('id="stSources"', 'id="stExport"', 'class="st-brand"', 'class="st-session"', 'data-st-mode='):
            assert removed not in response.text
        for panel in ('id="stUpList"','id="stDownList"','id="stHotList"','id="stRapidList"'):
            assert panel in response.text
        for name in ("lumo_short_term_launcher.js","lumo_short_term_screen.js","lumo_short_term_scene.js","lumo_short_term_scene_fallback.js","lumo_short_term_screen.css","vendor/three-r180/three.module.min.js","vendor/three-r180/three.core.min.js"):
            assert client.get("/static/"+name).status_code==200


def test_short_term_native_aggregate_section_detail_and_refresh(runtime,monkeypatch):
    from robyn.testing import TestClient
    monkeypatch.setattr(runtime.short_term_screen_service,"query",lambda jobs,date="":{"today":"2026-11-01","date":date})
    monkeypatch.setattr(runtime.short_term_screen_service,"section",lambda kind,jobs,**kw:{"kind":kind,**kw})
    monkeypatch.setattr(runtime.short_term_screen_service,"detail",lambda kind,ident,jobs,**kw:{"kind":kind,"id":ident,**kw})
    monkeypatch.setattr(runtime.short_term_screen_service,"start_refresh",lambda jobs,intelligence:{"id":"short-test","status":"queued"})
    with TestClient(runtime.app) as client:
        assert client.get("/api/short-term/overview",query_params={"date":"2026-10-30"}).json()["date"]=="2026-10-30"
        assert client.get("/api/short-term/section",query_params={"kind":"calendar","q":"峰会"}).json()["keyword"]=="峰会"
        assert client.get("/api/short-term/detail",query_params={"kind":"stock","id":"600001"}).json()["id"]=="600001"
        response=client.post("/api/short-term/refresh",json_data={})
        assert response.status_code==202 and response.json()["job"]["id"]=="short-test"


def test_invalid_short_term_query_is_400(runtime):
    from robyn.testing import TestClient
    with TestClient(runtime.app) as client:
        assert client.get("/api/short-term/overview",query_params={"date":"invalid"}).status_code==400
        assert client.get("/api/short-term/section",query_params={"kind":"invalid"}).status_code==400
        assert client.get("/api/short-term/detail",query_params={"kind":"invalid","id":"anything"}).status_code==400
