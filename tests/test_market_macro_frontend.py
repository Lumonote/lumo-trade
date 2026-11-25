"""Macro overview rendering preserves dates, missing values and refresh outcomes."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行宏观前端回归")
def test_macro_overview_filters_dates_and_refresh_states():
    script = r'''
const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const window={};
vm.runInNewContext(fs.readFileSync("webui/static/lumo_market_macro.js","utf8"),{
  window,document:{getElementById:()=>null}
});
const view=window.LumoMacroOverview;
const rows=[
  {key:"pmi",name:"制造业PMI",unit:"%",frequency:"monthly",group:"growth",value:50,
   date:"2026-10-31",period:"2026-10",previous_date:"2026-09-30",change:0,change_unit:"个百分点",
   change_label:"较上期",status:"fresh",status_label:"观察期内"},
  {key:"real10y",name:"美国实际利率",unit:"%",frequency:"daily",group:"external",value:-.2,
   date:"2026-11-20",change:-2,change_unit:"bp",status:"stale",status_label:"待更新"},
  {key:"m1",name:"M1同比",unit:"%",frequency:"monthly",group:"liquidity",value:null,
   date:null,change:null,status:"missing",status_label:"未取得"}
];
assert.equal(view.filterIndicators(rows).length,3);
assert.deepEqual(Array.from(view.filterIndicators(rows,"all","changed"),r=>r.key),["real10y"]);
assert.deepEqual(Array.from(view.filterIndicators(rows,"all","attention"),r=>r.key),["real10y","m1"]);
assert.equal(view.filterIndicators(rows,"growth","attention").length,0);
let html=view.indicatorCards(rows,"pmi");
assert(html.includes('data-mm-factor="pmi"'));
assert(html.includes('aria-pressed="true"'));
assert(html.includes("0.00 个百分点"));
assert(html.includes("-2.00 bp"));
assert(html.includes("-0.20"));
assert(html.includes("观测 2026-10"));
assert(html.includes("对比观测 2026-09-30"));
assert(html.includes("原始发布日期未提供"));
assert(html.includes("未取得"));
assert(!html.includes("NaN"));
assert(view.indicatorCards([],"pmi").includes("没有匹配指标"));
const data={as_of:"2026-10-31",today:"2026-11-25",metrics:[{...rows[0]}, {...rows[1]}, {...rows[2]}]};
const fallback=view.indicators(data);
assert.equal(fallback[2].status,"missing");
assert.equal(fallback[0].change,null);
assert.equal(view.indicators({...data,metrics:[{...rows[0],date:"2025-10-31"}]})[0].status,"stale");
html=view.overview(data);
assert(html.includes("研究快照 2026-10-31"));
assert(html.includes("时效评估 2026-11-25"));
assert(!html.includes("今日"));

assert(view.jobStatus({status:"running",progress:30}).busy);
assert(view.jobStatus({status:"running",progress:30}).text.includes("30%"));
assert.equal(view.jobStatus({status:"failed",error:"来源不可用"}).text,"来源不可用");
assert.equal(view.jobStatus({status:"cancelled"}).state,"unknown");
assert(!view.jobStatus({status:"cancelled"}).text.includes("更新完成"));
const partial=view.jobStatus({status:"finished",result:{notes:["财政读取失败，保留原日期"]}});
assert.equal(partial.state,"partial");
assert(partial.text.includes("部分更新完成"));
assert(partial.text.includes("保留原日期"));
assert.equal(view.jobStatus({status:"finished",result:{notes:[]}},["上次错误"]).state,"finished");

const hostile='<img src=x onerror="alert(1)">';
html=view.indicatorCards([{...rows[0],name:hostile,change_label:hostile,key:hostile,interpretation:hostile}],"pmi");
assert(!html.includes("<img"));assert(html.includes("&lt;img"));
html=view.dimensions({briefing:{indicators:rows,dimensions:[{key:"growth",name:hostile,tone:hostile,
  label:hostile,summary:hostile,evidence:[hostile],watch:[hostile],factor_keys:["pmi"]}]}});
assert(!html.includes("<img"));assert(html.includes("mm-direction-neutral"));
html=view.watchlist({briefing:{watchlist:[{title:hostile,reason:hostile,factor_key:hostile}],notes:[hostile]}});
assert(!html.includes("<img"));
html=view.watchlist({briefing:{indicators:[{key:"gdp",frequency:"quarterly"},rows[0]],
  watchlist:[{title:"GDP待确认",factor_key:"gdp"},{title:"PMI待确认",factor_key:"pmi"}]}});
assert(html.includes("季频环境指标，见指标观察台"));
assert(!html.includes('data-mm-factor="gdp"'));
assert(html.includes('data-mm-factor="pmi"'));
html=view.methodNotes({briefing:{watchlist:[],notes:["观察窗口不是法定披露截止日",hostile]}});
assert(html.includes("观察窗口不是法定披露截止日"));
assert(!html.includes("<img"));assert(html.includes("&lt;img"));

const applied={equity:"csi300",factor:"gold",frequency:"daily",window:252,lag:0};
const draft={equity:"csi500",factor:"m2",frequency:"monthly",window:60,lag:1,category:"fiscal",keyword:"预算"};
const params=view.appliedParameters(applied,draft);
for(const key of Object.keys(applied))assert.equal(params[key],applied[key]);
assert.equal(params.category,"fiscal");assert.equal(params.keyword,"预算");
assert.equal(draft.equity,"csi500");
assert.equal(view.appliedParameters(null,draft).equity,"csi500");

const chartRows=[{date:"2026-11-20",value:null},{date:"2026-11-21",value:0},
  {date:"2026-11-22",value:""},{date:"2026-11-23",value:NaN},
  {date:"2026-11-24",value:-1},{date:"invalid",value:9},{date:"2026-02-30",value:4}];
const sanitized=view.chartGroups([{name:"债券发行",rows:chartRows}])[0].rows;
assert.deepEqual(Array.from(sanitized,r=>r.value),[0,-1]);
assert.equal(chartRows.length,7);
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
