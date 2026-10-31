"""Command-line runtime entry for the data-query skills.

Usage:
    python -m <...> list
    python -m <...> query --skill <skill> --function <fn> [options]

The same dispatcher backs every skill's ``run.py``; a skill wrapper simply
pre-sets its own ``--skill`` default. Output is the standard JSON envelope on
stdout (one object), so it can be piped into other tooling. Nothing is written
to the database and no secret is read into logs.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import handlers
from .envelope import now_iso


def build_parser(skill_default: Optional[str] = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kronos-data-skill",
        description="Kronos 只读数据查询技能运行入口",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有可发现的技能与功能点")

    q = sub.add_parser("query", help="执行某个功能点查询")
    q.add_argument("--skill", default=skill_default,
                   help="技能名（run.py 已预置，可省略）")
    q.add_argument("--function", required=skill_default is None,
                   help="功能点名")
    q.add_argument("--date", help="请求日期 YYYY-MM-DD 或 YYYYMMDD")
    q.add_argument("--start", help="区间起始日期")
    q.add_argument("--end", help="区间结束日期")
    q.add_argument("--code", help="个股代码（6 位，可带后缀）")
    q.add_argument("--end-date", dest="end_date", help="报告期 end_date")
    q.add_argument("--top-n", dest="top_n", help="资金流 top_n 口径")
    q.add_argument("--limit", help="返回行数上限")
    q.add_argument("--window", help="K线窗口（天数）")
    q.add_argument("--direction", choices=("prev", "next"),
                   help="最近交易日方向")
    q.add_argument("--statement-type", dest="statement_type",
                   choices=("balance", "income", "cashflow"),
                   help="财报类型")
    q.add_argument("--namespace", help="kv_cache 命名空间")
    q.add_argument("--cache-type", dest="cache_type", help="sentiment_cache 类型")
    q.add_argument("--source", help="数据源名（sync_log）")
    q.add_argument("--frequency", default="1d", help="K线频率（默认 1d）")
    q.add_argument("--db", help="显式 SQLite 路径（默认 data/lumo_data.sqlite）")
    q.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    q.add_argument("--compact", action="store_true", help="单行输出 JSON（默认）")
    return parser


def _list_payload() -> Dict[str, Any]:
    out = {
        "command": "list",
        "generated_at": now_iso(),
        "skills": [
            {
                "name": skill,
                "functions": list(fns.keys()),
                "entry": f"test/skills/{skill}/run.py",
            }
            for skill, fns in handlers.REGISTRY.items()
        ],
    }
    return out


def _print(payload: Dict[str, Any], pretty: bool) -> None:
    if pretty:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


def main(argv: Optional[List[str]] = None, skill_default: Optional[str] = None) -> int:
    parser = build_parser(skill_default)
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 2

    if args.command == "list":
        _print(_list_payload(), pretty=True)
        return 0

    # query
    if not args.skill:
        print("需要 --skill", file=sys.stderr)
        return 2
    skill_fns = handlers.REGISTRY.get(args.skill)
    if not skill_fns:
        print(f"未知技能: {args.skill}（用 list 查看）", file=sys.stderr)
        return 2
    fn = skill_fns.get(args.function)
    if not fn:
        print(f"技能 {args.skill} 无功能点 {args.function}（用 list 查看）", file=sys.stderr)
        return 2

    payload = fn(vars(args))
    _print(payload, pretty=args.pretty)
    # Exit code reflects status so callers can branch, but output is always JSON.
    return 0 if payload.get("status") in ("ok", "limited") else 3


if __name__ == "__main__":
    raise SystemExit(main())
