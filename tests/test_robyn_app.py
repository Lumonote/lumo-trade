import importlib
import sys

import pytest


@pytest.fixture()
def robyn_module(tmp_path, monkeypatch):
    monkeypatch.setenv("KRONOS_USER_DIR", str(tmp_path))
    sys.modules.pop("webui.robyn_app", None)
    sys.modules.pop("webui.core", None)
    module = importlib.import_module("webui.robyn_app")
    yield module
    sys.modules.pop("webui.robyn_app", None)
    sys.modules.pop("webui.core", None)


def test_robyn_route_manifest_contains_native_routes(robyn_module):
    manifest = {
        (route["method"], route["route"])
        for route in robyn_module.get_route_manifest()
    }
    native_manifest = {
        (route["method"], route["route"])
        for route in robyn_module.get_native_route_manifest()
    }

    expected_native = {
        ("GET", "/"),
        ("GET", "/desktop"),
        ("GET", "/api/jobs"),
        ("GET", "/api/stock-kline/:stock_code"),
        ("GET", "/api/stock-context/:stock_code"),
        ("POST", "/api/opportunity-discovery/start"),
        ("POST", "/api/batch-analysis/start"),
        ("POST", "/api/pattern-search/match"),
        ("POST", "/api/pattern-search/refresh"),
        ("GET", "/api/trading-clients"),
        ("GET", "/api/snapshot"),
        ("GET", "/api/settings"),
        ("POST", "/api/settings/llm"),
        ("POST", "/api/settings/tushare"),
        ("POST", "/api/predict"),
        ("POST", "/api/load-model"),
    }
    assert expected_native <= manifest
    assert expected_native <= native_manifest
    assert manifest == native_manifest


def test_watchlist_storage_error_is_not_a_successful_empty_response(robyn_module):
    from robyn.testing import TestClient

    service = robyn_module.webui_core.WATCHLIST_SERVICE
    service.store_path.write_text('{"items": [')
    original = service.store_path.read_bytes()
    with TestClient(robyn_module.app) as client:
        responses = [client.get("/api/watchlist"), client.get("/api/watchlist/alerts")]
        for action in ("add", "remove", "pin"):
            responses.append(client.post(f"/api/watchlist/{action}", json_data={"code": "600519"}))
    for response in responses:
        assert response.status_code == 503
        assert response.json()["error_code"] == "watchlist_storage_unavailable"
        assert "items" not in response.json()
    assert service.store_path.read_bytes() == original


