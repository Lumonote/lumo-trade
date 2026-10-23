"""用现有TuShare配置重建可离线查看的板块研究基线，不写入任何凭据。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from webui.services import sector_depth_service as service


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=service.BASELINE)
    args=parser.parse_args()
    report=service.collect(print)
    report["details"]={}
    for kind in ("行业","概念"):
        leader=next((r for r in report["rows"] if r["type"]==kind and r["net"]>0),None)
        if leader:
            value=service.detail(leader["code"])
            report["details"][leader["code"]]={k:v for k,v in value.items() if k!="sector"}
    if len(report["calendar"])<20 or len(report["rows"])<100:
        raise ValueError("板块或交易日覆盖不足，原基线保留")
    for row in report["rows"]:
        row["history"]=[{k:r.get(k) for k in ("date","net","rate","pct","close","rank")} for r in row["history"]]
    args.output.write_text(json.dumps(report,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    print(f"已保存{report['as_of']}研究：{len(report['rows'])}板块、{len(report['calendar'])}交易日")


if __name__=="__main__":
    main()
