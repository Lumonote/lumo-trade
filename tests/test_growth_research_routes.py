"""原生桌面入口与成长研究HTTP契约，不访问外网。"""
import importlib
import sys

import pytest


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setenv("KRONOS_USER_DIR",str(tmp_path))
    monkeypatch.setenv("KRONOS_SQLITE_PATH",str(tmp_path/"research.sqlite"))
    monkeypatch.setenv("KRONOS_DISABLE_TORCH","1")
    sys.modules.pop("webui.robyn_app",None);sys.modules.pop("webui.core",None)
    from data_store.connection import reset_for_testing
    reset_for_testing()
    module=importlib.import_module("webui.robyn_app")
    yield module
    sys.modules.pop("webui.robyn_app",None);sys.modules.pop("webui.core",None)
    reset_for_testing()


def test_menu_replaced_and_legacy_url_resolves(runtime):
    from robyn.testing import TestClient
    assert "star_orbit" not in runtime.webui_core.DESKTOP_PAGES
    assert runtime.webui_core.resolve_desktop_page("star_orbit")=="growth_research"
    with TestClient(runtime.app) as client:
        for url in ("/desktop/growth_research","/desktop/star_orbit"):
            response=client.get(url)
            assert response.status_code==200
            assert "成长研究" in response.text
            assert 'id="growthResearchRoot"' in response.text
            assert 'id="orbitSvgWrap"' not in response.text
            assert "lumo_growth_research.js" in response.text


def test_query_routes_validation_and_export(runtime,monkeypatch):
    from robyn.testing import TestClient
    service=runtime.growth_research_service
    monkeypatch.setattr(service.repo,"read",lambda key:None)
    with TestClient(runtime.app) as client:
        empty=client.get("/api/growth-research")
        invalid=client.get("/api/growth-research",query_params={"sort":"invalid"})
        bad_refresh=client.post("/api/growth-research/refresh",json_data={"mode":"delete"})
        export=client.get("/api/growth-research/export")
    assert empty.status_code==200 and empty.json()["ready"] is False
    assert empty.json()["stocks"]==[]
    assert invalid.status_code==400 and bad_refresh.status_code==400
    assert export.status_code==200 and "text/csv" in export.headers["content-type"]


def test_background_update_reuses_shared_job_service(runtime,monkeypatch):
    from robyn.testing import TestClient
    jobs=runtime.webui_core.JOB_SERVICE;calls=[]
    monkeypatch.setattr(runtime.growth_research_service,"start_refresh",lambda mode,runner:calls.append((mode,runner)) or {"id":"growth-test","status":"queued"})
    with TestClient(runtime.app) as client:
        response=client.post("/api/growth-research/refresh",json_data={"mode":"financials"})
    assert response.status_code==202
    assert calls==[("financials",jobs)]
    assert response.json()["job"]["id"]=="growth-test"


def test_snapshot_and_inputs_persist_without_schema_changes(tmp_path,monkeypatch):
    from data_store import growth_research_repo as repo
    from data_store.connection import reset_for_testing
    monkeypatch.setenv("KRONOS_SQLITE_PATH",str(tmp_path/"growth.sqlite"));reset_for_testing()
    try:
        repo.write("snapshot",{"as_of":"2026-10-14","stocks":[{"code":"000001"}]})
        reset_for_testing()
        assert repo.read("snapshot")["stocks"][0]["code"]=="000001"
        assert repo.local_inputs()["market"]==[]
    finally:reset_for_testing()
