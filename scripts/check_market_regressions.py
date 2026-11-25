"""Run the CI market checks locally, without a listening server or data collection."""
import argparse
import os
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.market_test_sandbox import offline_market_runtime

UNIT = ("market_capital", "market_capital_service", "market_macro", "market_health_routes",
        "market_cycle", "market_ice_service", "market_health_realtime", "kline_service", "kline_patterns_payload",
        "model_runtime",
        "ohlcv_repo", "opportunity_pattern_backtest", "pattern_backtest", "paper_trading_engine",
        "paper_trading_service", "paper_auto_follow", "market_derivatives", "market_derivatives_frontend")
BROWSER = ("market_capital", "market_capital_history", "market_correlation", "market_macro", "market_ice",
           "price_charts", "opportunity_backtest", "market_derivatives")
JS = ("desktop_app", "desktop_kline", "particle_kline", "price_chart", "market_health", "market_capital",
      "market_correlation_scene", "market_macro", "opportunity_backtest", "market_derivatives")


def benchmark():
    from data_store.connection import close_conn
    from webui.services import market_macro_service as macro

    def timed(call, **kwargs):
        start = perf_counter()
        result = call(**kwargs)
        return perf_counter() - start, result

    try:
        cold, report = timed(macro.query)
        assert report["available"] and report["events"]["total"], "No distribution baseline"
        warm = [timed(macro.query)[0] for _ in range(7)]
        fiscal = []
        for _ in warm:
            elapsed, result = timed(macro.events, keyword="财政")
            assert 0 < result["events"]["total"] <= report["events"]["total"]
            fiscal.append(elapsed)
        print(f"baseline={report['as_of']}  metrics={len(report['metrics'])}  "
              f"events={report['events']['total']}  matched={result['events']['total']}\n"
              f"service only: cold report {cold:.3f}s; medians of {len(warm)}: "
              f"warm report {statistics.median(warm):.3f}s, event search {statistics.median(fiscal):.3f}s")
    finally:
        close_conn()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "unit", "browser", "benchmark"), default="all")
    suite = parser.parse_args().suite
    os.chdir(ROOT)
    if suite == "benchmark":
        with offline_market_runtime():
            benchmark()
        return 0
    for name in JS:
        subprocess.run(["node", "--check", f"webui/static/lumo_{name}.js"], check=True)
    if suite in ("all", "unit"):
        with offline_market_runtime() as tmp:
            os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            import pytest
            status = pytest.main(["-q", "--basetemp", str(tmp / "pytest"),
                                  *(f"tests/test_{name}.py" for name in UNIT)])
            if status:
                return status
    if suite in ("all", "browser"):
        for name in BROWSER:
            script = f"tests/browser_{name}.py"
            print(f"Checking {script}", flush=True)
            subprocess.run([sys.executable, script], check=True)
    print(f"Offline checks passed ({suite}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
