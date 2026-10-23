"""把用户提供的研究输入转为可随桌面程序分发的只读基线。"""
import json
import argparse
from pathlib import Path

from analysis.market_health import market_snapshot, build_health
from analysis.market_health_fusion import build_fusion

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "results/ashare_bottom"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supplement",type=Path,help="完整更新后的原始快照（含广度历史和跨市场证据）；默认保留现有已补齐基线")
    args = parser.parse_args()
    def read(name):
        return json.loads((SOURCE / "data" / f"{name}.json").read_text())
    metrics = read("metrics")
    supplemental = args.supplement or ROOT/"config/market_health_baseline.json"
    extra = json.loads(supplemental.read_text()) if supplemental.exists() else {}
    breadth = extra.get("breadth",{})
    state = {"source": "baseline", "updated_at": None,
             "bars": {s: read(f"index_{s}")["bars"] for s in metrics["indexes"]},
             "market": {"as_of": metrics["as_of"], "values": market_snapshot(read("snapshot")), "source": "原报告全A新浪收盘快照"},
             "breadth": breadth, "observations": extra.get("observations",[]), "historical": read("long_metrics"),
             "cross":extra.get("cross",{}),
             "notes": ["载入2026-10-22研究基线及公开接口回补数据；首次进入自动更新完整数据。",
                       "原报告将−24.78%按≤−25%计为达标；本页严格判定为未达标。底部卡新增RSI，并将反转信号移至独立确认区。",
                       "原报告11年历史对照固定保存，不随最新快照重算；历史同轮事件可能相关，小样本胜率不代表未来概率。"]}
    health = build_health(state)
    fusion = build_fusion(health,state["cross"],state["bars"]["sh000001"])
    if (breadth.get("as_of") != metrics["as_of"] or breadth.get("newhigh60") is None
        or len(state["observations"]) < 5 or not breadth.get("denominators")
        or not fusion["complete"] or any(v["known"] != 5 for v in health["confirmation"].values())
        or any(r["basis_change"] is None for d in fusion["dimensions"] if d["key"] == "futures" for r in d["details"]["basis"])):
        raise SystemExit("补充快照尚未齐备；请通过完整更新采集真实数据，并用 --supplement 指定含cross的原始快照。")
    (ROOT / "config/market_health_baseline.json").write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    target = ROOT / "webui/static/research/ashare_bottom_20261022.html"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes((SOURCE / "report.html").read_bytes())
    print(f"基线：{len(state['bars'])}指数 / {state['market']['values']['total']}个股；报告已加入桌面资源。")


if __name__ == "__main__":
    main()
