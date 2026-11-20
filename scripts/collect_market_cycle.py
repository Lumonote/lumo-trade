"""Rebuild the small, sourced NBS release archive, without contacting a trading account."""
import argparse
import json
from pathlib import Path

from webui.services.market_cycle_sources import AXES, fetch_releases
from webui.services.market_macro_service import _text, baseline


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pages', type=int, default=36)
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / 'config/market_cycle_releases.json'
    old = json.loads(path.read_text()) if path.exists() else {}
    rows, errors = fetch_releases(_text, baseline()['as_of'], old, args.pages)
    if not all(rows[key] for key in AXES):
        raise SystemExit('原文读取不足，未写入基线')
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print({k: len(v) for k, v in rows.items()}, '未能读取:', errors)
