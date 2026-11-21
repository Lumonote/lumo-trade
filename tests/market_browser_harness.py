"""Standalone browser checks: real Robyn GET handlers, no listener or startup jobs."""
from contextlib import contextmanager
from pathlib import Path
import os
import sys
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import sync_playwright
from robyn.testing import TestClient
from market_test_sandbox import offline_market_runtime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
URL = "http://kronos.test"
HEALTH = "/api/market-health"
READS = {HEALTH + suffix for suffix in ("", "/index-history", "/financing", "/participants", "/participants/history",
                                         "/macro", "/macro/cycle", "/macro/history", "/macro/events", "/macro/status")}
PAGES = {"/desktop/market_health", "/desktop/workbench"}


def _allowed(path):
    return path in READS or path in PAGES or path.startswith('/static/')


@contextmanager
def market_page():
    if len(sys.argv) != 1:
        raise SystemExit("Offline checks take no server URL.")
    channel = os.getenv('KRONOS_BROWSER_CHANNEL') or None
    with offline_market_runtime() as root:
        out = root / "screenshots"
        out.mkdir()
        from webui.robyn_app import app
        from data_store.connection import close_conn

        try:
            with TestClient(app) as client, sync_playwright() as p:
                def get(path):
                    parts = urlsplit(path)
                    if not _allowed(parts.path):
                        raise ValueError(f"Not a read-only test route: {parts.path}")
                    query = dict(parse_qsl(parts.query, keep_blank_values=True)) if parts.path != HEALTH else {}
                    return client.get(parts.path, query_params=query)

                def read(path):
                    response = get(path)
                    assert response.ok, (path, response.status_code, response.text[:200])
                    return response.json()

                snapshot = None

                def dispatch(route):
                    nonlocal snapshot
                    request = route.request
                    if not request.url.startswith(URL + "/"):
                        return route.abort()
                    path = urlsplit(request.url).path
                    if request.method != "GET":
                        return route.fulfill(status=405, json={"error": "read-only test"})
                    if path == HEALTH:
                        if snapshot is None:
                            snapshot = read(HEALTH)
                            snapshot.update(source="browser_read_only")
                            snapshot["fusion"]["updated_at"] = snapshot["as_of"]
                        return route.fulfill(json=snapshot)
                    if _allowed(path):
                        response = get(request.url)
                        return route.fulfill(status=response.status_code, body=response.content,
                                             content_type=response.headers.get("Content-Type"))
                    return route.fulfill(status=404, json={"error": "not a test route"})

                browser = p.chromium.launch(channel=channel)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True, service_workers="block")
                context.route("**/*", dispatch)
                try:
                    yield context.new_page(), read, out
                finally:
                    context.close()
                    browser.close()
        finally:
            close_conn()
