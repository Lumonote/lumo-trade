"""运行真实前端渲染器，防止低恐慌反转成贪婪及卡片口径串用。"""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行前端渲染回归")
def test_panic_badges_and_market_cards_use_original_scores_and_twenty_days():
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const meta = {tagName:"DIV", children:[], append(node) {this.children.push(node);}};
const document = {
  getElementById: id => id === "marketIndexMeta" ? meta : null,
  querySelector: () => null, querySelectorAll: () => [],
  createElement: () => ({dataset:{}}),
};
const window = {renderOverview() {}};
const context = vm.createContext({document, window,
  ResizeObserver: class {observe() {} unobserve() {}},
  IntersectionObserver: class {observe() {} unobserve() {}},
});
vm.runInContext(fs.readFileSync("webui/static/lumo_panic.js", "utf8"), context);
const low = {available:true, score:29.2, level:"low", label:"恐慌偏低", change_periods:20};
let markup = window.LumoPanic.badge(low, "大盘恐慌");
assert(markup.includes("29.2"));
assert(!markup.includes("70.8") && !markup.includes("贪婪"));
assert(markup.includes("20日"));
assert(window.LumoPanic.badge({available:false, score:null}).includes("数据不足"));
window.renderOverview({market:{panic:{...low,score:70.8},primary_index:{panic:low}}});
assert(meta.children[0].innerHTML.includes("29.2"));
assert(!meta.children[0].innerHTML.includes("70.8"));
vm.runInContext(fs.readFileSync("webui/static/lumo_operations.js", "utf8"), context);
const sectors = {innerHTML:"",querySelectorAll:()=>[],querySelector:()=>null};
const host = {innerHTML:"",querySelectorAll:()=>[],
  querySelector:s=>s==="#opSectorBody"?sectors:s==="[data-op-market]"?{}:null};
const data = {as_of:{index:"2026-10-16"}, sectors:{}, indices:[
  {name:"上证指数",close:3727.56,chg_5d:123,chg_20d:-12,panic:{...low,score:63.9,level:"high",label:"恐慌偏高"}}
]};
window.LumoOperations.renderMarketPulse(host,data);
assert(host.innerHTML.includes("20日 -12.00%") && !host.innerHTML.includes("123.00%"));
assert(host.innerHTML.includes("63.9"));
data.indices[0].panic.score=65.2;
window.LumoOperations.renderMarketPulse(host,data);
assert(host.innerHTML.includes("65.2"));
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
