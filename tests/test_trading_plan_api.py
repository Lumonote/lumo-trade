import importlib
import sys

import pytest

from tests.test_trading_plan_service import service


@pytest.fixture
def module(tmp_path, monkeypatch):
    monkeypatch.setenv("KRONOS_USER_DIR", str(tmp_path))
    monkeypatch.setenv("KRONOS_DISABLE_QUANT_RADAR_AUTOSAVE", "1")
    sys.modules.pop("webui.core", None)
    sys.modules.pop("webui.robyn_app", None)
    app = importlib.import_module("webui.robyn_app")
    monkeypatch.setattr(app.webui_core, "TRADING_PLAN_SERVICE", service(tmp_path))
    yield app
    sys.modules.pop("webui.core", None)
    sys.modules.pop("webui.robyn_app", None)


def test_get_plan_lock_and_reload_routes(module):
    from robyn.testing import TestClient

    with TestClient(module.app) as client:
        response = client.get("/api/trading-plans", query_params={"code": "000001"})
        assert response.status_code == 200
        item = response.json()["items"][0]
        body = {"code": item["code"], "basis_key": item["candidate"]["basis_key"],
                "expected_revision": item["revision"]}
        assert client.post("/api/trading-plans/lock", json_data=body).status_code == 200
        assert client.post("/api/trading-plans/lock", json_data=body).status_code == 409
        assert client.get("/api/trading-plans", query_params={"code": "000001"}).json()["items"][0]["locked"]


def test_invalid_requests_cannot_mutate_files(module):
    from robyn.testing import TestClient

    with TestClient(module.app) as client:
        assert client.get("/api/trading-plans", query_params={"code": "../file"}).status_code == 400
        assert client.get("/api/trading-plans", query_params={"scope": "invalid"}).status_code == 400
        assert client.post("/api/trading-plans/lock", json_data={"code": "000001"}).status_code == 400
    assert not module.webui_core.TRADING_PLAN_SERVICE.store_path.exists()


def test_command_center_uses_real_benchmark_returns(module, monkeypatch):
    monkeypatch.setattr(module.webui_core.MARKET_PULSE_SERVICE, "payload", lambda: {
        "benchmark": {"chg_5d": -4.2, "chg_20d": -6.5}})
    assert module.webui_core._cc_market_env() == {"hs300_ret_5d": -4.2, "hs300_ret_20d": -6.5}


def test_desktop_template_loads_plans_for_watchlist_and_stock(module):
    from robyn.testing import TestClient

    with TestClient(module.app) as client:
        response = client.get("/desktop/watchlist")
    assert response.status_code == 200
    assert 'id="tradingPlanBoard"' in response.text
    assert 'id="tradingPlanStock"' in response.text
    assert "/static/lumo_trading_plan.js?v=" in response.text
