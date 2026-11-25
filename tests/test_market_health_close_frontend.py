"""Exercise the real refresh loop across an intraday-to-close transition."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行前端刷新回归")
def test_close_refresh_runs_after_intraday_refresh_and_retries_without_duplicate_jobs():
    script = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const source = fs.readFileSync("webui/static/lumo_market_health.js", "utf8");
const today = "2026-11-25", yesterday = "2026-11-24";
const observation = day => ({current:{as_of:day,sh_sz_turnover_yuan:100}});
function data({pending=false,closed=false,samples=true,intraday=true,busy=false}={}) {
  return {
    as_of:closed ? today : yesterday, source:"updated", updated_at:"saved",
    indices:[{symbol:"sh000001"}], timing:[], fusion:{updated_at:"saved"},
    market:{as_of:closed && samples ? today : yesterday},
    breadth:{as_of:closed && samples ? today : yesterday},
    market_internals:observation(closed ? today : yesterday),
    live_status:{as_of:today,available:!closed,close_pending:pending,message:"same message"},
    job:{id:"previous",status:busy ? "running" : "finished"},
    intraday:intraday && !closed ? {
      as_of:today,quote_at:today+"T15:00:00",confirmation_as_of:yesterday,
      indices:[{symbol:"sh000001"}],timing:[],fusion:{updated_at:"saved"},
      market:{as_of:today},breadth:{as_of:today},market_internals:observation(today),
    } : null,
  };
}
function setup(initial) {
  const nodes = new Map(), posts = [];
  let value = initial, milliseconds = 1000000;
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {
      textContent:"",innerHTML:"",options:[],dataset:{},
      addEventListener(){},setAttribute(){},querySelectorAll(){return [];},
    });
    return nodes.get(id);
  };
  element("mhTabs").querySelectorAll = () => [{dataset:{mhTab:"overview"}}];
  const window = {addEventListener(){}};
  const context = vm.createContext({
    window,document:{hidden:false,getElementById:element,dispatchEvent(){}},
    location:{search:"",href:"http://localhost/desktop/market_health"},
    history:{replaceState(){}}, URL, URLSearchParams,
    Date:class extends Date {static now(){return milliseconds;}},
    setTimeout:()=>1,clearTimeout(){},CustomEvent:class {},
    fetch:async (url,options={}) => {
      if (options.method === "POST") {
        posts.push(JSON.parse(options.body).mode);
        return {ok:true,json:async()=>({job:{id:"new",status:"queued"}})};
      }
      return {ok:true,json:async()=>JSON.parse(JSON.stringify(value))};
    },
  });
  // Keep real load/refresh/timing/title rendering; silence unrelated charts.
  const instrumented = source.replace("selectTab(section);load();", `
    renderOverview=renderCross=renderSignals=renderIndices=renderBreadth=
      renderConfirmation=renderHistory=renderMethod=updateCorrelationSummaries=
      updateDailyBackfillButton=()=>{};
    window.test={load,refresh,setMode(mode){viewMode=mode;activatePayload();render();}};
  `);
  assert.notEqual(instrumented,source);
  vm.runInContext(instrumented,context);
  return {posts,nodes,api:window.test,
    set(next){value=next;},advance(ms){milliseconds+=ms;},
    async load(){await window.test.load(true);await new Promise(resolve=>setImmediate(resolve));},
  };
}
(async () => {
  // A morning automatic refresh must not consume the same day's closing refresh.
  const initial=data();initial.intraday.breadth.as_of=yesterday;
  const app=setup(initial);
  await app.load();
  assert.deepEqual(app.posts,["full"]);
  app.set(data({pending:true}));
  await app.load();
  assert.deepEqual(app.posts,["full","full"]);
  assert(app.nodes.get("mhMeta").textContent.includes("今日收盘数据待更新"));
  app.api.setMode("close");
  assert(app.nodes.get("mhMeta").textContent.includes(yesterday));
  assert(app.nodes.get("mhMeta").textContent.includes("今日收盘数据待更新"));
  app.advance(59999);await app.load();
  assert.deepEqual(app.posts,["full","full"]);
  app.advance(1);await app.load();
  assert.deepEqual(app.posts,["full","full","indices"]);
  await app.load();
  assert.deepEqual(app.posts,["full","full","indices"]);

  // Once the index source catches up, complete a missing closing sample once.
  app.set(data({closed:true,samples:false}));await app.load();
  assert.deepEqual(app.posts,["full","full","indices","full"]);
  await app.load();
  assert.equal(app.posts.length,4);
  app.set(data({closed:true}));app.advance(60000);await app.load();
  assert.equal(app.posts.length,4);
  assert(app.nodes.get("mhMeta").textContent.includes(today));
  assert(!app.nodes.get("mhMeta").textContent.includes("待更新"));

  // Do not dispatch while a job is running; fresh quotes remain available at close.
  const busy=setup(data({pending:true,busy:true}));await busy.load();
  assert.deepEqual(busy.posts,[]);
  busy.set(data({pending:true}));await busy.load();
  assert.deepEqual(busy.posts,["full"]);

  // Manual refresh performs a forced GET; that nested load must not submit again.
  const manual=setup(data({pending:true}));
  await manual.api.refresh("full");
  assert.deepEqual(manual.posts,["full"]);

  // An outdated quote before close never triggers a closing refresh.
  const stale=setup(data({intraday:false}));
  const previous=data({intraday:false});previous.live_status.available=false;
  stale.set(previous);await stale.load();
  assert.deepEqual(stale.posts,[]);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
