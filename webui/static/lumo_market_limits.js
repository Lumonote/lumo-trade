/* 涨跌停独立日期区间，不受大盘盘中/收盘视图切换影响。 */
(() => {
  "use strict";
  const root = document.getElementById("mhView-limits");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num = (v, d=0) => v == null ? "—" : Number(v).toLocaleString("zh-CN", {maximumFractionDigits:d});
  const pct = v => v == null ? "—" : `${num(v,2)}%`;
  const money = v => v == null ? "—" : `${num(v / 1e8,3)}亿`;
  const clock = v => {const text=String(v||"").replace(/\.0+$/,"");const padded=/^\d{1,6}$/.test(text)&&Number(text)>0?text.padStart(6,"0"):text;return /^\d{6}$/.test(padded)?`${padded.slice(0,2)}:${padded.slice(2,4)}:${padded.slice(4)}`:esc(padded||"—");};
  const kinds = {U:"涨停", D:"跌停", Z:"炸板", O:"跌停开板"};
  const pages = {}, size = 50;
  const urlParams = new URLSearchParams(location.search);
  let data = null, poll = null, sequence = 0, detailSequence = 0, started = false, selection = null, autoKey = "", requested = null;
  $("mlStart").value = urlParams.get("limitStart") || "";
  $("mlEnd").value = urlParams.get("limitEnd") || "";

  async function api(url, options={}) {
    const response = await fetch(url, {...options, headers:{"Content-Type":"application/json"}});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "涨跌停数据读取失败");
    return value;
  }
  function error(message="") { $("mlError").hidden = !message; $("mlError").textContent = message; }
  function remember() {
    const url = new URL(location.href);
    url.searchParams.set("limitStart", data.start); url.searchParams.set("limitEnd", data.end);
    history.replaceState(null,"",url);
  }
  function tags(pattern) {
    const p = pattern || {};
    return `<div class="ml-tags">${(p.tags || []).map(t => `<span class="ml-tag" title="${esc(t.evidence)}">${esc(t.label)}</span>`).join("") || `<small>${esc(p.as_of ? (p.status === "已计算" ? "无命中形态" : p.status) : p.status || "待补日K")}</small>`}</div><small>${esc(p.as_of || "未取得形态日期")} · ${esc(p.status || "待补")}</small>`;
  }
  const link = (kind, r) => `<button type="button" class="mh-inline-link" data-ml-kind="${kind}" data-ml-code="${esc(r.code)}">${esc(r.name)} ${esc(r.code)}</button>`;
  function summary(events) {
    const result = {U:0,D:0,Z:0,O:0,stocks:new Set(events.map(r=>r.code)).size,max_up:0,max_down:0,up_dates:[],down_dates:[],
      up_stocks:new Set(events.filter(r=>r.kind==="U").map(r=>r.code)).size,
      down_stocks:new Set(events.filter(r=>r.kind==="D").map(r=>r.code)).size};
    for (const r of events) {
      result[r.kind]++;
      if (r.kind === "U") result.max_up = Math.max(result.max_up,r.streak);
      if (r.kind === "D") result.max_down = Math.max(result.max_down,r.streak);
    }
    result.up_dates = [...new Set(events.filter(r=>r.kind==="U").map(r=>r.date))].sort();
    result.down_dates = [...new Set(events.filter(r=>r.kind==="D").map(r=>r.date))].sort();
    return result;
  }
  function filtered() {
    const keyword = $("mlSearch").value.trim().toLowerCase(), kind = $("mlKind").value;
    const type = $("mlType").value, tag = $("mlPattern").value, streak = Number($("mlStreak").value);
    const boardMap = new Map(data.sectors.map(r=>[r.code,r]));
    return data.events.filter(r => {
      const boards = r.sectors.map(c=>boardMap.get(c)).filter(Boolean);
      const text = `${r.name} ${r.code} ${boards.map(b=>`${b.name} ${b.code}`).join(" ")}`.toLowerCase();
      return (!kind || r.kind===kind) && (!keyword || text.includes(keyword))
        && (!type || boards.some(b=>b.type===type))
        && (!streak || (["U","D"].includes(r.kind) && r.streak>=streak))
        && (!tag || [r.pattern,...boards.map(b=>b.pattern)].some(p=>(p?.tags||[]).some(t=>t.key===tag)));
    });
  }
  function table(id, headings, rows, render) {
    const count = Math.max(1,Math.ceil(rows.length/size));
    pages[id] = Math.max(1,Math.min(pages[id] || 1,count));
    const offset = (pages[id]-1)*size;
    return `<div class="mh-table-wrap ml-scroll"><table><thead><tr>${headings.map(h=>`<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.slice(offset,offset+size).map(render).join("") || `<tr><td colspan="${headings.length}" class="ml-empty">${data?.available ? "当前区间或筛选没有匹配记录" : "尚无区间快照，请更新区间数据"}</td></tr>`}</tbody></table></div><div class="ml-pager"><span>${rows.length}条 · ${pages[id]}/${count}页</span><button type="button" class="mh-inline-link" data-ml-page="${id}:${pages[id]-1}" ${pages[id]<=1?"disabled":""}>上一页</button><button type="button" class="mh-inline-link" data-ml-page="${id}:${pages[id]+1}" ${pages[id]>=count?"disabled":""}>下一页</button></div>`;
  }
  function groups(events, kind) {
    const map = new Map();
    for (const r of events) for (const code of kind==="stock" ? [r.code] : r.sectors) {
      if (!map.has(code)) map.set(code,[]);
      map.get(code).push(r);
    }
    const sort = $("mlSort").value;
    return data[kind==="stock"?"stocks":"sectors"].filter(r=>map.has(r.code) && (kind==="stock" || !$("mlType").value || r.type===$("mlType").value))
      .map(r=>({...r,...summary(map.get(r.code))})).sort((a,b)=>(b[sort]||0)-(a[sort]||0)||a.code.localeCompare(b.code));
  }
  function rowsTable(id, rows, kind) {
    const dates = values => values.length>3 ? `<details><summary>${values.length}天 · 展开全部日期</summary>${esc(values.join("、"))}</details>` : esc(values.join("、")||"—");
    return table(id,[kind==="stock"?"股票":"板块 / 类型","涨停次数 / 家数","跌停次数 / 家数","炸板 / 跌停开板","最高连板 / 连跌停","涨停日期","跌停日期","条件选股形态 · 截止日"],rows,r=>`<tr><td>${link(kind,r)}<small>${esc(kind==="sector"?r.type:`关联${r.sectors.length}个板块`)}</small></td><td class="mh-positive">${r.U}${kind==="sector"?` / ${r.up_stocks}家`:""}</td><td class="mh-negative">${r.D}${kind==="sector"?` / ${r.down_stocks}家`:""}</td><td>${r.Z} / ${r.O}</td><td>${r.max_up} / ${r.max_down}<small>连续数详见事件，可能为下界</small></td><td class="ml-date-list">${dates(r.up_dates)}</td><td class="ml-date-list">${dates(r.down_dates)}</td><td>${tags(r.pattern)}</td></tr>`);
  }
  function eventTable(id, events) {
    const boardMap = new Map(data.sectors.map(r=>[r.code,r]));
    const boards = r => r.sectors.length ? `<details class="ml-event-boards"><summary>关联 ${r.sectors.length} 个板块</summary>${r.sectors.map(c=>boardMap.get(c)).filter(Boolean).map(b=>link("sector",b)).join("")}</details>` : "未映射（股票记录保留）";
    return table(id,["日期 / 股票","事件","连板 / 连跌停","连续下跌日","收盘 / 涨跌幅","涨停价 / 跌停价","一字板","首次 / 最后封板","开板次数","封单金额","成交额 / 换手率","涨停统计 / 源连板","板上成交 / 流通市值 / 总市值","当天所属板块","个股形态 · 截止日"],events,r=>`<tr><td>${esc(r.date)}<br>${link("stock",r)}</td><td class="ml-kind-${r.kind}">${esc(kinds[r.kind])}</td><td>${["U","D"].includes(r.kind)?`${r.streak_lower_bound?"至少 ":""}${r.streak}`:"—"}</td><td>${r.fall_lower_bound?"至少 ":""}${num(r.fall_days)}</td><td>${num(r.close,2)} / ${pct(r.pct)}</td><td>${num(r.up_limit,2)} / ${num(r.down_limit,2)}</td><td>${r.one_price?"是":"否"}</td><td>${clock(r.first_time)} / ${clock(r.last_time)}</td><td>${num(r.open_times)}</td><td>${money(r.fd_amount)}</td><td>${money(r.amount)} / ${pct(r.turnover_ratio)}</td><td>${esc(r.up_stat||"—")} / ${num(r.source_streak)}</td><td>${money(r.limit_amount)} / ${money(r.float_mv)} / ${money(r.total_mv)}</td><td>${boards(r)}</td><td>${tags(r.pattern)}</td></tr>`);
  }
  const kpi = (label,value,note) => `<div class="mh-kpi"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  function render() {
    const events = filtered(), stats = summary(events), sectors = groups(events,"sector"), stocks = groups(events,"stock");
    $("mlSummary").innerHTML = `<div class="mh-kpis">${kpi("涨停 / 跌停次数",`${stats.U} / ${stats.D}`,"股票×日期去重；随下方筛选变化")}${kpi("相关股票 / 板块",`${stocks.length} / ${sectors.length}`,"个股去重；板块成分可重叠")}${kpi("最高连板 / 连跌停",`${stats.max_up} / ${stats.max_down}`,"承接区间前连续数；缺日显示下界")}${kpi("炸板 / 跌停开板",`${stats.Z} / ${stats.O}`,"触及限价但收盘未封板")}</div>`;
    $("mlSectors").innerHTML = rowsTable("sectors",sectors,"sector");
    $("mlStocks").innerHTML = rowsTable("stocks",stocks,"stock");
    $("mlSectorCount").textContent = `${sectors.length}个板块`;
    $("mlStockCount").textContent = `${stocks.length}只股票`;
    $("mlEvents").innerHTML = eventTable("events",events);
    $("mlDaily").innerHTML = table("daily",["交易日","涨停","跌停","炸板","跌停开板","已核验股票","未核验限价记录"],[...data.daily].reverse(),r=>`<tr><td>${esc(r.date)}${r.available?"":" · 待补数据"}</td><td>${num(r.U)}</td><td>${num(r.D)}</td><td>${num(r.Z)}</td><td>${num(r.O)}</td><td>${num(r.covered)}</td><td>${num(r.unverified)}</td></tr>`);
    $("mlExport").disabled = !data.available; $("mlCsv").disabled = !data.available;
  }
  function jobState(job) {
    clearTimeout(poll);
    const busy = job && ["queued","running"].includes(job.status);
    $("mlRefresh").disabled = Boolean(busy);
    $("mlJob").hidden = !job;
    if (job) $("mlJob").innerHTML = `${busy?`<progress max="100" value="${Math.min(100,Math.max(0,job.progress||0))}"></progress>`:""}<strong>${esc({queued:"已排队",running:"正在回补",finished:"更新完成",failed:"更新失败"}[job.status] || job.status)} ${job.params?.start?esc(`${job.params.start} 至 ${job.params.end}`):""}</strong><small>${esc(job.error || job.logs?.at(-1) || "正在核验数据，首次回补可能需要数分钟")}</small>`;
    if (busy) poll = setTimeout(()=>load(false),2500);
  }
  async function load(autofill=false) {
    clearTimeout(poll);
    const id = ++sequence;
    const params = requested || {start:$("mlStart").value,end:$("mlEnd").value};
    try {
      const value = await api("/api/market-health/limits?"+new URLSearchParams(params));
      if (id!==sequence) return;
      data = value; requested = {start:value.start,end:value.end}; error();
      $("mlStart").value = data.start; $("mlEnd").value = data.end; remember();
      $("mlMeta").textContent = `${data.start} 至 ${data.end} · 实际收盘数据截至 ${data.as_of||"未取得"} · ${data.updated_at?`更新 ${data.updated_at}`:"尚无快照"}`;
      $("mlStatus").textContent = [data.message,`完整日数据 ${data.daily.filter(r=>r.available).length}/${data.calendar.length}个交易日`,data.pattern_coverage?`形态已计算 ${data.pattern_coverage.computed}/${data.pattern_coverage.total}项`:"",data.unmapped_events?`${data.unmapped_events}条事件尚未关联板块`:"",...(data.notes||[]),"形态筛选匹配个股或其所属板块；标签日期见各行。"].filter(Boolean).join("；");
      const chosen = $("mlPattern").value;
      $("mlPattern").innerHTML = '<option value="">全部形态</option>'+(data.pattern_catalog||[]).map(p=>`<option value="${esc(p.key)}">${esc(p.label)}</option>`).join("");
      $("mlPattern").value = chosen;
      render(); jobState(data.job);
      if (selection) choose(selection.kind,selection.code,false);
      const key = `${data.start}:${data.end}`;
      if (autofill && !data.available && autoKey!==key && !["queued","running"].includes(data.job?.status)) { autoKey=key; await refresh(); }
    } catch (e) { if (id===sequence) {error(e.message); $("mlRefresh").disabled=false;} }
  }
  async function refresh() {
    if (!$("mlForm").reportValidity()) return;
    error(); $("mlRefresh").disabled=true;
    requested = {start:$("mlStart").value,end:$("mlEnd").value};
    try {
      const value = await api("/api/market-health/limits/refresh",{method:"POST",body:JSON.stringify(requested)});
      jobState(value.job);
    } catch (e) {error(e.message); $("mlRefresh").disabled=false;}
  }
  async function choose(kind,code,focus=true) {
    const id=++detailSequence; selection={kind,code}; pages.detailEvents=1; pages.detailStocks=1;
    $("mlDetail").hidden=false;
    if (focus) $("mlDetail").innerHTML='<div class="mh-box" role="status">正在读取完整档案…</div>';
    try {
      const value = await api("/api/market-health/limits/detail?"+new URLSearchParams({kind,code,start:data.start,end:data.end}));
      if(id!==detailSequence)return;
      const r=value.row;
      $("mlDetail").innerHTML=`<article class="mh-box ml-detail"><div class="mh-box-head"><div><h3>${esc(r.name)} ${esc(r.code)} · ${kind==="sector"?esc(r.type):"股票"}完整档案</h3><p>${esc(value.start)} 至 ${esc(value.end)} · 此处展示完整区间，不受上方事件与形态筛选影响。</p></div><button class="button secondary" type="button" data-ml-close>关闭档案</button></div><div class="mh-kpis">${kpi("涨停 / 跌停次数",`${r.U} / ${r.D}`,`最高连板 ${r.max_up} · 最高连跌停 ${r.max_down}`)}${kpi("炸板 / 跌停开板",`${r.Z} / ${r.O}`,"实际价格触及限价")}${kpi("涨停日期",r.up_dates.length+"天",r.up_dates.join("、")||"无")}${kpi("跌停日期",r.down_dates.length+"天",r.down_dates.join("、")||"无")}</div><h3>${kind==="sector"?"板块自身":"个股"}形态</h3>${tags(r.pattern)}<p class="mh-note">${esc(data.pattern_basis||"")}</p><h3>全部关联板块（各板块的全区间统计可继续点选）</h3><div class="ml-related">${value.sectors.map(b=>`<span>${link("sector",b)} · 涨停${b.U}次 / 跌停${b.D}次 ${tags(b.pattern)}</span>`).join("")||"历史板块成分尚未取得"}</div>${kind==="sector"?`<h3>全部相关股票 · 各股全区间统计</h3><div id="mlDetailStocks">${rowsTable("detailStocks",value.stocks,"stock")}</div>`:""}<h3>${kind==="sector"?"板块内":"个股"}全部逐日事件 · ${value.events.length}条</h3><div id="mlDetailEvents">${eventTable("detailEvents",value.events)}</div>${kind==="stock"?`<button type="button" class="button secondary" data-ml-analysis="${esc(code)}" data-ml-name="${esc(r.name)}">打开个股分析</button>`:""}</article>`;
      selection.value=value;
      if(focus){$("mlDetail").focus({preventScroll:true});$("mlDetail").scrollIntoView({behavior:"smooth",block:"start"});}
    } catch(e) {if(id===detailSequence)$("mlDetail").innerHTML=`<div class="mh-box mh-notice" role="alert">${esc(e.message)} <button type="button" class="mh-inline-link" data-ml-close>关闭</button></div>`;}
  }
  function download(content,name,type) {
    const url=URL.createObjectURL(new Blob([content],{type}));const a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  root.addEventListener("click",e=>{
    const item=e.target.closest("[data-ml-code]"), pager=e.target.closest("[data-ml-page]"), quick=e.target.closest("[data-ml-days]");
    if(item)choose(item.dataset.mlKind,item.dataset.mlCode);
    if(pager){const [id,page]=pager.dataset.mlPage.split(":");pages[id]=Number(page);if(id==="detailEvents"&&selection?.value)$("mlDetailEvents").innerHTML=eventTable(id,selection.value.events);else if(id==="detailStocks"&&selection?.value)$("mlDetailStocks").innerHTML=rowsTable(id,selection.value.stocks,"stock");else render();}
    if(e.target.closest("[data-ml-close]")){++detailSequence;selection=null;$("mlDetail").hidden=true;}
    const analysis=e.target.closest("[data-ml-analysis]");
    if(analysis && typeof window.openStockKlineModal==="function")window.openStockKlineModal(analysis.dataset.mlAnalysis,240,{stockName:analysis.dataset.mlName}).catch(e=>error(e.message));
    if(quick){const today=new Date(),end=today.toLocaleDateString("sv-SE",{timeZone:"Asia/Shanghai"});today.setDate(today.getDate()-Number(quick.dataset.mlDays));$("mlStart").value=today.toLocaleDateString("sv-SE",{timeZone:"Asia/Shanghai"});$("mlEnd").value=end;$("mlForm").requestSubmit();}
  });
  $("mlForm").addEventListener("submit",e=>{e.preventDefault();selection=null;++detailSequence;$("mlDetail").hidden=true;requested={start:$("mlStart").value,end:$("mlEnd").value};Object.keys(pages).forEach(k=>delete pages[k]);load(true);});
  ["mlType","mlKind","mlPattern","mlStreak","mlSort"].forEach(id=>$(id).addEventListener("change",()=>{Object.keys(pages).forEach(k=>delete pages[k]);if(data)render();}));
  $("mlSearch").addEventListener("input",()=>{Object.keys(pages).forEach(k=>delete pages[k]);if(data)render();});
  $("mlRefresh").addEventListener("click",refresh);
  $("mlExport").addEventListener("click",()=>download(JSON.stringify(data,null,2),`涨跌停研究_${data.start}_${data.end}.json`,"application/json;charset=utf-8"));
  $("mlCsv").addEventListener("click",()=>{
    const columns={date:"日期",code:"股票代码",name:"名称",label:"事件",streak:"连续封板",streak_lower_bound:"连续数为下界",fall_days:"连续下跌天数",close:"收盘价",pct:"涨跌幅%",up_limit:"涨停价",down_limit:"跌停价",first_time:"首次封板",last_time:"最后封板",open_times:"开板次数",fd_amount:"封单金额元",amount:"成交额元",turnover_ratio:"换手率%",up_stat:"涨停统计",source_streak:"来源连板数",limit_amount:"板上成交元",float_mv:"流通市值元",total_mv:"总市值元",sector_names:"当天板块",pattern_names:"形态标签",pattern_date:"形态日期"};
    const cell=v=>{let text=String(v??"");if(/^[=+\-@]/.test(text)&&!/^[-+]?\d+(\.\d+)?$/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';};
    const records=filtered().map(r=>({...r,sector_names:r.sectors.map(c=>data.sectors.find(b=>b.code===c)?.name||c).join("、"),pattern_names:(r.pattern.tags||[]).map(t=>t.label).join("、"),pattern_date:r.pattern.as_of}));
    download("\uFEFF"+[Object.values(columns),...records.map(r=>Object.keys(columns).map(k=>r[k]))].map(r=>r.map(cell).join(",")).join("\r\n"),`涨跌停记录_${data.start}_${data.end}.csv`,"text/csv;charset=utf-8");
  });
  function begin(){if(!root.hidden&&!started){started=true;load(true);}}
  new MutationObserver(begin).observe(root,{attributes:true,attributeFilter:["hidden"]});begin();
  document.addEventListener("market-health-full-refresh",()=>{if(started)refresh();});
  window.addEventListener("pagehide",()=>{clearTimeout(poll);++sequence;++detailSequence;});
})();
