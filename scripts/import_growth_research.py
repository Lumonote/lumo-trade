#!/usr/bin/env python3
"""将已采集的原始财报/预测/K线导入原生研究缓存，不依赖某次报告路径。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir",type=Path,required=True,help="含公开原始JSON的目录")
    parser.add_argument("--rebuild",action="store_true",help="导入后用当前数据库的行情重算")
    args=parser.parse_args()
    from data_store import growth_research_repo as repo
    reports={};forecasts={};qfq={}
    for path in sorted(args.input_dir.glob("*.json")):
        payload=json.loads(path.read_text());rows=payload.get("rows",[])
        for row in rows:
            code=row.get("SECURITY_CODE")
            if not code:continue
            if "EPS1" in row:forecasts[code]=row
            elif row.get("REPORTDATE"):reports.setdefault(row["REPORTDATE"][:10],{})[code]=row
    for path in sorted((args.input_dir/"qfq").glob("*.json")):
        payload=json.loads(path.read_text()).get("payload",{}).get("data") or {};bars=[]
        for raw in payload.get("klines") or []:
            k=raw.split(",");bars.append({"date":k[0],"open":float(k[1]),"close":float(k[2]),"high":float(k[3]),"low":float(k[4]),"volume":float(k[5]),"pct_change":float(k[8])})
        if bars:qfq[path.stem]=bars
    if not reports and not forecasts:parser.error("目录内没有可导入的财报或盈利预测")
    previous=repo.read("financials",{})
    from webui.services.growth_research_service import merge_financials
    repo.write("financials",merge_financials(previous,{"reports":reports,"forecasts":forecasts,"errors":[]},dt.datetime.now().isoformat()))
    repo.write("qfq",{**repo.read("qfq",{}),**qfq})
    print(json.dumps({"periods":len(reports),"forecasts":len(forecasts),"qfq":len(qfq)},ensure_ascii=False))
    if args.rebuild:
        from webui.services.growth_research_service import rebuild
        print(json.dumps(rebuild()["summary"],ensure_ascii=False))


if __name__=="__main__":main()
