"""Refresh status keeps disclosure notes and actual errors visible together."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行前端状态回归")
def test_capital_refresh_status_preserves_notes_errors_and_legacy_range_results():
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const source = fs.readFileSync("webui/static/lumo_market_capital.js", "utf8");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {hidden:true,innerHTML:"",textContent:"",
    value:"",addEventListener(){}});
  return elements.get(id);
}
const window = {addEventListener(){}};
const context = vm.createContext({window,
  document:{getElementById:element,addEventListener(){}},
  MutationObserver:class {observe(){}},
});
vm.runInContext(source.replace(/\}\)\(\);\s*$/, "window.statusForTest=refreshStatus;})();"),context);
const status = window.statusForTest;
const note = "2026-11-25 两融数据按次日披露，当前截至 2026-11-24";
const error = "个股两融接口超时";
const finished = result => ({status:"finished",result});
const once = (text,part) => assert.equal(text.split(part).length-1,1,text);

// New result fields must combine with the current data's messages.
let text = status({notes:[note],errors:[error],job:finished({as_of:"2026-11-24",notes:[note],errors:[error]})},false);
assert(text.startsWith("更新完成 · 数据截至 2026-11-24"));
once(text,note);once(text,error);

// An empty job result.notes used to suppress data.errors entirely.
text = status({notes:[],errors:[error],job:finished({notes:[],errors:[]})},false);
once(text,error);
assert.equal(status({notes:[],errors:[]},false),"");
assert.equal(status({},false),"");
assert.equal(status({notes:null,errors:[error]},false),error);
assert.equal(status({notes:[note]},false),note);
assert.equal(status({job:finished({errors:[error]})},false),"更新完成 · "+error);

// Old jobs stored real errors inside notes; retain those and de-duplicate them.
text = status({notes:[note],errors:[error],job:finished({notes:[error,error]})},false);
once(text,note);once(text,error);

// Participant range jobs keep their existing processed/remaining-day explanation.
text = status({errors:[error],job:{...finished({notes:["部分日期待补"],processed_days:5,
  amount_days:3,remaining_days:2}),params:{participant:"etf",start:"2026-11-16",end:"2026-11-24"}}},false);
assert(text.startsWith("ETF 2026-11-16 至 2026-11-24 · 已处理 5 个交易日，其中 3 日有可估算净额"));
assert(text.includes("还有 2 个交易日待检查"));
once(text,"部分日期待补");once(text,error);

// Running and failed statuses keep snapshot notes visible without repeating logs/errors.
text = status({notes:[note],errors:[error],job:{status:"running",progress:40,logs:[note]}},true);
assert(text.startsWith("更新中 40%"));once(text,note);once(text,error);
text = status({notes:[note],errors:[error],job:{status:"failed",error}},false);
assert(text.startsWith(error));once(text,note);once(text,error);
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
