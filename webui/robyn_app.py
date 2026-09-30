"""Robyn entry point for the WebUI runtime.

Robyn owns the HTTP runtime. All routes are implemented as native Robyn
handlers backed by shared services in ``webui.core``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, unquote_plus

from jinja2 import Environment, FileSystemLoader, select_autoescape
from robyn import ALLOW_CORS, Headers, Request, Response, Robyn
from robyn.argument_parser import Config

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 用点号形式而非 `from webui import core`:后者经由父包属性解析,测试里
# pop 掉 sys.modules["webui.core"] 重载时会拿到陈旧模块(其 CONFIGURATION_SERVICE
# 仍绑定真实用户目录),曾把测试占位 Token 写进真实 tushare_config.json。
import webui.core as webui_core
from webui.parent_watchdog import start_parent_watchdog
from webui.services.model_runtime import load_model_payload, loaded_model_info, run_prediction_payload
from webui.services import futures_service, quant_radar_service, star_orbit_service
from webui.services import license_service
from webui.services import stock_screener_service
from data_store import quant_radar_repo, star_orbit_repo


def _robyn_config() -> Config:
    config = Config()
    config.log_level = os.environ.get("ROBYN_LOG_LEVEL", "WARN")
    return config


app = Robyn(__file__, config=_robyn_config())
ALLOW_CORS(app, "*")

TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(str(Path(__file__).resolve().parent / "templates")),
    autoescape=select_autoescape(("html", "xml")),
)

# 静态资源版本号(文件 mtime)：桌面页 HTML 走 no-store 始终最新，但其 ~250KB 的内联 JS 拆成
# /static/lumo_desktop_app.js 后用 ?v=<mtime> 做强缓存——内容不变则 WKWebView 复用已解析的脚本，
# 切换左侧菜单不再每次重新下载+解析整份 JS；文件一改 mtime 变化，URL 即自动失效。
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version(filename: str) -> str:
    try:
        return str(int((_STATIC_DIR / filename).stat().st_mtime))
    except OSError:
        return "0"


TEMPLATE_ENV.globals["asset_v"] = _asset_version
NATIVE_ROUTE_KEYS: set[tuple[str, str]] = set()


def _native_route(method: str, route: str):
    def decorator(handler):
        normalized_method = method.upper()
        app.add_route(normalized_method, route, handler)
        NATIVE_ROUTE_KEYS.add((normalized_method, route))
        return handler

    return decorator


def _native_get(route: str):
    return _native_route("GET", route)


def _native_post(route: str):
    return _native_route("POST", route)


def _safe_unquote_plus(value: Any) -> str:
    text = "" if value is None else str(value)
    try:
        return unquote_plus(text)
    except Exception:
        return text


def _iter_query_pairs(query_params: Any) -> list[tuple[str, str]]:
    if query_params is None:
        return []
    try:
        query_dict = query_params.to_dict()
    except AttributeError:
        query_dict = {}

    pairs: list[tuple[str, str]] = []
    for key, value in query_dict.items():
        decoded_key = _safe_unquote_plus(key)
        if isinstance(value, list):
            pairs.extend((decoded_key, _safe_unquote_plus(item)) for item in value)
        else:
            pairs.append((decoded_key, _safe_unquote_plus(value)))
    return pairs


def _raw_request_path(request: Request) -> str:
    path = getattr(getattr(request, "url", None), "path", "") or "/"
    return path if path.startswith("/") else f"/{path}"


def _split_path_query(path: str) -> tuple[str, list[tuple[str, str]]]:
    clean_path, separator, query_string = str(path or "/").partition("?")
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    query_pairs = parse_qsl(query_string, keep_blank_values=True) if separator else []
    return clean_path or "/", [(str(key), str(value)) for key, value in query_pairs]


def _request_query_pairs(request: Request) -> list[tuple[str, str]]:
    _path, path_pairs = _split_path_query(_raw_request_path(request))
    return path_pairs + _iter_query_pairs(request.query_params)


def _query_value(request: Request, name: str, default: Any = None) -> Any:
    for key, value in _request_query_pairs(request):
        if key == name:
            return value
    return default


def _path_param(request: Request, name: str, fallback: Any = None) -> str:
    value = fallback
    path_params = getattr(request, "path_params", None)
    if value is None and isinstance(path_params, dict):
        value = path_params.get(name)
    value = "" if value is None else str(value)
    raw = value.split("?", 1)[0]
    try:
        return unquote(raw)
    except Exception:
        return raw


def _request_json(request: Request) -> dict[str, Any]:
    body = _request_body(request)
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not body:
        return {}
    try:
        payload = json.loads(str(body))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_response(payload: Any, status_code: int = 200) -> Response:
    safe_payload = webui_core._json_safe(payload)
    return Response(
        status_code=status_code,
        headers=Headers({"Content-Type": "application/json; charset=utf-8"}),
        description=json.dumps(safe_payload, ensure_ascii=False, default=str),
    )


def _text_response(text: str, status_code: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
    return Response(
        status_code=status_code,
        headers=Headers({"Content-Type": content_type}),
        description=text,
    )


def _html_response(html: str, status_code: int = 200) -> Response:
    return _text_response(html, status_code=status_code, content_type="text/html; charset=utf-8")


def _render_template(template_name: str, **context: Any) -> Response:
    html = TEMPLATE_ENV.get_template(template_name).render(**context)
    # 应用外壳页面禁用缓存：Tauri 用 WKWebView，桌面页 URL 固定为 http://127.0.0.1:7070/desktop，
    # 不加 no-store 时升级新构建后 WKWebView 会回放旧构建的缓存页
    # （曾导致已改为 ${analysts.length} 的源码仍持续显示「投资人评审团（51）」）。
    return Response(
        status_code=200,
        headers=Headers({
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }),
        description=html,
    )


def _safe_child_path(root: Path, relative_path: str) -> Path | None:
    root_path = root.resolve()
    candidate = (root_path / str(relative_path).replace("\\", "/")).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _file_response(path: Path | None, cache_immutable: bool = False, attachment: bool = False) -> Response:
    if path is None:
        return _text_response("Not Found", status_code=404)
    content_type = mimetypes.guess_type(path.name)[0]
    if content_type is None:
        # .md 在多数环境 guess 不出 → 曾以 application/octet-stream 直出,WKWebView
        # 猜编码把中文渲成乱码;显式给 markdown 类型,其余未知类型保持二进制。
        content_type = "text/markdown" if path.suffix.lower() in (".md", ".markdown") else "application/octet-stream"
    if content_type.startswith("text/") or content_type == "application/json":
        content_type += "; charset=utf-8"
    headers: dict[str, str] = {"Content-Type": content_type}
    if attachment:
        headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    if cache_immutable:
        # 带 ?v=<版本> 的静态资源按内容版本强缓存：版本不变即永久命中(不再下载/重解析)，
        # 版本变化(文件 mtime 改变)即换 URL 自动失效。仅作用于显式带版本号的请求，
        # 无版本号的旧资源维持原有(无显式缓存头)行为，避免误缓存正在迭代的 CSS。
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return Response(
        status_code=200,
        headers=Headers(headers),
        description=path.read_bytes(),
    )


def _request_body(request: Request) -> bytes | str:
    body = getattr(request, "body", b"")
    if body is None:
        return b""
    return body


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 设备验证门禁：打包态(或 KRONOS_LICENSE_REQUIRED=1)下，激活通过前拦截全部功能。
# 老启动器 lumo_modern_gui 的验证从未接入 Tauri 打包链，这里在 HTTP 层补上：
# 页面 302 → /activate，API 403，激活面(激活页/授权API/静态资源)放行。
#
# [已按需求注释关闭] 2026-09-11
# 桌面端不再做设备验证，本钩子直接放行；开关侧 webui/services/license_service.py
# 的 license_required() 也一并返回 False。恢复时需把两处一起还原
# (取消下面 _license_gate 的注释，并还原 license_required())。
# 注意: /activate 页面与 /api/license/* 接口保留不动，激活能力仍可用，
# 只是不再强制拦截。
# ---------------------------------------------------------------------------

_LICENSE_ALLOWED_PREFIXES = ("/api/license/", "/static/", "/assets/")
_LICENSE_ALLOWED_PATHS = {"/activate", "/favicon.ico"}


def _license_path_allowed(path: str) -> bool:
    return path in _LICENSE_ALLOWED_PATHS or path.startswith(_LICENSE_ALLOWED_PREFIXES)


@app.before_request()
def _license_gate(request: Request):
    # --- 原门禁拦截逻辑(注释保留, 恢复时取消注释) ---
    # if not license_service.license_required():
    #     return request
    # path, _query = _split_path_query(_raw_request_path(request))
    # method = str(getattr(request, "method", "GET") or "GET").upper()
    # if method == "OPTIONS" or _license_path_allowed(path):
    #     return request
    # if license_service.is_activated():
    #     return request
    # if path.startswith("/api/"):
    #     return _json_response(
    #         {"error": "license_required", "message": "设备未激活授权，请先完成设备验证"},
    #         status_code=403,
    #     )
    # return Response(
    #     status_code=302,
    #     headers=Headers({"Location": "/activate"}),
    #     description="",
    # )

    # 桌面端设备验证已关闭: 一律放行
    return request


@_native_get("/activate")
def activate_page(request: Request) -> Response:
    return _render_template("activation.html", status=license_service.activation_status())


@_native_get("/api/license/status")
def api_license_status(request: Request) -> Response:
    return _json_response(license_service.activation_status())


@_native_post("/api/license/activate")
def api_license_activate(request: Request) -> Response:
    body = _request_json(request)
    ok, message = license_service.activate(body.get("license_code", ""))
    payload: dict[str, Any] = {"success": ok, "message": message}
    if ok:
        payload["status"] = license_service.activation_status()
    return _json_response(payload, status_code=200 if ok else 400)


def _model_status_payload() -> dict[str, Any]:
    disabled = os.environ.get("KRONOS_DISABLE_TORCH", "0").lower() in {"1", "true", "yes"}
    bundle_mode = os.environ.get("KRONOS_BACKEND_BUNDLE_MODE", "source")
    if not webui_core.MODEL_AVAILABLE:
        message = "Kronos model library not available, please install related dependencies"
        if disabled:
            message = "当前桌面 lite 包未内置 Kronos/PyTorch 推理依赖，请使用 full backend 构建或源码环境载入模型"
        return {
            "available": False,
            "loaded": False,
            "message": message,
            "bundle_mode": bundle_mode,
            "torch_disabled": disabled,
        }
    if webui_core.predictor is None:
        return {
            "available": True,
            "loaded": False,
            "message": "Kronos model available but not loaded",
            "bundle_mode": bundle_mode,
            "torch_disabled": disabled,
        }
    return {
        "available": True,
        "loaded": True,
        "message": "Kronos model loaded and available",
        "bundle_mode": bundle_mode,
        "torch_disabled": disabled,
        "current_model": loaded_model_info(webui_core.predictor),
    }


def _dataframe_info(df: Any) -> dict[str, Any]:
    def detect_timeframe() -> str:
        if len(df) < 2 or "timestamps" not in df.columns:
            return "Unknown"

        time_diffs = [
            df["timestamps"].iloc[index] - df["timestamps"].iloc[index - 1]
            for index in range(1, min(10, len(df)))
        ]
        if not time_diffs:
            return "Unknown"

        avg_diff = sum(time_diffs, webui_core.pd.Timedelta(0)) / len(time_diffs)
        if avg_diff < webui_core.pd.Timedelta(minutes=1):
            return f"{avg_diff.total_seconds():.0f} seconds"
        if avg_diff < webui_core.pd.Timedelta(hours=1):
            return f"{avg_diff.total_seconds() / 60:.0f} minutes"
        if avg_diff < webui_core.pd.Timedelta(days=1):
            return f"{avg_diff.total_seconds() / 3600:.0f} hours"
        return f"{avg_diff.days} days"

    return {
        "rows": len(df),
        "columns": list(df.columns),
        "start_date": df["timestamps"].min().isoformat() if "timestamps" in df.columns else "N/A",
        "end_date": df["timestamps"].max().isoformat() if "timestamps" in df.columns else "N/A",
        "price_range": {
            "min": float(df[["open", "high", "low", "close"]].min().min()),
            "max": float(df[["open", "high", "low", "close"]].max().max()),
        },
        "prediction_columns": ["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else []),
        "timeframe": detect_timeframe(),
    }


@_native_get("/")
def index(request: Request) -> Response:
    return _render_template(
        "stock_analysis_home.html",
        desktop_mode=webui_core.desktop_mode_enabled(),
    )


@_native_get("/prediction")
def prediction_console(request: Request) -> Response:
    return _render_template("index.html")


def _desktop_response(page: str = "features") -> Response:
    page = webui_core.resolve_desktop_page(page)
    if page not in webui_core.DESKTOP_PAGES:
        return _text_response("Not Found", status_code=404)
    return _render_template(
        "desktop.html",
        pages=webui_core.DESKTOP_PAGES,
        active_page=page,
        page_title=webui_core.DESKTOP_PAGES[page]["title"],
        page_subtitle=webui_core.DESKTOP_PAGES[page]["subtitle"],
        desktop_mode=webui_core.desktop_mode_enabled(),
    )


@_native_get("/desktop")
def desktop_home(request: Request) -> Response:
    return _desktop_response("features")


@_native_get("/desktop/:page")
def desktop_page(request: Request, page=None) -> Response:
    return _desktop_response(_path_param(request, "page", page))


@_native_get("/static/*filename")
def static_asset(request: Request, filename=None) -> Response:
    relative = _path_param(request, "filename", filename)
    path = _safe_child_path(webui_core.PROJECT_ROOT / "webui" / "static", relative)
    # 仅当 URL 带 ?v=<版本> 时启用强缓存(版本即缓存键)；裸 URL 维持原行为。
    versioned = bool(_query_value(request, "v", ""))
    return _file_response(path, cache_immutable=versioned)


@_native_get("/figures/*filename")
def figures(request: Request, filename=None) -> Response:
    relative = _path_param(request, "filename", filename)
    return _file_response(_safe_child_path(webui_core.PROJECT_ROOT / "figures", relative))


@_native_get("/assets/*filename")
def assets(request: Request, filename=None) -> Response:
    relative = _path_param(request, "filename", filename)
    return _file_response(_safe_child_path(webui_core.PROJECT_ROOT / "assets", relative))


@_native_get("/favicon.ico")
def favicon(request: Request) -> Response:
    return _file_response(_safe_child_path(webui_core.PROJECT_ROOT / "assets", "lumo_ai_stock.ico"))


@_native_get("/particles")
def particles(request: Request) -> Response:
    path = webui_core.PROJECT_ROOT / "webui" / "templates" / "market_particles.html"
    return _file_response(path if path.is_file() else None)


@_native_get("/analysis-reports/*subpath")
def analysis_report(request: Request, subpath=None) -> Response:
    safe_path = _path_param(request, "subpath", subpath).replace("\\", "/")
    root_key, _separator, filename = safe_path.partition("/")
    directory = webui_core.REPORT_DIRS.get(root_key)
    if not directory or not filename:
        return _text_response("Not Found", status_code=404)
    return _file_response(_safe_child_path(directory, filename), attachment=filename.lower().endswith((".xlsx", ".xls")))


@_native_get("/api/data-files")
def get_data_files(request: Request) -> Response:
    return _json_response(webui_core.load_data_files())


@_native_post("/api/load-data")
def load_data(request: Request) -> Response:
    try:
        data = _request_json(request)
        file_path = data.get("file_path")
        if not file_path:
            return _json_response({"error": "File path cannot be empty"}, status_code=400)

        df, error = webui_core.load_data_file(file_path)
        if error:
            return _json_response({"error": error}, status_code=400)

        return _json_response(
            {
                "success": True,
                "data_info": _dataframe_info(df),
                "message": f"Successfully loaded data, total {len(df)} rows",
            }
        )
    except Exception as exc:
        return _json_response({"error": f"Failed to load data: {exc}"}, status_code=500)


@_native_post("/api/predict")
def predict(request: Request) -> Response:
    payload, status_code = run_prediction_payload(
        _request_json(request),
        webui_core._model_runtime_context(),
    )
    return _json_response(payload, status_code=status_code)


@_native_post("/api/load-model")
def load_model(request: Request) -> Response:
    payload, status_code = load_model_payload(
        _request_json(request),
        webui_core._model_runtime_context(),
    )
    return _json_response(payload, status_code=status_code)


@_native_get("/api/available-models")
def get_available_models(request: Request) -> Response:
    return _json_response(
        {
            "models": webui_core.AVAILABLE_MODELS,
            "model_available": webui_core.MODEL_AVAILABLE,
        }
    )


@_native_get("/api/model-status")
def get_model_status(request: Request) -> Response:
    return _json_response(_model_status_payload())


@_native_get("/api/settings")
def get_settings(request: Request) -> Response:
    return _json_response(webui_core.CONFIGURATION_SERVICE.settings_payload())


@_native_post("/api/settings/llm")
def save_llm_settings(request: Request) -> Response:
    return _json_response(webui_core.CONFIGURATION_SERVICE.save_llm_settings(_request_json(request)))


@_native_post("/api/settings/tushare")
def save_tushare_settings(request: Request) -> Response:
    return _json_response(webui_core.CONFIGURATION_SERVICE.save_tushare_settings(_request_json(request)))


@_native_get("/api/stock-dashboard")
def get_stock_dashboard(request: Request) -> Response:
    refresh_market = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    dashboard = {
        "generated_at": webui_core.datetime.datetime.now().isoformat(),
        "market": webui_core._build_market_dashboard(force_market_refresh=refresh_market),
        "opportunity": webui_core._load_latest_opportunities(),
        "batch": webui_core._load_batch_summary(),
        "reports": webui_core._load_report_history(),
        "modules": webui_core._module_health(),
        "model": {
            "library_available": webui_core.MODEL_AVAILABLE,
            "loaded": webui_core.predictor is not None,
            "available_models": webui_core.AVAILABLE_MODELS,
        },
        "settings": webui_core.CONFIGURATION_SERVICE.settings_payload(),
        "jobs": webui_core._get_job_snapshot(),
    }
    return _json_response(dashboard)


@_native_get("/api/market-cloud")
def get_market_cloud(request: Request) -> Response:
    refresh_market = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    limit = webui_core._safe_int(_query_value(request, "limit"), 5000, minimum=100, maximum=6000) or 5000
    trade_date = _query_value(request, "date", "")
    return _json_response(webui_core._market_cloud_payload(
        force_refresh=refresh_market,
        limit=limit,
        trade_date=trade_date,
    ))


@_native_get("/api/stock-kline/:stock_code")
def get_stock_kline(request: Request, stock_code=None) -> Response:
    payload, error = webui_core._get_stock_kline_payload(
        _path_param(request, "stock_code", stock_code),
        period=_query_value(request, "period", "daily"),
        limit=_query_value(request, "limit", 120),
    )
    if error:
        return _json_response({"error": error}, status_code=400)
    return _json_response({"success": True, "data": payload})


@_native_get("/api/stock-context/:stock_code")
def get_stock_context(request: Request, stock_code=None) -> Response:
    payload, error = webui_core._stock_context_payload(
        _path_param(request, "stock_code", stock_code),
        stock_name=_query_value(request, "name", ""),
    )
    if error:
        return _json_response({"error": error}, status_code=400)
    return _json_response(payload)


@_native_get("/api/stock-analysis-suite/:stock_code")
def get_stock_analysis_suite(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    name = _query_value(request, "name", "")
    force_refresh = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    try:
        quote = webui_core._latest_stock_quote(code) or {}
        current_price = webui_core._safe_float(quote.get("price"), 0.0)
        payload = webui_core.STOCK_SUITE_SERVICE.get_suite(
            code,
            name=name,
            force_refresh=force_refresh,
            current_price=current_price if current_price > 0 else None,
        )
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_get("/api/stock-capital-rankings/:stock_code")
def get_stock_analysis_suite_capital_rankings(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    date = (_query_value(request, "date") or "").strip() or None
    start_date = (_query_value(request, "start_date") or "").strip() or None
    end_date = (_query_value(request, "end_date") or "").strip() or None
    days = webui_core._safe_int(_query_value(request, "days"), 5, minimum=1, maximum=120) or 5
    try:
        payload = webui_core.STOCK_SUITE_SERVICE.get_capital_rankings(
            code,
            date=date,
            days=days,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_get("/api/diagnostics/data-sources")
def get_diagnostics_data_sources(request: Request) -> Response:
    """最近 24h 各数据源同步摘要（从 sync_log 读取）。"""
    try:
        from data_store import sync_log_repo

        summary = sync_log_repo.summary_last_24h()
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": str(exc)}, status_code=500)
    return _json_response(summary)


@_native_post("/api/stock-analysis-suite/:stock_code/ai")
def post_stock_analysis_suite_ai(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    body = _request_json(request) or {}
    name = str(body.get("name") or "")
    force_refresh = bool(body.get("force_refresh", False))
    model_full_key = body.get("model_full_key")
    try:
        # 异步启动：立即返回 status:running，由前端轮询 GET 同一路由获取结果。
        # DeepSeek 推理模型耗时 ~100s，WKWebView 会在 ~60s 掐断同步 fetch（用户看到 Load failed）。
        service = webui_core.STOCK_SUITE_SERVICE
        if hasattr(service, "start_ai_interpretation"):
            payload = service.start_ai_interpretation(
                code,
                name=name,
                model_full_key=model_full_key,
                force_refresh=force_refresh,
            )
        else:
            payload = service.trigger_ai_interpretation(
                code,
                name=name,
                model_full_key=model_full_key,
                force_refresh=force_refresh,
            )
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_get("/api/stock-analysis-suite/:stock_code/ai")
def get_stock_analysis_suite_ai(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    try:
        payload = webui_core.STOCK_SUITE_SERVICE.get_ai_interpretation(code)
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_post("/api/stock-analysis-suite/:stock_code/related-news/refresh")
def post_stock_analysis_suite_related_news_refresh(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    body = _request_json(request) or {}
    force_refresh = bool(body.get("force_refresh", False))
    try:
        payload = webui_core.STOCK_SUITE_SERVICE.refresh_related_news(
            code, force_refresh=force_refresh)
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_get("/api/stock-analysis-suite/:stock_code/related-news/status")
def get_stock_analysis_suite_related_news_status(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    try:
        payload = webui_core.STOCK_SUITE_SERVICE.get_related_news_status(code)
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_post("/api/stock-analysis-suite/:stock_code/panel-overlay")
def post_stock_analysis_suite_panel_overlay(request: Request, stock_code=None) -> Response:
    code = _path_param(request, "stock_code", stock_code)
    body = _request_json(request) or {}
    tier = str(body.get("tier") or "deep")
    force_refresh = bool(body.get("force_refresh", False))
    try:
        payload = webui_core.STOCK_SUITE_SERVICE.trigger_panel_overlay(
            code, tier=tier, force_refresh=force_refresh,
        )
    except ValueError as exc:
        return _json_response({"success": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"success": False, "error": str(exc)}, status_code=500)
    return _json_response(payload)


@_native_post("/api/opportunity-discovery/start")
def start_opportunity_discovery(request: Request) -> Response:
    params, error = webui_core.ANALYSIS_JOB_PARSER.opportunity_params(_request_json(request))
    if error:
        return _json_response({"error": error}, status_code=400)
    job = webui_core.JOB_SERVICE.start("opportunity_discovery", params, webui_core._run_opportunity_job)
    return _json_response({"success": True, "job": webui_core._get_job_snapshot(job["id"])})


@_native_post("/api/batch-analysis/start")
def start_batch_analysis(request: Request) -> Response:
    params, error = webui_core.ANALYSIS_JOB_PARSER.batch_params(_request_json(request))
    if error:
        return _json_response({"error": error}, status_code=400)
    job = webui_core.JOB_SERVICE.start("batch_analysis", params, webui_core._run_batch_analysis_job)
    return _json_response({"success": True, "job": webui_core._get_job_snapshot(job["id"])})


@_native_get("/api/jobs")
def list_jobs(request: Request) -> Response:
    return _json_response({"jobs": webui_core._get_job_snapshot()})


@_native_get("/api/jobs/:job_id")
def get_job(request: Request, job_id=None) -> Response:
    job = webui_core._get_job_snapshot(_path_param(request, "job_id", job_id))
    if not job:
        return _json_response({"error": "Job not found"}, status_code=404)
    return _json_response({"job": job})


@_native_get("/api/trading-clients")
def list_trading_clients(request: Request) -> Response:
    refresh = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    return _json_response(webui_core.TRADING_CLIENT_SERVICE.discover_clients(refresh=refresh))


@_native_post("/api/trading-clients/open")
def open_trading_client(request: Request) -> Response:
    payload = _request_json(request)
    client_id = str(payload.get("client_id") or "").strip()
    target = payload.get("target") or {}
    if not client_id or not isinstance(target, dict):
        return _json_response({"success": False, "error": "参数不完整"}, status_code=400)
    result = webui_core.TRADING_CLIENT_SERVICE.open_target(client_id, target)
    return _json_response(result, status_code=200 if result.get("success") else 400)


def _capital_ranking_payload(request: Request, kind: str) -> dict[str, Any]:
    date = (_query_value(request, "date") or "").strip() or None
    start_date = (_query_value(request, "start_date") or "").strip() or None
    end_date = (_query_value(request, "end_date") or "").strip() or None
    mode = (_query_value(request, "mode") or "single").strip()
    if mode not in ("single", "aggregate"):
        mode = "single"
    days = webui_core._safe_int(_query_value(request, "days"), 1, minimum=1, maximum=120) or 1
    top_n = webui_core._safe_int(_query_value(request, "top"), 50, minimum=1, maximum=500) or 50
    with_quotes = str(_query_value(request, "quotes", "1")).strip().lower() not in ("0", "false", "no")
    no_quant = str(_query_value(request, "no_quant", "0")).strip().lower() in ("1", "true", "yes")
    svc = webui_core.CAPITAL_RANKINGS_SERVICE
    if kind == "moneyflow":
        return svc.moneyflow_ranking(
            date=date,
            days=days,
            top_n=top_n,
            mode=mode,
            with_quotes=with_quotes,
            start_date=start_date,
            end_date=end_date,
            no_quant=no_quant,
        )
    return svc.dragon_tiger_ranking(
        date=date,
        days=days,
        top_n=top_n,
        mode=mode,
        with_quotes=with_quotes,
        start_date=start_date,
        end_date=end_date,
        no_quant=no_quant,
    )


@_native_get("/api/capital-rankings/moneyflow")
def capital_rankings_moneyflow(request: Request) -> Response:
    return _json_response(_capital_ranking_payload(request, "moneyflow"))


@_native_get("/api/capital-rankings/dragon-tiger")
def capital_rankings_dragon_tiger(request: Request) -> Response:
    return _json_response(_capital_ranking_payload(request, "dragon_tiger"))


@_native_post("/api/capital-rankings/backfill")
def capital_rankings_backfill(request: Request) -> Response:
    body = _request_json(request) or {}
    days = webui_core._safe_int(body.get("days"), 30, minimum=1, maximum=120) or 30
    kinds = body.get("kinds") if isinstance(body.get("kinds"), list) else ["moneyflow", "dragon_tiger"]
    params = {"days": days, "kinds": kinds}
    job = webui_core.JOB_SERVICE.start(
        "capital_backfill", params, webui_core._run_capital_backfill_job
    )
    return _json_response({
        "job_id": job["id"], "status": "queued",
        "job": webui_core._get_job_snapshot(job["id"]),
    })


@_native_get("/api/opportunity-report")
def opportunity_report_cards(request: Request) -> Response:
    file = (_query_value(request, "file") or "").strip()
    payload = webui_core.load_opportunity_report_cards(file)
    if payload is None:
        return _json_response({"error": "Report not found"}, status_code=404)
    return _json_response(payload)


@_native_get("/api/opportunity-canvas")
def opportunity_canvas(request: Request) -> Response:
    """投资机会画布按日切换:?run_id=N 优先,否则 ?date=YYYY-MM-DD 当日最新 run。"""
    run_id_raw = (_query_value(request, "run_id") or "").strip()
    date = (_query_value(request, "date") or "").strip()
    run_id = None
    if run_id_raw:
        try:
            run_id = int(run_id_raw)
        except (TypeError, ValueError):
            run_id = None
    payload = webui_core.opportunity_canvas_payload(run_id=run_id, date=date or None)
    if payload is None:
        return _json_response({"error": "该日无挖掘记录"}, status_code=404)
    return _json_response(payload)


# ----------------------------- 风险·机遇 作战大屏 command center -----------------------------

@_native_get("/api/command-center/overview")
def command_center_overview(request: Request) -> Response:
    quotes_only = str(_query_value(request, "quotes_only", "") or "").lower() in {"1", "true", "yes"}
    date = (_query_value(request, "date") or "").strip() or None
    return _json_response(webui_core.command_center_overview(date=date, quotes_only=quotes_only))


@_native_post("/api/command-center/recompute")
def command_center_recompute(request: Request) -> Response:
    return _json_response(webui_core.start_command_center_recompute())


@_native_get("/api/market-pulse")
def market_pulse(request: Request) -> Response:
    """指数风向 + 板块机会与拐点(总览页两个分区;盘中 30s 一拍独立刷新)。"""
    as_of = (_query_value(request, "as_of") or "").strip() or None
    return _json_response(webui_core.market_pulse_payload(as_of=as_of))


# ----------------------------- 模拟盘 paper trading -----------------------------

@_native_get("/api/trading-plans")
def trading_plans(request: Request) -> Response:
    try:
        payload = webui_core.TRADING_PLAN_SERVICE.overview(
            scope=_query_value(request, "scope", "watchlist"),
            code=_query_value(request, "code") or None,
            offset=webui_core._safe_int(_query_value(request, "offset"), 0, minimum=0) or 0,
            limit=webui_core._safe_int(_query_value(request, "limit"), 10, minimum=1, maximum=20) or 10,
        )
        return _json_response(payload)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return _json_response({"error": str(exc)}, status_code=500)


@_native_post("/api/trading-plans/lock")
def trading_plan_lock(request: Request) -> Response:
    try:
        payload, status = webui_core.TRADING_PLAN_SERVICE.lock_plan(_request_json(request))
        return _json_response(payload, status_code=status)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return _json_response({"error": str(exc)}, status_code=500)

@_native_get("/api/paper/account")
def paper_account(request: Request) -> Response:
    return _json_response(webui_core.PAPER_TRADING_SERVICE.account_summary())


@_native_get("/api/paper/positions")
def paper_positions(request: Request) -> Response:
    return _json_response({"positions": webui_core.PAPER_TRADING_SERVICE.positions()})


@_native_get("/api/paper/orders")
def paper_orders(request: Request) -> Response:
    status = (_query_value(request, "status") or "").strip() or None
    return _json_response({"orders": webui_core.PAPER_TRADING_SERVICE.orders(status=status)})


@_native_get("/api/paper/trades")
def paper_trades(request: Request) -> Response:
    limit = webui_core._safe_int(_query_value(request, "limit"), 200, minimum=1, maximum=2000)
    return _json_response({"trades": webui_core.PAPER_TRADING_SERVICE.trades(limit=limit)})


@_native_get("/api/paper/stats")
def paper_stats(request: Request) -> Response:
    return _json_response(webui_core.PAPER_TRADING_SERVICE.stats())


@_native_post("/api/paper/order")
def paper_place_order(request: Request) -> Response:
    body = _request_json(request) or {}
    ts_code = str(body.get("ts_code") or body.get("code") or "").strip()
    if not ts_code:
        return _json_response({"error": "ts_code required"}, status_code=400)
    order = webui_core.PAPER_TRADING_SERVICE.place_order(
        ts_code,
        str(body.get("side") or "buy"),
        str(body.get("price_type") or "market"),
        qty=body.get("qty"),
        amount=body.get("amount"),
        limit_price=body.get("limit_price"),
        name=str(body.get("name") or ""),
    )
    return _json_response({"order": order, "account": webui_core.PAPER_TRADING_SERVICE.account_summary()})


@_native_post("/api/paper/history-buy")
def paper_history_buy(request: Request) -> Response:
    body = _request_json(request) or {}
    ts_code = str(body.get("ts_code") or body.get("code") or "").strip()
    if not ts_code:
        return _json_response({"error": "ts_code required"}, status_code=400)
    order = webui_core.PAPER_TRADING_SERVICE.import_historical_buy(
        ts_code,
        name=str(body.get("name") or ""),
        price=body.get("price"),
        qty=body.get("qty"),
        trade_date=body.get("trade_date"),
    )
    return _json_response({"order": order, "account": webui_core.PAPER_TRADING_SERVICE.account_summary()})


@_native_post("/api/paper/order/:order_id/cancel")
def paper_cancel_order(request: Request, order_id=None) -> Response:
    oid = webui_core._safe_int(_path_param(request, "order_id", order_id), 0, minimum=0)
    if not oid:
        return _json_response({"error": "invalid order id"}, status_code=400)
    order = webui_core.PAPER_TRADING_SERVICE.cancel_order(oid)
    if order is None:
        return _json_response({"error": "order not found"}, status_code=404)
    return _json_response({"order": order})


@_native_post("/api/paper/reset")
def paper_reset(request: Request) -> Response:
    body = _request_json(request) or {}
    initial = body.get("initial_cash")
    return _json_response(webui_core.PAPER_TRADING_SERVICE.reset(initial_cash=initial))


@_native_post("/api/paper/settle")
def paper_settle(request: Request) -> Response:
    """手动触发 EOD：撮合补算 pending 单 + 盯市 + 生成/复用当日复盘。"""
    body = _request_json(request) or {}
    date = str(body.get("date") or "").strip() or None
    try:
        result = webui_core.run_paper_eod(date=date)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": str(exc)}, status_code=500)
    return _json_response(result)


@_native_get("/api/paper/reviews")
def paper_reviews(request: Request) -> Response:
    limit = webui_core._safe_int(_query_value(request, "limit"), 60, minimum=1, maximum=500)
    reviews = webui_core.PAPER_TRADING_SERVICE.list_reviews(str(webui_core.RESULTS_DIR), limit=limit)
    return _json_response({"reviews": reviews})


@_native_get("/api/paper/review/:date")
def paper_review_detail(request: Request, date=None) -> Response:
    day = _path_param(request, "date", date)
    content = webui_core.PAPER_TRADING_SERVICE.read_review(day, str(webui_core.RESULTS_DIR))
    if content is None:
        return _json_response({"error": "review not found"}, status_code=404)
    return _json_response({"date": day, "content": content})


# ----------------------------- 整库备份 data backup -----------------------------

@_native_get("/api/data/export")
def data_export(request: Request) -> Response:
    """整库导出:回传一致性 .db 快照供下载。"""
    try:
        path, name = webui_core.export_db_snapshot()
        data = Path(path).read_bytes()
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": f"导出失败:{exc}"}, status_code=500)
    return Response(
        status_code=200,
        headers=Headers({
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        }),
        description=data,
    )


@_native_post("/api/data/import")
def data_import(request: Request) -> Response:
    """整库导入(multipart 上传 .db):校验 → 自动备份当前库 → 灌库 → migrate。"""
    files = getattr(request, "files", None) or {}
    if not files:
        return _json_response({"error": "未收到上传文件(需 multipart 上传 .db)"}, status_code=400)
    raw = next(iter(files.values()))
    if isinstance(raw, str):
        raw = raw.encode("latin-1", errors="ignore")
    if not raw:
        return _json_response({"error": "上传文件为空"}, status_code=400)
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "kronos_import_upload.db"
    tmp.write_bytes(raw)
    try:
        result = webui_core.import_db_snapshot(str(tmp))
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": f"导入失败:{exc}"}, status_code=500)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return _json_response(result, status_code=200 if result.get("ok") else 400)


@_native_get("/api/pattern-search/status")
def pattern_search_status(request: Request) -> Response:
    return _json_response(webui_core.PATTERN_SEARCH_SERVICE.status())


@_native_post("/api/pattern-search/match")
def pattern_search_match(request: Request) -> Response:
    result, status_code = webui_core.PATTERN_SEARCH_SERVICE.match(_request_json(request))
    return _json_response(result, status_code=status_code)


@_native_get("/api/pattern-search/stocks")
def pattern_search_stocks(request: Request) -> Response:
    payload = webui_core.PATTERN_SEARCH_SERVICE.search_stocks(
        _query_value(request, "q"),
        limit=_query_value(request, "limit"),
    )
    return _json_response(payload)


@_native_get("/api/pattern-search/stock-curve/:stock_code")
def pattern_search_stock_curve(request: Request, stock_code=None) -> Response:
    result, status_code = webui_core.PATTERN_SEARCH_SERVICE.stock_curve(
        _path_param(request, "stock_code", stock_code)
    )
    return _json_response(result, status_code=status_code)


@_native_post("/api/pattern-search/refresh")
def pattern_search_refresh(request: Request) -> Response:
    params = webui_core.PATTERN_SEARCH_SERVICE.refresh_params(_request_json(request))
    job = webui_core.JOB_SERVICE.start("pattern_refresh", params, webui_core._run_pattern_refresh_job)
    return _json_response({"job_id": job["id"], "status": "queued", "job": webui_core._get_job_snapshot(job["id"])})


@_native_post("/api/pattern-search/backtest")
def pattern_search_backtest(request: Request) -> Response:
    params, error = webui_core.PATTERN_SEARCH_SERVICE.backtest_params(_request_json(request))
    if error:
        return _json_response({"error": error}, status_code=400)
    job = webui_core.JOB_SERVICE.start("pattern_backtest", params, webui_core._run_pattern_backtest_job)
    return _json_response({"job_id": job["id"], "status": "queued", "job": webui_core._get_job_snapshot(job["id"])})


@_native_post("/api/opportunity/pattern-backtest")
def opportunity_pattern_backtest(request: Request) -> Response:
    """机会挖掘形态回测:对某次 run 的 Top-N 股票做形态自回测打分(后台 job)。"""
    body = _request_json(request) or {}
    run_id = body.get("run_id")
    date = (str(body.get("date") or "").strip() or None)
    top_n = body.get("top_n") or 20
    return _json_response(webui_core.start_opportunity_pattern_backtest(run_id=run_id, date=date, top_n=top_n))


@_native_post("/api/pattern-search/save")
def pattern_search_save(request: Request) -> Response:
    result, status_code = webui_core.PATTERN_SEARCH_SERVICE.save_pattern(_request_json(request))
    return _json_response(result, status_code=status_code)


@_native_get("/api/pattern-search/saved")
def pattern_search_saved_list(request: Request) -> Response:
    payload = webui_core.PATTERN_SEARCH_SERVICE.list_saved_patterns(
        limit=_query_value(request, "limit"),
    )
    return _json_response(payload)


@_native_get("/api/pattern-search/saved/:pattern_id")
def pattern_search_saved_get(request: Request, pattern_id=None) -> Response:
    result, status_code = webui_core.PATTERN_SEARCH_SERVICE.get_saved_pattern(
        _path_param(request, "pattern_id", pattern_id)
    )
    return _json_response(result, status_code=status_code)


@_native_post("/api/pattern-search/saved/:pattern_id/delete")
def pattern_search_saved_delete(request: Request, pattern_id=None) -> Response:
    result, status_code = webui_core.PATTERN_SEARCH_SERVICE.delete_saved_pattern(
        _path_param(request, "pattern_id", pattern_id)
    )
    return _json_response(result, status_code=status_code)


@_native_get("/api/snapshot")
def get_snapshot(request: Request) -> Response:
    return _json_response(webui_core.market_state)


@_native_get("/api/market/hotspots")
def market_hotspots(request: Request) -> Response:
    """实时热点 / 异动 / 快讯（东方财富 + 金十 + 雪球），通知栏数据源。"""
    return _json_response(webui_core.MARKET_INTELLIGENCE_SERVICE.load())


@_native_get("/api/market/board-stocks")
def market_board_stocks(request: Request) -> Response:
    """板块成分股（东财 ``fs=b:BKxxxx``,失败回退星轨链路: Tushare dc_member/缓存）——行情台/行业热力/画布板块芯片 → 成分股弹窗数据源。"""
    code = _query_value(request, "code", "") or ""
    name = _query_value(request, "name", "") or ""
    limit = webui_core._safe_int(_query_value(request, "limit"), 60, minimum=1, maximum=200) or 60
    return _json_response(webui_core.board_stocks_payload(code, name, limit=limit))


# ---------------- 星轨图谱(物理AI/AI产业链 同心轨道图) ----------------
@_native_get("/api/star-orbit")
def star_orbit_map(request: Request) -> Response:
    """读星轨全图 + 叠加板块实时涨跌。``?live=0`` 跳过联网只读结构。"""
    live = str(_query_value(request, "live", "1") or "1") not in ("0", "false", "no")
    return _json_response(star_orbit_service.get_orbit_map(live=live))


@_native_get("/api/star-orbit/board-search")
def star_orbit_board_search(request: Request) -> Response:
    """按关键词搜东财真实概念+行业板块,供「添加板块」选择(保证 BK 码真实)。"""
    kw = _query_value(request, "kw", "") or _query_value(request, "keyword", "") or ""
    limit = webui_core._safe_int(_query_value(request, "limit"), 30, minimum=1, maximum=100) or 30
    return _json_response({"results": star_orbit_service.search_boards(kw, limit=limit)})


@_native_get("/api/star-orbit/board-stocks")
def star_orbit_board_stocks(request: Request) -> Response:
    """星轨内板块实时成分股(点击板块钻取)。"""
    code = _query_value(request, "code", "") or ""
    name = _query_value(request, "name", "") or ""
    limit = webui_core._safe_int(_query_value(request, "limit"), 30, minimum=1, maximum=500) or 30
    return _json_response(star_orbit_service.board_constituents(code, name, limit=limit))


@_native_post("/api/star-orbit/ring")
def star_orbit_ring_add(request: Request) -> Response:
    body = _request_json(request)
    name = str(body.get("name") or "").strip()
    if not name:
        return _json_response({"ok": False, "error": "轨道环名称不能为空"}, status_code=400)
    rid = star_orbit_repo.add_ring(name, body.get("subtitle") or "", body.get("color") or "")
    return _json_response({"ok": True, "id": rid})


@_native_post("/api/star-orbit/ring/update")
def star_orbit_ring_update(request: Request) -> Response:
    body = _request_json(request)
    rid = webui_core._safe_int(body.get("id"), 0, minimum=1)
    if not rid:
        return _json_response({"ok": False, "error": "缺少 id"}, status_code=400)
    star_orbit_repo.update_ring(rid, name=body.get("name"), subtitle=body.get("subtitle"),
                                color=body.get("color"), sort_order=body.get("sort_order"))
    return _json_response({"ok": True})


@_native_post("/api/star-orbit/ring/delete")
def star_orbit_ring_delete(request: Request) -> Response:
    rid = webui_core._safe_int(_request_json(request).get("id"), 0, minimum=1)
    if not rid:
        return _json_response({"ok": False, "error": "缺少 id"}, status_code=400)
    star_orbit_repo.delete_ring(rid)
    return _json_response({"ok": True})


@_native_post("/api/star-orbit/board")
def star_orbit_board_add(request: Request) -> Response:
    body = _request_json(request)
    ring_id = webui_core._safe_int(body.get("ring_id"), 0, minimum=1)
    code = str(body.get("board_code") or "").strip()
    if not ring_id or not code:
        return _json_response({"ok": False, "error": "缺少 ring_id 或 board_code"}, status_code=400)
    bid = star_orbit_repo.add_board(ring_id, code, body.get("board_name") or code,
                                    body.get("board_type") or "concept", body.get("note") or "")
    return _json_response({"ok": True, "id": bid})


@_native_post("/api/star-orbit/board/delete")
def star_orbit_board_delete(request: Request) -> Response:
    bid = webui_core._safe_int(_request_json(request).get("id"), 0, minimum=1)
    if not bid:
        return _json_response({"ok": False, "error": "缺少 id"}, status_code=400)
    star_orbit_repo.delete_board(bid)
    return _json_response({"ok": True})


@_native_post("/api/star-orbit/board/move")
def star_orbit_board_move(request: Request) -> Response:
    body = _request_json(request)
    bid = webui_core._safe_int(body.get("id"), 0, minimum=1)
    ring_id = webui_core._safe_int(body.get("ring_id"), 0, minimum=1)
    if not bid or not ring_id:
        return _json_response({"ok": False, "error": "缺少 id 或 ring_id"}, status_code=400)
    star_orbit_repo.move_board(bid, ring_id)
    return _json_response({"ok": True})


@_native_post("/api/star-orbit/stock")
def star_orbit_stock_add(request: Request) -> Response:
    body = _request_json(request)
    board_id = webui_core._safe_int(body.get("board_id"), 0, minimum=1)
    code = str(body.get("stock_code") or "").strip()
    if not board_id or not code:
        return _json_response({"ok": False, "error": "缺少 board_id 或 stock_code"}, status_code=400)
    sid = star_orbit_repo.add_stock(board_id, code, body.get("stock_name") or "", body.get("note") or "")
    return _json_response({"ok": True, "id": sid})


@_native_post("/api/star-orbit/stock/delete")
def star_orbit_stock_delete(request: Request) -> Response:
    sid = webui_core._safe_int(_request_json(request).get("id"), 0, minimum=1)
    if not sid:
        return _json_response({"ok": False, "error": "缺少 id"}, status_code=400)
    star_orbit_repo.delete_stock(sid)
    return _json_response({"ok": True})


@_native_post("/api/star-orbit/reset")
def star_orbit_reset(request: Request) -> Response:
    """恢复默认种子(物理AI/AI产业链 A股映射)。"""
    star_orbit_repo.reset_to_seed()
    return _json_response({"ok": True})


# ---------------- 股指期货(行情 + 中金所前20席位多空持仓) ----------------
@_native_get("/api/futures/overview")
def futures_overview(request: Request) -> Response:
    """四大股指期货全合约行情 + 现货指数与基差。``?refresh=1`` 跳过 30s 缓存。"""
    force = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    return _json_response(futures_service.overview(force=force))


@_native_get("/api/futures/positions")
def futures_positions(request: Request) -> Response:
    """中金所前20席位多空持仓排名(``?variety=IF&date=YYYY-MM-DD&refresh=1``)。

    无数据(节假日/盘中未发布)也返回 200 + ``ok:false``,由前端就地提示。
    """
    return _json_response(futures_service.position_rank(
        _query_value(request, "variety", "IF"),
        date=_query_value(request, "date", "") or "",
        force=str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"},
    ))


@_native_get("/api/futures/position-trend")
def futures_position_trend(request: Request) -> Response:
    """近 N 交易日前20席位多/空/净持仓趋势(``?variety=IF&days=10``)。"""
    days = webui_core._safe_int(_query_value(request, "days"), 10, minimum=2, maximum=30) or 10
    return _json_response(futures_service.position_trend(
        _query_value(request, "variety", "IF"), days=days))


# ---------------- 量化交易分析(Quant Radar:活跃识别 + 五机制收割预警) ----------------
def _quant_kline_fetcher(code: str, limit: int) -> list:
    """给 quant_radar 注入日K:复用 STOCK_KLINE_SERVICE 的 60s TTL 与实时叠加。"""
    payload, _err = webui_core.STOCK_KLINE_SERVICE.get_payload(code, period="daily", limit=limit)
    return ((payload or {}).get("records")) or []


@_native_get("/api/quant-radar/overview")
def quant_radar_overview(request: Request) -> Response:
    """量化雷达总览:市场温度计/活跃股票榜/板块榜/高危预警/知识卡。``?refresh=1`` 跳过 60s 缓存。

    ``?date=YYYY-MM-DD`` 回看历史(kv 快照优先,按日榜单表重建兜底,纯本地不发网络);
    休市或盘后异动为空时自动回退最近快照(payload 标 ``fallback_date``),不会 404。
    """
    force = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    date = (_query_value(request, "date", "") or "").strip()
    return _json_response(quant_radar_service.overview(force=force, date=date))


@_native_get("/api/quant-radar/day")
def quant_radar_day(request: Request) -> Response:
    """按日榜单搜索:``?date=YYYY-MM-DD&q=代码/名称/行业&min_activity=&limit=``。

    date 留空 = 最新有数据的交易日;数据源 quant_radar_stock_daily(当日全量评分行)。
    limit 缺省 0 = 返回全量(前端分页展示)。
    """
    date = (_query_value(request, "date", "") or "").strip()
    if not date:
        dates = quant_radar_repo.list_dates(limit=1)
        date = dates[0] if dates else ""
    q = (_query_value(request, "q", "") or "").strip()
    direction = (_query_value(request, "direction", "") or "").strip()
    min_activity = webui_core._safe_int(
        _query_value(request, "min_activity"), 0, minimum=0, maximum=100) or 0
    limit = webui_core._safe_int(_query_value(request, "limit"), 0, minimum=0, maximum=5000) or 0
    rows = (quant_radar_repo.get_day(date, limit=limit, q=q, min_activity=min_activity,
                                     direction=direction) if date else [])
    return _json_response({"ok": True, "date": date, "q": q, "direction": direction,
                           "count": len(rows), "rows": rows})


@_native_get("/api/quant-radar/dates")
def quant_radar_dates(request: Request) -> Response:
    """有按日榜单数据的日期列表(最新在前),供日期选择器。"""
    return _json_response({"ok": True, "dates": quant_radar_repo.list_dates()})


@_native_get("/api/quant-radar/stock/:stock_code")
def quant_radar_stock(request: Request, stock_code=None) -> Response:
    """个股量化行为深评:五机制评分 + 异动统计 + 量化席位 + 资金结构 + 行为预测。"""
    code = _safe_unquote_plus(stock_code or _query_value(request, "stock_code", "") or "").strip()
    payload = quant_radar_service.stock_analysis(code, kline_fetcher=_quant_kline_fetcher)
    return _json_response(payload, status_code=200 if payload.get("ok") else 400)


@_native_get("/api/quant-radar/accumulation")
def quant_radar_accumulation(request: Request) -> Response:
    """吸筹埋伏榜:主力持续净流入+量价背离的疑似吸筹股(``?window=20/40/60&date=&refresh=1``)。

    date 留空 = 最新交易日(实时增强);传历史日期走 kv 快照/本地 as-of 重算,不发网络。
    """
    window = webui_core._safe_int(_query_value(request, "window"), 40) or 40
    if window not in (20, 40, 60):  # 白名单,非法一律回退默认(不做 min/max 钳制)
        window = 40
    date = (_query_value(request, "date", "") or "").strip()
    force = str(_query_value(request, "refresh", "") or "").lower() in {"1", "true", "yes"}
    no_quant = str(_query_value(request, "no_quant", "") or "").lower() in {"1", "true", "yes"}
    return _json_response(quant_radar_service.accumulation_payload(
        window=window, date=date, force=force, no_quant=no_quant))


@_native_get("/api/screener/meta")
def screener_meta(request: Request) -> Response:
    """条件选股:各维度数据新鲜度(日期+覆盖数),供页面顶部提示条件可用性。"""
    return _json_response(stock_screener_service.meta())


@_native_post("/api/screener/run")
def screener_run(request: Request) -> Response:
    """条件选股:body 为条件 JSON(market/flow/radar/accum/dragon/opportunity/tech + sort/limit)。

    行情基座注入大盘云图快照(缓存优先,不强刷);技术条件注入 STOCK_KLINE_SERVICE
    (60s TTL,候选封顶见服务 TECH_CAP)。
    """
    try:
        cond = _request_json(request) or {}
    except Exception:
        cond = {}
    payload = stock_screener_service.screen(
        cond,
        market_rows_fn=lambda: (webui_core._market_cloud_payload(limit=6000) or {}).get("stocks") or [],
        kline_fetcher=_quant_kline_fetcher,
    )
    return _json_response(payload, status_code=200 if payload.get("ok") else 400)


@_native_get("/api/notifications/events")
def notification_events(request: Request) -> Response:
    """应用内系统事件（EOD 复盘 / 机会挖掘 / 自动跟单完成），通知条「系统」分类数据源。"""
    since = webui_core._safe_int(_query_value(request, "since"), 0, minimum=0) or 0
    limit = webui_core._safe_int(_query_value(request, "limit"), 50, minimum=1, maximum=200) or 50
    return _json_response(webui_core.NOTIFICATION_EVENTS.list(since_id=since, limit=limit))


@_native_get("/api/scoring-health")
def scoring_health(request: Request) -> Response:
    """评分算法健康度：最新可用回测 CSV 的分档胜率 / 降级占比 / 日期范围。"""
    try:
        start_date = (_query_value(request, "start_date") or "").strip() or None
        end_date = (_query_value(request, "end_date") or "").strip() or None
        window = str(_query_value(request, "window", "") or "").strip().lower()
        recent_month = window == "recent_month" or (not start_date and not end_date and not window)
        return _json_response(webui_core.SCORING_HEALTH_SERVICE.health(
            start_date=start_date,
            end_date=end_date,
            recent_month=recent_month,
        ))
    except Exception as exc:  # noqa: BLE001
        return _json_response({"available": False, "message": str(exc)}, status_code=500)


@_native_get("/api/opportunity-runs")
def opportunity_runs(request: Request) -> Response:
    """机会挖掘入库 run 列表（?date=YYYY-MM-DD 过滤单日；?days=1 返回按天聚合）。"""
    try:
        from data_store import opportunity_repo
        if str(_query_value(request, "days") or "").strip() in ("1", "true", "yes"):
            return _json_response({"days": opportunity_repo.runs_by_day(limit=60)})
        run_date = str(_query_value(request, "date") or "").strip() or None
        limit = webui_core._safe_int(_query_value(request, "limit"), 50, minimum=1, maximum=200) or 50
        return _json_response({"runs": opportunity_repo.list_runs(run_date=run_date, limit=limit)})
    except Exception as exc:  # noqa: BLE001
        return _json_response({"runs": [], "error": str(exc)}, status_code=500)


@_native_get("/api/opportunity-runs/:run_id/items")
def opportunity_run_items(request: Request, run_id=None) -> Response:
    """单次挖掘 run 的全量评分明细（按名次升序）。"""
    try:
        from data_store import opportunity_repo
        rid = webui_core._safe_int(_path_param(request, "run_id", run_id), 0, minimum=1)
        if not rid:
            return _json_response({"items": [], "error": "invalid run_id"}, status_code=400)
        return _json_response({"items": opportunity_repo.items_for_run(rid)})
    except Exception as exc:  # noqa: BLE001
        return _json_response({"items": [], "error": str(exc)}, status_code=500)


@_native_get("/api/opportunity/stock-scores/:stock_code")
def opportunity_stock_scores(request: Request, stock_code=None) -> Response:
    """投资机会挖掘·个股深度评分(懒加载):形态回测 + 多空评审团 + 资金榜单。

    按需现算(联网拉日K,约 5–15 秒),三块分值各自降级互不影响。
    """
    code = _path_param(request, "stock_code", stock_code)
    name = _query_value(request, "name", "")
    window_days = webui_core._safe_int(_query_value(request, "window_days"), 30, minimum=5, maximum=120) or 30
    try:
        payload = webui_core._opportunity_stock_scores(code, name=name, window_days=window_days)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"ok": False, "error": str(exc)}, status_code=500)
    status = 200 if payload.get("ok") else 400
    return _json_response(payload, status_code=status)


@_native_get("/api/hot-sector-snapshot")
def hot_sector_snapshot(request: Request) -> Response:
    """最新热门板块全量快照摘要（板块级，不展开全部成分股）。"""
    sid = webui_core._safe_int(_query_value(request, "snapshot_id"), None, minimum=1)
    payload = webui_core._load_hot_sector_snapshot_summary(sid)
    if not payload:
        return _json_response({"snapshot": None, "boards": [], "stats": {"boards": 0, "stocks": 0, "relations": 0}})
    return _json_response(payload)


@_native_get("/api/hot-sector-snapshots")
def hot_sector_snapshots(request: Request) -> Response:
    """历史热门板块快照列表（不展开成分股）。"""
    limit = webui_core._safe_int(_query_value(request, "limit"), 50, minimum=1, maximum=200) or 50
    return _json_response({"snapshots": webui_core.list_hot_sector_snapshots(limit)})


@_native_get("/api/opportunity-stock-pool")
def opportunity_stock_pool(request: Request) -> Response:
    """跨 run 聚合的股票池（入选次数/首末入选/重复入选日期/最佳分等）。

    ``since``/``until`` (YYYY-MM-DD, 闭区间) 限定统计的 run 日期区间;
    缺省不过滤(全部历史)。前端默认传最近一个月。

    「入选后涨幅」要按交易日取全市场开盘/收盘价,这里顺带触发一次**后台**补数
    (见 data_store.market_daily_fetch),不阻塞本次响应:缺的日期会在随后几次
    刷新里补齐,也可以直接跑 scripts/backfill_market_daily.py 一次补完。
    """
    try:
        from data_store import market_daily_fetch, opportunity_repo
        limit = webui_core._safe_int(_query_value(request, "limit"), 300, minimum=1, maximum=2000) or 300
        since = str(_query_value(request, "since") or "").strip() or None
        until = str(_query_value(request, "until") or "").strip() or None
        try:
            market_daily_fetch.ensure_pool_dates_background(since)
        except Exception:  # noqa: BLE001 — 补数是锦上添花,不能影响榜单返回
            pass
        return _json_response({
            "stocks": opportunity_repo.stock_pool(limit=limit, since_date=since, until_date=until),
            "since": since,
            "until": until,
            "price_date": market_daily_fetch.latest_filled_date(),
        })
    except Exception as exc:  # noqa: BLE001
        return _json_response({"stocks": [], "error": str(exc)}, status_code=500)


@_native_get("/api/hot-sector-pool")
def hot_sector_pool(request: Request) -> Response:
    """跨所有快照聚合的板块池（上榜次数/首末上榜/重复上榜日期/最佳名次等）。"""
    try:
        from data_store import hot_sector_repo
        limit = webui_core._safe_int(_query_value(request, "limit"), 200, minimum=1, maximum=2000) or 200
        return _json_response({"sectors": hot_sector_repo.sector_pool(limit=limit)})
    except Exception as exc:  # noqa: BLE001
        return _json_response({"sectors": [], "error": str(exc)}, status_code=500)


@_native_get("/api/hot-sector-snapshot/:snapshot_id/stocks")
def hot_sector_snapshot_stocks(request: Request, snapshot_id=None) -> Response:
    """热门板块成分股分页钻取（?board_code=BKxxxx&limit=200&offset=0）。"""
    sid = webui_core._safe_int(_path_param(request, "snapshot_id", snapshot_id), None, minimum=1)
    if not sid:
        return _json_response({"stocks": [], "relations": [], "error": "invalid snapshot_id"}, status_code=400)
    payload = webui_core.hot_sector_stocks_payload(
        sid,
        board_code=str(_query_value(request, "board_code") or "").strip() or None,
        limit=_query_value(request, "limit", 200),
        offset=_query_value(request, "offset", 0),
    )
    return _json_response(payload)


@_native_post("/api/hot-sector-snapshot/:snapshot_id/export")
def hot_sector_snapshot_export(request: Request, snapshot_id=None) -> Response:
    """导出热门板块快照为多 sheet Excel，并返回 analysis-reports 下载 URL。"""
    sid = webui_core._safe_int(_path_param(request, "snapshot_id", snapshot_id), None, minimum=1)
    if not sid:
        return _json_response({"error": "invalid snapshot_id"}, status_code=400)
    try:
        payload = webui_core.export_hot_sector_snapshot(sid)
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": str(exc)}, status_code=500)
    if not payload:
        return _json_response({"error": "snapshot not found"}, status_code=404)
    # WKWebView 不触发附件下载且 :7070 拿不到 Tauri IPC,改由本机后端在文件管理器里
    # 定位刚导出的文件(详见 webui_core.reveal_in_file_manager)。
    revealed = webui_core.reveal_in_file_manager(payload.get("path"))
    return _json_response({"success": True, "revealed": revealed, **payload})


@_native_post("/api/opportunity-canvas/export")
def opportunity_canvas_export(request: Request) -> Response:
    """导出当前投资机会画布为多 sheet Excel；没有热门板块快照也可用。"""
    try:
        payload = webui_core.export_opportunity_canvas_excel()
    except Exception as exc:  # noqa: BLE001
        return _json_response({"error": str(exc)}, status_code=500)
    revealed = webui_core.reveal_in_file_manager(payload.get("path"))
    return _json_response({"success": True, "revealed": revealed, **payload})


@_native_get("/api/stock/financial-statements")
def stock_financial_statements(request: Request) -> Response:
    """个股财务三大表(默认读缓存,?force=1 强制联网刷新)。"""
    code = (_query_value(request, "code") or "").strip()
    if not code:
        return _json_response({"error": "缺少股票代码"}, status_code=400)
    force = str(_query_value(request, "force", "") or "").lower() in {"1", "true", "yes"}
    return _json_response(webui_core.stock_financial_statements(code, force_refresh=force))



@_native_post("/api/settings/auto-follow")
def save_auto_follow_settings(request: Request) -> Response:
    """保存模拟盘自动跟单配置（开关 / 最低档位 / 单票金额 / 持有天数 / 买入方式）。"""
    return _json_response(webui_core.CONFIGURATION_SERVICE.save_auto_follow_settings(_request_json(request)))


@_native_get("/api/watchlist")
def watchlist_list(request: Request) -> Response:
    return _json_response(webui_core.WATCHLIST_SERVICE.list_with_quotes())


@_native_get("/api/watchlist/alerts")
def watchlist_alerts_api(request: Request) -> Response:
    """自选股智能提醒:超跌反弹/超买/超卖/量化介入/主力出逃/收割预警/放量异动。

    数据面与条件选股共用(本地资金流/量化雷达/吸筹/量化席位 + STOCK_KLINE_SERVICE 日K)。
    """
    items = webui_core.WATCHLIST_SERVICE.list_items()
    return _json_response(stock_screener_service.watchlist_alerts(
        items, kline_fetcher=_quant_kline_fetcher))


@_native_post("/api/watchlist/add")
def watchlist_add(request: Request) -> Response:
    body = _request_json(request)
    result, status_code = webui_core.WATCHLIST_SERVICE.add(body.get("code"), body.get("name"))
    return _json_response(result, status_code=status_code)


@_native_post("/api/watchlist/remove")
def watchlist_remove(request: Request) -> Response:
    body = _request_json(request)
    result, status_code = webui_core.WATCHLIST_SERVICE.remove(body.get("code"))
    return _json_response(result, status_code=status_code)


@_native_post("/api/watchlist/pin")
def watchlist_pin(request: Request) -> Response:
    body = _request_json(request)
    result, status_code = webui_core.WATCHLIST_SERVICE.pin(
        body.get("code"), body.get("pinned", True)
    )
    return _json_response(result, status_code=status_code)


@_native_post("/api/open-url")
def api_open_url(request: Request) -> Response:
    """桌面 App 外链代开:WKWebView 吞掉 target=_blank,由系统默认浏览器打开。"""
    body = _request_json(request)
    result, status_code = webui_core.open_external_url(body.get("url"))
    return _json_response(result, status_code=status_code)


@app.startup_handler
def startup() -> None:
    webui_core.mark_interrupted_jobs_on_boot()
    if license_service.license_required():
        license_service.warm_in_background()
    webui_core.start_market_monitor()
    webui_core.start_pattern_autorefresh()
    webui_core.start_paper_eod()
    quant_radar_service.start_autosave()
    webui_core.start_sector_refresh()


def configure_server_from_env() -> None:
    # processes 必须保持 1：任务注册表/自选/形态库等都是进程内共享状态。
    # workers 是单进程内并发执行 handler 的线程数——本服务 handler 全是同步函数，
    # workers=1 时任意一个慢请求(如 Sina K线超时8s)会让 /desktop/* 页面 HTML 一起排队，
    # 表现为点左侧菜单整页卡顿。handler 以 I/O 等待为主，多线程即可解除串行。
    app.config.processes = _env_int("ROBYN_PROCESSES", 1)
    app.config.workers = _env_int("ROBYN_WORKERS", 8)
    app.config.log_level = os.environ.get("ROBYN_LOG_LEVEL", app.config.log_level)


def get_route_manifest() -> list[dict[str, Any]]:
    """Return the registered Robyn route manifest for tests and diagnostics."""
    return [
        {"method": str(route.route_type).split(".")[-1], "route": route.route}
        for route in app.router.get_routes()
    ]


def get_native_route_manifest() -> list[dict[str, str]]:
    """Return routes implemented by native Robyn handlers."""
    return [
        {"method": method, "route": route}
        for method, route in sorted(NATIVE_ROUTE_KEYS)
    ]


def run_server() -> None:
    host, port, _debug = webui_core.get_server_config()
    configure_server_from_env()
    _ensure_port_available(host, port)
    # 桌面外壳非正常死亡时没人来杀我们(后端在独立进程组),靠守望线程自了。
    start_parent_watchdog()
    app.start(host=host, port=port, _check_port=False)


def _ensure_port_available(host: str, port: int) -> None:
    """Fail fast with a clear message instead of Robyn's interactive port prompt.

    Robyn's default _check_port=True loops on input() when the port is busy,
    which hangs Tauri-spawned backends (no tty attached). We probe with a
    fresh socket (no SO_REUSEADDR) and exit non-zero so the host process
    can recover.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        msg = (
            f"❌ Port {port} on {host} is already in use ({exc}). "
            "Kill the stale Kronos backend or set KRONOS_PORT to a free port."
        )
        print(msg, file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        probe.close()


if __name__ == "__main__":
    print("Starting Kronos Web UI with Robyn...")
    print(json.dumps({"routes": get_route_manifest()[:5], "server": "robyn"}, ensure_ascii=False))
    run_server()
