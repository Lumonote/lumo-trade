"""Breadth/valuation rendering preserves incomplete samples and independent dates."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行广度估值前端回归")
def test_breadth_renderer_missing_zero_cohorts_and_valuation_distribution():
    script = r'''
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const window={};
vm.runInNewContext(fs.readFileSync("webui/static/lumo_market_health.js","utf8"),{
  window,document:{getElementById:()=>null}
});
const view=window.LumoBreadthView;
const payload={as_of:"2026-11-25",provisional:true,
  market:{as_of:"2026-11-25",provisional:true,values:{total:5000,change_sample:4900,up:2000,down:2800,flat:100,
    up_ratio:2000/49,pb_median:1.234,pe_median:30,pb_sample:4500,pe_sample:4000}},
  breadth:{as_of:"2026-11-24",sample:300,method:"fixed",cohort:"one",above_ma20:0,above_ma60:null,
    denominators:{above_ma20:300}},
  observations:[{as_of:"2026-11-23",method:"fixed",cohort:"one",sample:300},
    {as_of:"2026-11-24",method:"fixed",cohort:"two",sample:310},
    {as_of:"2026-11-24",method:"fixed",cohort:"one",sample:300}]
};
let r=view.report(payload),html=view.overview(r);
assert(html.includes("2026-11-25 · 盘中观测"));
assert(html.includes("2026-11-24 · 历史收盘"));
r.trend.provisional=true;r.trend.captured_at="2026-11-24T14:30:00+08:00";r.trend.source="真实采样来源";
html=view.overview(r);
assert(html.includes("2026-11-24 · 历史盘中"));
assert(html.includes("真实采样来源"));assert(html.includes("14:30:00"));
r.trend.provisional=false;
html=view.trend(r);
assert(html.includes('data-mh-breadth-key="above_ma20"'));
assert(html.includes('style="--position:0%"'));
assert(html.includes("实际分母 300 只"));
assert(html.includes("实际分母 — 只"));
assert(html.includes('class="mh-bar-track mh-breadth-missing" aria-label="数据缺失，不按零显示"'));
assert(!html.includes("null"));assert(!html.includes("NaN"));
assert(html.includes("暂无可比收盘变化"));
html=view.valuation(r,"pb");
assert(html.includes("1.234"));
assert(html.includes("此快照未保存估值分布"));
assert(!html.includes("P90"));
assert(html.includes('data-mh-valuation="pb" aria-pressed="true"'));
html=view.valuation(r,"pe");
assert(html.includes("30.00"));
assert(html.includes('data-mh-valuation="pe" aria-pressed="true"'));
assert.equal(view.recordRows(payload.observations,r.trend,"cohort").length,2);
assert.equal(view.recordRows(payload.observations,r.trend,"all").length,3);
assert.equal(view.recordRows(payload.observations,{},"cohort").length,0);
assert(view.records(payload,r,"all").includes("不可直接连比"));
assert(view.records(payload,{trend:{}},"cohort").includes("未保存样本组与方法"));
assert(view.records({observations:[]},r,"all").includes("暂无观测记录"));

r.valuation.pb.distribution={sample:4500,total:5000,coverage_pct:90,missing:200,nonpositive:300,
  quantiles:{p10:.7,p25:1,p50:1.234,p75:2,p90:4},
  buckets:[{label:"0 < PB < 1",count:0,pct:0},{label:"PB ≥ 1",count:4500,pct:100}]};
html=view.valuation(r,"pb");
assert(html.includes("P10"));assert(html.includes("P90"));
assert(html.includes("横截面分位"));assert(html.includes("不等于历史分位"));
assert(html.includes("非正值 300 个"));assert(html.includes("缺失 200 个"));
assert(html.includes('style="--position:0%"'));
assert(html.includes("0 &lt; PB &lt; 1"));
assert(!html.includes("未保存估值分布"));
r.trend.previous_date="2026-11-23";
r.trend.rows[0]={...r.trend.rows[0],comparable:true,change_pp:0};
assert(view.trend(r).includes("较 2026-11-23 0.00 个百分点"));

const hostile='<img src=x onerror="alert(1)">';
r.trend.rows[0].label=hostile;r.trend.rows[0].key=hostile;r.trend.note=hostile;
r.valuation.note=hostile;r.valuation.pb.distribution.buckets[0].label=hostile;
for(const rendered of [view.trend(r),view.valuation(r),view.records({...payload,observations:[{method:hostile,cohort:hostile,as_of:hostile}]},r,"all")]){
  assert(!rendered.includes("<img"));assert(rendered.includes("&lt;img"));
}
assert.equal(view.report({breadth_valuation:r}),r);
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
