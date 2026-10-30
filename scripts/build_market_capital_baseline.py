"""复用现有配置采集两融/资金研究，生成可随应用分发的真实日期基线。"""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import os

from webui.services import market_capital_service as service
from data_store.connection import close_conn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("financing", "participants", "both"), default="both")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--from-cache", action="store_true", help="仅打包已采集缓存，不请求外部接口")
    parser.add_argument("--quant-db", type=Path, help="可选：只读复用现有桌面量化雷达数据库")
    args = parser.parse_args()
    kinds = ("financing", "participants") if args.kind == "both" else (args.kind,)
    results = {}
    try:
        for kind in kinds:
            results[kind] = service.bundle(kind) if args.from_cache else service.collect(kind, lambda message: print(message, flush=True))
            if not results[kind]:
                raise ValueError(f"{kind}尚无可打包的研究缓存")
            print(json.dumps({"kind": kind, "as_of": results[kind]["as_of"], "sources": results[kind]["sources"], "errors": results[kind]["errors"]}, ensure_ascii=False), flush=True)
        if args.quant_db and "participants" in results:
            data = results["participants"]
            connection = sqlite3.connect(args.quant_db.resolve().as_uri() + "?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                date = connection.execute("SELECT MAX(trade_date) FROM quant_radar_stock_daily WHERE trade_date<=?", (data["as_of"],)).fetchone()[0]
                raw = connection.execute("SELECT code,name,industry,activity,direction,changes_bull,changes_bear FROM quant_radar_stock_daily WHERE trade_date=?", (date,)).fetchall() if date else []
                data["quant"] = [{"code": r["code"], "name": r["name"] or r["code"], "sector": r["industry"] or "未分类", "as_of": date,
                                  "activity": r["activity"], "direction": r["direction"], "bull": r["changes_bull"], "bear": r["changes_bear"], "net": None} for r in raw]
                data["sources"] = [r for r in data["sources"] if r["key"] != "quant"] + [{"key": "quant", "as_of": date, "rows": len(raw), "status": "现有桌面量化雷达公开行为缓存 / 不推算金额"}]
                print(json.dumps({"quant_as_of": date, "quant_rows": len(raw)}, ensure_ascii=False))
            finally:
                connection.close()
        if args.write_baseline:
            previous = {}
            if service.BASELINE.exists():
                with gzip.open(service.BASELINE, "rt", encoding="utf-8") as handle:
                    previous = json.load(handle)
            previous.update(results)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=service.BASELINE.parent, suffix=".json.gz", delete=False) as handle:
                    temporary = Path(handle.name)
                with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=9) as handle:
                    json.dump(previous, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                os.replace(temporary, service.BASELINE)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
            print(str(service.BASELINE), flush=True)
    finally:
        close_conn()


if __name__ == "__main__":
    main()
