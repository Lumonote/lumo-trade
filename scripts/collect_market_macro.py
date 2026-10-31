"""采集可分发的公开宏观研究基线；不保存Token或账户配置。"""
import gzip
import json

from webui.services.market_macro_service import BASELINE, baseline, collect


if __name__ == "__main__":
    result = collect(lambda message: print(message, flush=True), old=baseline())
    temporary = BASELINE.with_suffix(".tmp.gz")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    temporary.replace(BASELINE)
    print(json.dumps({"as_of": result["as_of"], "series": {k: len(v) for k, v in result["series"].items()},
                      "sectors": len(result["sectors"]), "fiscal": len(result["fiscal"]), "debt": len(result["debt"]),
                      "calendar": len(result["calendar"]), "errors": result["errors"]}, ensure_ascii=False), flush=True)
