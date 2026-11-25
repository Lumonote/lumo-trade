"""Execute the standalone derivatives renderer without network or an application DB."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行衍生品前端回归")
def test_derivatives_renderer_dates_missing_values_and_escape():
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const window = {};
vm.runInNewContext(fs.readFileSync("webui/static/lumo_market_derivatives.js","utf8"), {window});
const render = window.LumoMarketDerivatives.render;
let html = render();
assert.equal((html.match(/data-md-product=/g)||[]).length,4);
assert(html.includes("等待衍生品数据"));
assert.equal((html.match(/持仓覆盖不足/g)||[]).length,4);
assert(!html.includes('style="width:0%"'));
assert(!html.includes("NaN"));
assert(html.includes("当前没有接入期权数据"));

const product = {variety:"IF",name:"沪深300",position_date:"2026-11-24",position_status:"lagged",
  direction:"bullish",direction_label:"偏多",reason:"多单增加，空单减少",action_label:"增多减空",
  long:100,short:300,long_change:20,short_change:-10,net:-200,net_change:30,long_share_pct:25,ls_ratio:1/3,
  quote_date:"2026-11-25",quote_status:"current",quote_at:"2026-11-25T15:00:00+08:00",
  contract:"IF2612",basis_pct:-0.25,basis_change:0.125,previous_date:"2026-11-24",
  price:4200,price_change_pct:0,open_interest:18000,oi_change:0,price_position_label:"价格与持仓持平"};
const report = {as_of:"2026-11-25",summary:{label:"偏多",tone:"bullish",date:"2026-11-24",
  bullish:2,bearish:0,neutral:0,missing:2,eligible:2,total:4,reason:"两个品种同日净变化偏多"},products:[product]};
html = render(report);
assert(html.includes("上一交易日"));
assert(html.includes("观察日同日"));
assert(html.includes("席位 2026-11-24"));
assert(html.includes("行情 2026-11-25"));
assert(html.includes('style="width:25%"'));
assert(html.includes('style="width:75%"'));
assert(html.includes("+12.5 bp"));
assert(html.includes("+30"));
assert(html.includes("-200 手"));
assert(html.includes("0.00%"));
assert(html.includes("0 手"));
assert(html.includes("2 / 4 个品种纳入比较"));
assert(html.includes("data-mh-derivatives-refresh"));
assert(html.includes('data-mh-go="cross"'));
assert(html.includes('href="/desktop/futures"'));

html = render({...report,products:[{...product,direction:"unavailable",direction_label:"证据过期",
  position_status:"stale",position_date:"2026-11-18",net_change:null,long_share_pct:null,
  basis_pct:null,basis_change:null}]});
assert(html.includes("历史观测 · 不计方向"));
assert(html.includes("证据过期"));
assert(html.includes("暂无可比前日"));
assert(!html.includes('style="width:25%"'));
assert(!html.includes("+30"));

const hostile = '<img src=x onerror="alert(1)">';
html = render({...report,summary:{...report.summary,label:hostile,reason:hostile,tone:hostile},
  notes:[hostile],errors:[hostile],products:[{...product,name:hostile,source:hostile,reason:hostile,
    direction_label:hostile,position_date:hostile,contract:hostile,quote_at:hostile}]});
assert(!html.includes("<img"));
assert(!html.includes('class="md-'+hostile));
assert(html.includes("&lt;img"));
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
