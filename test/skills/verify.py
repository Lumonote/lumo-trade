#!/usr/bin/env python3
"""Reproducible verification for the Kronos read-only data-query skills.

It drives each skill's ``run.py`` over an *independent* subprocess path (not
in-process), parses the JSON envelope, and asserts expected behavior.

Scenarios cover:
  1. normal return (data present for a known date),
  2. historical date (older snapshot),
  3. no-data (date outside coverage -> no_data, nearest date reported),
  4. data-source failure (missing DB file -> error, no fabricated data).

Safety: it never reads a secret into logs and never deletes or resets any user
data; the only file it writes is the JSON report under test/docs.

Usage:
    python3 verify.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE
DOCS_DIR = HERE.parents[0] / "docs"   # test/skills -> test/docs


def run(skill: str, function: str, extra: Optional[List[str]] = None) -> Dict[str, Any]:
    cmd = [sys.executable, str(SKILLS_DIR / skill / "run.py"),
           "query", "--function", function] + (extra or [])
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SKILLS_DIR))
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"_parse_error": True, "stdout": proc.stdout, "stderr": proc.stderr}


# Each scenario: (id, description, skill, function, extra, assertions)
# assertions receive the envelope and return (passed: bool, detail: str).
def has_rows(env: Dict[str, Any]) -> (bool, str):
    n = env.get("coverage", {}).get("rows", 0)
    return (n and n > 0), f"rows={n}"


def is_no_data(env: Dict[str, Any]) -> (bool, str):
    ok = env.get("status") == "no_data"
    nearest = env.get("as_of", {}).get("date")
    return ok, f"status={env.get('status')}, nearest={nearest}"


def is_error(env: Dict[str, Any]) -> (bool, str):
    ok = env.get("status") == "error" and bool(env.get("error"))
    return ok, f"status={env.get('status')}, error={env.get('error')}"


def is_historical(env: Dict[str, Any]) -> (bool, str):
    nature = env.get("data_nature")
    asof = env.get("as_of", {}).get("date")
    ok = nature == "historical_snapshot" and bool(asof)
    return ok, f"nature={nature}, as_of={asof}"


SCENARIOS = [
    # 1) normal returns
    ("S1", "正常：交易日判定（日历内工作日）", "market-calendar-query",
     "is_trading_day", ["--date", "2026-05-28"], has_rows),
    ("S2", "正常：市场宽度（全市场涨跌家数）", "market-overview-query",
     "market_breadth", ["--date", "2026-06-01"], has_rows),
    ("S3", "正常：资金流事实排序", "capital-flow-query",
     "moneyflow_ranking", ["--date", "2026-06-01"], has_rows),
    ("S4", "正常：个股日K窗口", "stock-research-query",
     "daily_kline", ["--code", "000001", "--date", "2026-06-01"], has_rows),
    # 2) historical date (older snapshot)
    ("H1", "历史日期：估值快照（2026-01-xx）", "market-overview-query",
     "daily_basic_snapshot", ["--date", "2026-01-05"], is_historical),
    ("H2", "历史日期：指数日线（较早快照）", "market-overview-query",
     "index_daily", ["--date", "2026-07-15"], is_historical),
    # 3) no-data (outside coverage)
    ("N1", "无数据：宽度早于覆盖区间", "market-overview-query",
     "market_breadth", ["--date", "2024-01-01"], is_no_data),
    ("N2", "无数据：财报（财报表为空）", "stock-research-query",
     "financial_statements", ["--code", "600519", "--statement-type", "income"], is_no_data),
    ("N3", "无数据：个股资金流（非交易日未来日期）", "capital-flow-query",
     "moneyflow_stock", ["--code", "000001", "--date", "2026-10-28"], is_no_data),
    # 4) data-source failure
    ("F1", "数据源失败：数据库文件缺失", "market-calendar-query",
     "is_trading_day", ["--date", "2026-05-28", "--db", "/tmp/kronos_missing_verify.sqlite"], is_error),
]


def main() -> int:
    results = []
    passed_count = 0
    for sid, desc, skill, fn, extra, check in SCENARIOS:
        env = run(skill, fn, extra)
        if env.get("_parse_error"):
            passed, detail = False, "JSON 解析失败"
        else:
            passed, detail = check(env)
        # Extra global invariants for every successful envelope.
        if not env.get("_parse_error"):
            disclaimer_ok = bool(env.get("disclaimer"))
            nature_ok = env.get("data_nature") != "live_quote"  # never relabeled as live
            passed = passed and disclaimer_ok and nature_ok
            detail += f" | disclaimer={'y' if disclaimer_ok else 'n'}, not_live={'y' if nature_ok else 'n'}"
        results.append({
            "id": sid, "description": desc, "skill": skill, "function": fn,
            "args": extra, "passed": passed, "detail": detail,
            "status": env.get("status"), "data_nature": env.get("data_nature"),
        })
        passed_count += 1 if passed else 0
        print(f"[{'PASS' if passed else 'FAIL'}] {sid} {desc}  ({detail})")

    report = {
        "summary": {
            "total": len(SCENARIOS),
            "passed": passed_count,
            "failed": len(SCENARIOS) - passed_count,
        },
        "results": results,
    }
    out_path = (DOCS_DIR / "verification-results.json").resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{passed_count}/{len(SCENARIOS)} passed; report -> {out_path}")
    return 0 if passed_count == len(SCENARIOS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