def test_watchlist_api_recovers_saved_list_with_notice(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    service = robyn_module.webui_core.WATCHLIST_SERVICE
    monkeypatch.setattr(service, "quotes", lambda codes: {})
    monkeypatch.setattr(service, "_sector_info", lambda code: {})
    with TestClient(robyn_module.app) as client:
        assert client.post("/api/watchlist/add", json_data={"code": "600519", "name": "贵州茅台"}).status_code == 200
        assert client.post("/api/watchlist/pin", json_data={"code": "600519"}).status_code == 200
        service.store_path.write_text("damaged")
        response = client.get("/api/watchlist")
    assert response.status_code == 200
    assert response.json()["items"][0]["code"] == "600519"
    assert response.json()["items"][0]["pinned"] is True
    assert "备份恢复" in response.json()["storage_notice"]


def test_robyn_native_json_routes(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    monkeypatch.setattr(
        robyn_module.webui_core,
        "_get_stock_kline_payload",
        lambda code, period="daily", limit=120: (
            {
                "code": code,
                "name": "贵州茅台",
                "source": "test",
                "available": True,
                "records": [
                    {"date": "2026-01-01", "open": 1, "close": 1.2, "high": 1.3, "low": 0.9, "volume": 100},
                    {"date": "2026-01-02", "open": 1.2, "close": 1.1, "high": 1.4, "low": 1.0, "volume": 120},
                ],
            },
            None,
        ),
    )
    monkeypatch.setattr(
        robyn_module.webui_core.TRADING_CLIENT_SERVICE,
        "discover_clients",
        lambda refresh=False: {"platform": "test", "clients": [], "generated_at": "test"},
    )

    with TestClient(robyn_module.app) as client:
        jobs_response = client.get("/api/jobs")
        status_response = client.get("/api/model-status")
        models_response = client.get("/api/available-models")
        context_response = client.get("/api/stock-context/600519", query_params={"name": "贵州茅台"})
        match_response = client.post(
            "/api/pattern-search/match",
            json_data={"curve": [1, 2, 3]},
        )

    assert jobs_response.status_code == 200
    assert jobs_response.json() == {"jobs": []}
    assert status_response.status_code == 200
    assert "available" in status_response.json()
    assert models_response.status_code == 200
    assert "kronos-small" in models_response.json()["models"]
    assert context_response.status_code == 200
    assert context_response.json()["stock"]["code"] == "600519"
    assert match_response.status_code == 400
    assert "curve 必须" in match_response.json()["error"]


def test_robyn_native_template_static_and_path_params(robyn_module):
    from robyn.testing import TestClient

    with TestClient(robyn_module.app) as client:
        desktop_response = client.get("/desktop")
        missing_page_response = client.get("/desktop/missing")
        static_response = client.get("/static/lumo_desktop.css")
        bad_kline_response = client.get("/api/stock-kline/bad")
        bad_context_response = client.get("/api/stock-context/bad")
        missing_job_response = client.get("/api/jobs/missing")

    assert desktop_response.status_code == 200
    assert b"Lumo Trade" in desktop_response.content
    assert missing_page_response.status_code == 404
    assert static_response.status_code == 200
    assert b"app-shell" in static_response.content
    assert bad_kline_response.status_code == 400
    assert bad_kline_response.json() == {"error": "Invalid stock code"}
    assert bad_context_response.status_code == 400
    assert bad_context_response.json() == {"error": "Invalid stock code"}
    assert missing_job_response.status_code == 404
    assert missing_job_response.json() == {"error": "Job not found"}


def test_robyn_native_query_params(robyn_module):
    from robyn.testing import TestClient

    with TestClient(robyn_module.app) as client:
        stocks_response = client.get(
            "/api/pattern-search/stocks",
            query_params={"q": "", "limit": "2"},
        )
        trading_response = client.get(
            "/api/trading-clients",
            query_params={"refresh": "true"},
        )

    assert stocks_response.status_code == 200
    assert stocks_response.json() == {"stocks": [], "count": 0}
    assert trading_response.status_code == 200
    assert "clients" in trading_response.json()


def test_robyn_stock_dashboard_refresh_query_forces_market_refresh(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    seen = []

    def fake_dashboard(force_market_refresh=False):
        seen.append(force_market_refresh)
        return {"intelligence": {"eastmoney": {"concept_boards": []}}}

    monkeypatch.setattr(robyn_module.webui_core, "_build_market_dashboard", fake_dashboard)
    monkeypatch.setattr(robyn_module.webui_core, "_load_latest_opportunities", lambda: {})
    monkeypatch.setattr(robyn_module.webui_core, "_load_batch_summary", lambda: {})
    monkeypatch.setattr(robyn_module.webui_core, "_load_report_history", lambda: [])
    monkeypatch.setattr(robyn_module.webui_core, "_module_health", lambda: {})
    monkeypatch.setattr(robyn_module.webui_core, "_get_job_snapshot", lambda: [])

    with TestClient(robyn_module.app) as client:
        normal_response = client.get("/api/stock-dashboard")
        refresh_response = client.get("/api/stock-dashboard", query_params={"refresh": "1"})

    assert normal_response.status_code == 200
    assert refresh_response.status_code == 200
    assert seen == [False, True]


def test_robyn_native_job_start_validation(robyn_module):
    from robyn.testing import TestClient

    with TestClient(robyn_module.app) as client:
        bad_batch_response = client.post("/api/batch-analysis/start", json_data={})
        bad_trading_response = client.post("/api/trading-clients/open", json_data={})

    assert bad_batch_response.status_code == 400
    assert "stock code" in bad_batch_response.json()["error"]
    assert bad_trading_response.status_code == 400
    assert bad_trading_response.json() == {"success": False, "error": "参数不完整"}


def test_robyn_desktop_discovery_live_page(robyn_module):
    from robyn.testing import TestClient

    with TestClient(robyn_module.app) as client:
        resp = client.get("/desktop/discovery_live")

    assert resp.status_code == 200
    assert b'id="dlvRoot"' in resp.content


def test_robyn_markdown_report_served_with_utf8_charset(robyn_module):
    """.md 报告必须带 charset=utf-8(曾以 octet-stream 直出导致中文乱码)。"""
    from robyn.testing import TestClient

    results_dir = robyn_module.webui_core.RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "opportunity_top10_test.md").write_text("# 测试报告\n中文内容", encoding="utf-8")

    with TestClient(robyn_module.app) as client:
        resp = client.get("/analysis-reports/results/opportunity_top10_test.md")

    assert resp.status_code == 200
    ctype = "".join(resp.headers.get("content-type") or resp.headers.get("Content-Type") or "")
    assert "charset=utf-8" in ctype.lower(), ctype
    assert "测试报告" in resp.text


def test_robyn_native_settings_routes(robyn_module):
    from robyn.testing import TestClient

    with TestClient(robyn_module.app) as client:
        settings_response = client.get("/api/settings")
        llm_response = client.post(
            "/api/settings/llm",
            json_data={"enabled_models": ["Provider/Model"], "api_keys": {"Provider": "sk-test"}},
        )
        tushare_response = client.post(
            "/api/settings/tushare",
            # 用真实形状的假 Token:占位值 12345678901234567890 会被
            # _looks_like_placeholder_token 判为未配置(configured=False),
            # 且历史上曾因环境泄漏被写进真实配置、清掉用户真 Token。
            json_data={"token": "f" * 40, "timeout": 45, "retry_count": 2},
        )

    assert settings_response.status_code == 200
    assert "llm" in settings_response.json()
    assert llm_response.status_code == 200
    assert llm_response.json()["success"] is True
    assert tushare_response.status_code == 200
    assert tushare_response.json()["settings"]["tushare"]["configured"] is True


def test_robyn_native_prediction_validation(robyn_module, tmp_path):
    from robyn.testing import TestClient

    data_file = tmp_path / "sample.csv"
    data_file.write_text(
        "timestamps,open,high,low,close,volume\n"
        "2026-01-01,1,2,0.5,1.5,100\n"
        "2026-01-02,1.5,2.5,1,2,120\n",
        encoding="utf-8",
    )

    with TestClient(robyn_module.app) as client:
        missing_path_response = client.post("/api/predict", json_data={})
        not_loaded_response = client.post(
            "/api/predict",
            json_data={"file_path": str(data_file), "lookback": 1, "pred_len": 1},
        )

    assert missing_path_response.status_code == 400
    assert missing_path_response.json() == {"error": "File path cannot be empty"}
    assert not_loaded_response.status_code == 400
    assert not_loaded_response.json() == {"error": "Kronos model not loaded, please load model first"}


def test_robyn_stock_analysis_suite_get(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    captured = {}

    class _StubSvc:
        def get_suite(self, code, name="", **kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "stock": {"code": code, "name": name, "market": "XSHE", "sector": "—"},
                "warnings": [],
            }

    monkeypatch.setattr(robyn_module.webui_core, "STOCK_SUITE_SERVICE", _StubSvc())
    monkeypatch.setattr(
        robyn_module.webui_core,
        "_latest_stock_quote",
        lambda _code: {"price": 66.66, "source": "tencent"},
    )

    client = TestClient(robyn_module.app)
    response = client.get("/api/stock-analysis-suite/000001?name=平安银行")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["stock"]["code"] == "000001"
    assert body["stock"]["name"] == "平安银行"
    assert captured["current_price"] == 66.66


def test_robyn_stock_analysis_suite_capital_rankings_get(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    class _StubSvc:
        def get_capital_rankings(self, code, **kwargs):
            return {
                "success": True,
                "capital_rankings": {
                    "code": code,
                    "days": kwargs.get("days"),
                    "start_date": kwargs.get("start_date"),
                    "end_date": kwargs.get("end_date"),
                },
            }

    monkeypatch.setattr(robyn_module.webui_core, "STOCK_SUITE_SERVICE", _StubSvc())

    client = TestClient(robyn_module.app)
    response = client.get(
        "/api/stock-capital-rankings/000001"
        "?days=20&start_date=2026-06-01&end_date=2026-06-04"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["capital_rankings"] == {
        "code": "000001",
        "days": 20,
        "start_date": "2026-06-01",
        "end_date": "2026-06-04",
    }


def test_robyn_stock_analysis_suite_invalid_code(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    class _ErrSvc:
        def get_suite(self, code, name="", **_kwargs):
            raise ValueError(f"invalid stock code: {code!r}")

    monkeypatch.setattr(robyn_module.webui_core, "STOCK_SUITE_SERVICE", _ErrSvc())
    client = TestClient(robyn_module.app)
    response = client.get("/api/stock-analysis-suite/INVALID")
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert "invalid" in body["error"]


def test_robyn_stock_analysis_suite_ai_post_success(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    captured = {}

    class _StubSvc:
        def trigger_ai_interpretation(self, code, name="", model_full_key=None, force_refresh=False):
            captured["code"] = code
            captured["name"] = name
            captured["model_full_key"] = model_full_key
            captured["force_refresh"] = force_refresh
            return {
                "success": True,
                "status": "ready",
                "report": "## 1. 核心定性\n平安银行...",
                "token_usage": 3651,
                "generated_at": "2026-05-23T14:32:05",
                "cached_path": "reports/stock_suite/000001_20260523_143205.md",
            }

    monkeypatch.setattr(robyn_module.webui_core, "STOCK_SUITE_SERVICE", _StubSvc())

    with TestClient(robyn_module.app) as client:
        response = client.post(
            "/api/stock-analysis-suite/000001/ai",
            json_data={"name": "平安银行", "force_refresh": True, "model_full_key": "DeepSeek/v3"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["status"] == "ready"
    assert body["token_usage"] == 3651
    assert captured == {
        "code": "000001",
        "name": "平安银行",
        "model_full_key": "DeepSeek/v3",
        "force_refresh": True,
    }


def test_robyn_stock_analysis_suite_ai_post_invalid_code(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    class _ErrSvc:
        def trigger_ai_interpretation(self, code, name="", model_full_key=None, force_refresh=False):
            raise ValueError(f"invalid stock code: {code!r}")

    monkeypatch.setattr(robyn_module.webui_core, "STOCK_SUITE_SERVICE", _ErrSvc())

    with TestClient(robyn_module.app) as client:
        response = client.post(
            "/api/stock-analysis-suite/INVALID/ai",
            json_data={},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert "invalid" in body["error"]


def test_diagnostics_data_sources_returns_summary(robyn_module, tmp_path, monkeypatch):
    """/api/diagnostics/data-sources 返回最近 24h sync_log 摘要。"""
    from robyn.testing import TestClient

    monkeypatch.setenv("KRONOS_SQLITE_PATH", str(tmp_path / "diag.sqlite"))
    from data_store import connection, sync_log_repo

    connection.reset_for_testing()
    try:
        import datetime as _dt
        now = _dt.datetime.now()
        recent = (now - _dt.timedelta(hours=1)).isoformat(timespec="seconds")
        recent2 = (now - _dt.timedelta(hours=2)).isoformat(timespec="seconds")
        sync_log_repo.append("lhb", "000001.SZ", recent, "ok", rows=42)
        sync_log_repo.append("hsgt", "", recent2, "failed", error="429")

        with TestClient(robyn_module.app) as client:
            response = client.get("/api/diagnostics/data-sources")

        assert response.status_code == 200
        body = response.json()
        assert "lhb" in body
        assert body["lhb"]["ok"] == 1
        assert "hsgt" in body
        assert body["hsgt"]["failed"] == 1
    finally:
        connection.reset_for_testing()


def test_panel_overlay_endpoint_returns_overlay(robyn_module, monkeypatch):
    """POST /…/panel-overlay 返回 {success, overlay, merged_panel}。service 层 stub（经 robyn_module.webui_core）。"""
    from robyn.testing import TestClient

    fake = {
        "success": True,
        "overlay": {"reviewed": True, "tier": "deep", "data_status": "fresh",
                    "great_divide_override": {"punchline": "多头占优"}, "risks": ["x"],
                    "panel_insights": {}, "buy_zones": {"value": [], "growth": [],
                    "technical": [], "youzi": []}, "narrative_override": None,
                    "last_updated": "2026-05-30T14:00:00", "reason": None},
        "merged_panel": {"great_divide": {"punchline": "多头占优"}},
    }
    monkeypatch.setattr(robyn_module.webui_core.STOCK_SUITE_SERVICE, "trigger_panel_overlay",
                        lambda code, tier="deep", force_refresh=False: fake)

    with TestClient(robyn_module.app) as client:
        resp = client.post("/api/stock-analysis-suite/000001/panel-overlay",
                           json_data={"tier": "deep"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["overlay"]["reviewed"] is True
    assert body["merged_panel"]["great_divide"]["punchline"] == "多头占优"


def test_panel_overlay_endpoint_bad_code_returns_400(robyn_module, monkeypatch):
    from robyn.testing import TestClient

    def _raise(code, tier="deep", force_refresh=False):
        raise ValueError("bad code")

    monkeypatch.setattr(robyn_module.webui_core.STOCK_SUITE_SERVICE, "trigger_panel_overlay", _raise)

    with TestClient(robyn_module.app) as client:
        resp = client.post("/api/stock-analysis-suite/zzz/panel-overlay", json_data={"tier": "deep"})

    assert resp.status_code == 400


def test_configure_server_defaults_to_concurrent_workers(robyn_module, monkeypatch):
    """workers=1时所有同步handler串行：一个8s的Sina K线请求会卡住整页导航。"""
    monkeypatch.delenv("ROBYN_WORKERS", raising=False)

    robyn_module.configure_server_from_env()

    assert robyn_module.app.config.workers >= 4
    assert robyn_module.app.config.processes == 1


def test_configure_server_respects_workers_env(robyn_module, monkeypatch):
    monkeypatch.setenv("ROBYN_WORKERS", "2")

    robyn_module.configure_server_from_env()

    assert robyn_module.app.config.workers == 2
