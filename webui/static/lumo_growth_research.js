/* 成长研究页面。筛选和统计由后端统一执行，前端不重新解释财务规则。 */
(() => {
  "use strict";
  const root=document.getElementById("growthResearchRoot");
  if (!root) return;
  const $=id=>document.getElementById(id), form=$("grFilters");
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const n=(v,d=2)=>v==null||!Number.isFinite(Number(v))?"—":Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
  const pct=v=>v==null?"—":n(v*100,1)+"%";
  let payload=null, pool="near", page=1, tab="stocks", direction="desc", controller=null, timer=null, pollTimer=null, selected=null, bootstrapped=false;
  const initial=new URLSearchParams(location.search);
  if (initial.get("direction")==="asc") direction="asc";
  for (const el of form.elements) {
    if (!el.name||!initial.has(el.name)) continue;
    if (el.type==="checkbox") el.checked=initial.get(el.name)==="1";
    else el.value=initial.get(el.name);
  }
  if (initial.has("pool")) pool=initial.get("pool");
  function error(message="") { $("grError").hidden=!message; $("grError").textContent=message; }
  function params() {
    const q=new URLSearchParams({pool,page:String(page),page_size:"50",direction});
    for (const el of form.elements) {
      if (!el.name) continue;
      const value=el.type==="checkbox"?(el.checked?"1":"0"):el.value.trim();
      if (value!=="") q.set(el.name,value);
    }
    return q;
  }
  async function request(url,options={}) {
    const response=await fetch(url,options);
    const value=await response.json();
    if (!response.ok) throw new Error(value.error||"数据读取失败，请重试");
    return value;
  }
  function fillOptions(name,values,label) {
    const el=form.elements[name],value=el.dataset.grLoaded?el.value:(initial.get(name)||el.value||"");
    el.innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join("");
    el.value=value;
    el.dataset.grLoaded="1";
  }
  function renderMeta() {
    const s=payload.summary||{},years=payload.forecast_years||[];
    $("grMeta").textContent=payload.ready?`行情 ${payload.as_of} 收盘 · 财报 ${payload.report_date||"待补齐"} · 预期 ${years.length?years.join(" / "):"待补齐"} · 快照 ${String(payload.generated_at||"").slice(0,16).replace("T"," ")}`:"尚未建立研究快照";
    if($("refreshMeta"))$("refreshMeta").textContent="成长研究 · 本地快照";
    $("grCoverage").innerHTML=[
      ["行情样本",s.total,"有正价格的沪深京A股"],
      ["财报覆盖",s.financial_covered,"同一报告期可比"],
      ["可用EPS预测",s.forecast_covered,"缺少预测不等于没有增长"],
      ["新质成长且低位/支撑",s.near_growth,`其中 ${s.verified_near||0} 家已校验前复权K线`],
    ].map(([label,value,note])=>`<div class="gr-kpi"><span>${label}</span><strong>${n(value||0,0)}</strong><small>${esc(note)}</small></div>`).join("");
    const notes=[...(payload.notes||[])];
    if (payload.stale) notes.unshift(`行情已滞后${payload.age_days}个自然日。请先在资金榜更新全市场行情，再重算本地数据。`);
    $("grNotes").hidden=!notes.length; $("grNotes").innerHTML=notes.map(note=>`<p>${esc(note)}</p>`).join("");
    const groups=[["near","新质成长＋低位/支撑",s.near_growth],["all","全市场",s.total],["actual","实际高增长",s.actual_high],["future","未来高增长",s.future_high],...Object.entries(payload.groups||{}).filter(([k])=>k!=="other").map(([k,label])=>[k,label,s.groups?.[k]])];
    $("grGroups").innerHTML=groups.map(([key,label,count])=>`<button type="button" data-gr-pool="${esc(key)}" class="${pool===key?"active":""}" aria-pressed="${pool===key}">${esc(label)}<b>${n(count||0,0)}</b></button>`).join("");
    fillOptions("industry",payload.industries||[],"全部行业");fillOptions("theme",payload.themes||[],"全部方向");
    $("grCriteria").innerHTML=(payload.criteria||[]).map(line=>`<p>${esc(line)}</p>`).join("")+`<p>本期财报缓存：${esc(payload.financial_updated_at||"尚未取得")}；盈利预测缓存：${esc(payload.forecasts_updated_at||"尚未取得")}。板块比较采用全市场可比正EPS预测样本中位数，不等于板块总体利润增速。</p><p>来源：本地行情快照、<a href="https://data.eastmoney.com/bbsj/" target="_blank" rel="noopener">东方财富财报</a>、<a href="https://data.eastmoney.com/report/profitforecast.jshtml" target="_blank" rel="noopener">盈利预测</a>。概念标签还需核实主营收入。</p>`;
  }
  function stockTable() {
    const rows=payload.stocks||[],years=payload.forecast_years||[];
    $("grCount").textContent=`命中 ${n(payload.total_matched||0,0)} 家 · 默认排除风险警示名称可取消`;
    if (!rows.length) {
      $("grTable").innerHTML=`<div class="gr-empty">${payload.ready?"没有符合当前条件的股票。可切到全市场或放宽筛选。":"尚无研究数据。点击“重算本地数据”，或更新财报与预期。"}</div>`;$("grPager").innerHTML="";return;
    }
    const sortable=(label,key)=>`<button type="button" data-gr-sort="${key}">${esc(label)}${form.elements.sort.value===key?(direction==="desc"?" ↓":" ↑"):""}</button>`;
    $("grTable").innerHTML=`<table class="gr-table"><thead><tr><th>股票</th><th>行业</th><th>研究分组</th><th>${sortable("收盘(元)","price")}</th><th>${sortable("营收同比","revenue_growth")}</th><th>${sortable("归母同比","profit_growth")}</th><th>${sortable(`${years[0]||"未来"}—${years[2]||"两年"}E CAGR`,"cagr")}</th><th>${sortable(`${years[0]||"预期"}E PE`,"pe")}</th><th>价格位置</th><th>参考支撑</th><th>${sortable("距支撑","support_distance")}</th><th>技术依据</th><th>风险/数据缺口</th></tr></thead><tbody>`+rows.map(r=>`<tr><td><button class="gr-stock-name" type="button" data-gr-stock="${esc(r.code)}">${esc(r.name)}</button><small>${esc(r.code)}</small></td><td class="gr-left">${esc(r.industry)}</td><td class="gr-left">${esc(r.group_label)}</td><td>${n(r.price)}</td><td>${pct(r.revenue_growth)}</td><td>${pct(r.profit_growth)}</td><td>${pct(r.cagr)}</td><td>${n(r.pe,1)}</td><td class="gr-left">${esc(r.position_status)}</td><td>${n(r.support)}</td><td>${pct(r.support_distance)}</td><td><span class="gr-tag ${r.technical_basis==="qfq"?"verified":""}">${r.technical_basis==="qfq"?"前复权已校验":"收盘初筛"}</span></td><td class="gr-risk gr-left">${esc(r.risks.join("；")||"列示风险未触发，仍需核公告")}</td></tr>`).join("")+"</tbody></table>";
    $("grPager").innerHTML=`<button class="button secondary compact" type="button" data-gr-page="${payload.page-1}" ${payload.page<=1?"disabled":""}>上一页</button><span>第 ${payload.page} / ${payload.pages} 页 · 每页 ${payload.page_size} 家</span><button class="button secondary compact" type="button" data-gr-page="${payload.page+1}" ${payload.page>=payload.pages?"disabled":""}>下一页</button>`;
  }
  function sectorTable() {
    const search=form.elements.search.value.trim().toLowerCase(),industry=form.elements.industry.value;
    const rows=(payload.sectors||[]).filter(r=>(!industry||r.industry===industry)&&(!search||[r.industry,...r.examples.map(x=>x.name+" "+x.code)].join(" ").toLowerCase().includes(search)));
    $("grCount").textContent=`${rows.length} 个行业 · 全市场可比样本中位数`;
    $("grPager").innerHTML="";
    $("grTable").innerHTML=rows.length?`<table class="gr-table"><thead><tr><th>行业 / 查看成分</th><th>行情样本</th><th>可比预测样本</th><th>实际高增长</th><th>未来高增长</th><th>下一年EPS增速中位数</th><th>未来两年CAGR中位数</th><th>预期PE中位数</th><th>低位成长候选</th><th>高预期公司</th></tr></thead><tbody>`+rows.map(r=>`<tr><td><button type="button" data-gr-industry="${esc(r.industry)}">${esc(r.industry)}</button></td><td>${r.stocks}</td><td>${r.forecast_covered}<small>覆盖 ${pct(r.forecast_covered/r.stocks)}</small></td><td>${r.actual_high}</td><td>${r.future_high}</td><td>${pct(r.growth_next)}</td><td>${pct(r.cagr)}</td><td>${n(r.pe,1)}</td><td>${r.near_growth}</td><td class="gr-risk gr-left">${esc(r.examples.map(x=>x.name).join("、")||"—")}</td></tr>`).join("")+"</tbody></table>":'<p class="gr-empty">暂无匹配行业。</p>';
  }
  function render() {
    renderMeta();tab==="stocks"?stockTable():sectorTable();
    const sectors=tab==="sectors";
    for(const el of form.elements)if(el.name)el.disabled=sectors&&!["search","industry"].includes(el.name);
    root.querySelectorAll("[data-gr-pool]").forEach(b=>b.disabled=sectors);
    $("grExport").textContent=sectors?"导出板块统计":"导出筛选结果";
    form.elements.search.placeholder=sectors?"搜索行业或表中列示公司":"代码、名称、方向、风险关键词";
    root.querySelectorAll("[data-gr-tab]").forEach(button=>button.setAttribute("aria-selected",String(button.dataset.grTab===tab)));
    if (selected) detail(selected);
  }
  async function load() {
    controller?.abort();controller=new AbortController();const current=controller;
    root.setAttribute("aria-busy","true");
    try {
      const q=params();const next=await request("/api/growth-research?"+q,{signal:current.signal});
      if (current!==controller) return;
      if(selected)selected=next.stocks.find(r=>r.code===selected.code)||null;
      if(!selected)$("grDetail").hidden=true;
      payload=next;page=next.page;error();render();
      history.replaceState(null,"",location.pathname+"?"+q.toString());
      if (!payload.ready&&!bootstrapped) {bootstrapped=true;await refresh("rebuild");}
    } catch (e) {if (e.name!=="AbortError") error(e.message);}
    finally {if(current===controller) root.removeAttribute("aria-busy");}
  }
  function detail(row) {
    selected=row;const years=payload.forecast_years||[];const host=$("grDetail");host.hidden=false;
    host.innerHTML=`<div class="gr-detail-head"><h3>${esc(row.name)} <span class="muted">${esc(row.code)}</span></h3><div class="actions"><button class="button secondary compact" type="button" data-gr-action="kline">查看K线</button><button class="button secondary compact" type="button" data-gr-action="watch">加自选</button><button class="button compact" type="button" data-gr-action="analysis">个股分析</button><button class="button secondary compact" type="button" data-gr-action="close">收起</button></div></div>
      <div class="gr-detail-grid">${[["收盘价",n(row.price)],["报告期归母 / 亿元",n(row.profit_yi)],["参考支撑 / 元",n(row.support)],["模型失效参考 / 元",n(row.invalidation_reference)],...years.map((y,i)=>[`${y}E EPS / 元`,n(row[["eps_base","eps_next","eps_last"][i]],4)]),["近6月评级机构",n(row.forecast_orgs,0)]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("")}</div>
      <p>财报 ${esc(row.report_date||"缺失")}，公告 ${esc(row.notice_date||"缺失")}；去年同期归母 ${n(row.prior_profit_yi)} 亿元，扣非EPS ${n(row.deduct_eps,4)} 元，经营现金流/股 ${n(row.operating_cf_ps,4)} 元。</p>
      <p>方向依据：${esc(row.theme_evidence.join("；")||"未命中新质映射")}。${row.sustained_growth?"Q1与本期同时满足持续增长条件。":"未满足Q1与本期的持续增长条件，或数据不足。"}</p>
      <p>价格位置：${esc(row.position_status)}；120日区间位置 ${pct(row.position120)}，区间高点回撤 ${pct(row.drawdown120)}；${esc(row.support_type||"未命中重复支撑或MA60回踩")}。${row.technical_basis==="qfq"?"前复权K线截至":"收盘初筛截至"} ${esc(row.history_date||"无历史")}。</p>
      <p>风险与缺口：${esc(row.risks.join("；")||"上述风险项未触发，尚需核查公告、审计、订单和减持等信息")}。</p><p id="grDetailStatus" role="status"></p>`;
  }
  function showJob(job) {
    const busy=job&&["queued","running"].includes(job.status),host=$("grJob");
    root.querySelectorAll("[data-gr-refresh]").forEach(b=>b.disabled=!!busy);
    host.hidden=!job;if(!job)return;
    const label={rebuild:"重算本地数据",financials:"更新财报与预期",technical:"校验候选支撑"}[job.params?.mode]||"更新研究数据";
    const done=job.result?.partial?"部分更新完成":"已完成";
    const status={queued:"已排队",running:"进行中",finished:done,completed:done,failed:"未完成"}[job.status]||job.status;
    host.innerHTML=`<strong>${esc(label)} · ${esc(status)}</strong>　${esc(job.error||job.logs?.at(-1)||"")}${busy?`<progress max="100" ${job.progress>0?`value="${Number(job.progress)}"`:""}></progress>`:""}`;
  }
  async function poll() {
    clearTimeout(pollTimer);
    try {
      const value=await request("/api/growth-research/status");showJob(value.job);
      if(value.job&&["queued","running"].includes(value.job.status))pollTimer=setTimeout(poll,1800);
      else if(["finished","completed"].includes(value.job?.status))await load();
    } catch(e) {error(e.message);}
  }
  async function refresh(mode) {
    try {error();const value=await request("/api/growth-research/refresh",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode})});showJob(value.job);pollTimer=setTimeout(poll,700);}
    catch(e){error(e.message);root.querySelectorAll("[data-gr-refresh]").forEach(b=>b.disabled=false);}
  }
  form.addEventListener("submit",event=>{event.preventDefault();page=1;load();});
  form.addEventListener("input",event=>{if(event.target.tagName==="SELECT"||event.target.type==="checkbox")return;clearTimeout(timer);timer=setTimeout(()=>{page=1;load();},300);});
  form.addEventListener("change",event=>{if(event.target.tagName==="SELECT"||event.target.type==="checkbox"){page=1;load();}});
  form.addEventListener("reset",()=>{pool="near";page=1;direction="desc";selected=null;$("grDetail").hidden=true;setTimeout(load,0);});
  root.addEventListener("click",async event=>{
    const button=event.target.closest("button");if(!button||button.disabled)return;
    try {
      if(button.dataset.grRefresh)return refresh(button.dataset.grRefresh);
      if(button.dataset.grPool){pool=button.dataset.grPool;page=1;return load();}
      if(button.dataset.grTab){tab=button.dataset.grTab;if(payload)render();return;}
      if(button.dataset.grPage){page=Number(button.dataset.grPage);return load();}
      if(button.dataset.grSort){const key=button.dataset.grSort;direction=form.elements.sort.value===key&&direction==="desc"?"asc":"desc";if(!Array.from(form.elements.sort.options).some(o=>o.value===key))form.elements.sort.add(new Option("收盘价",key));form.elements.sort.value=key;page=1;return load();}
      if(button.dataset.grIndustry){form.elements.industry.value=button.dataset.grIndustry;pool="all";tab="stocks";page=1;return load();}
      if(button.dataset.grStock){const row=payload.stocks.find(r=>r.code===button.dataset.grStock);if(row){detail(row);$("grDetail").scrollIntoView({behavior:"smooth",block:"nearest"});}return;}
      if(button.dataset.grAction&&selected){
        const action=button.dataset.grAction;
        if(action==="close"){$("grDetail").hidden=true;selected=null;return;}
        if(action==="watch"){await request("/api/watchlist/add",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:selected.code,name:selected.name})});$("grDetailStatus").textContent="已加入自选";return;}
        if(action==="kline"&&typeof window.openStockKlineModal==="function")return await window.openStockKlineModal(selected.code,240,{stockName:selected.name});
        if(action==="analysis"&&typeof window.openStockContext==="function")return await window.openStockContext({type:"stock",stock_code:selected.code,stock_name:selected.name});
        throw new Error("个股面板尚未就绪，请刷新页面后重试");
      }
    }catch(e){error(e.message);}
  });
  $("grExport").addEventListener("click",async()=>{
    try{const q=params();q.set("view",tab);const response=await fetch("/api/growth-research/export?"+q);if(!response.ok)throw new Error("导出失败，请重试");const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=`成长研究_${tab==="sectors"?"板块_":""}${payload?.as_of||"快照"}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){error(e.message);}
  });
  window.addEventListener("beforeunload",()=>{controller?.abort();clearTimeout(pollTimer);clearTimeout(timer);});
  $("refreshBtn")?.addEventListener("click",()=>load());
  load();
  request("/api/growth-research/status").then(value=>{showJob(value.job);if(value.job&&["queued","running"].includes(value.job.status))pollTimer=setTimeout(poll,1500);}).catch(e=>error(e.message));
})();
