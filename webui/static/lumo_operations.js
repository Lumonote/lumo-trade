/* 图形化操作分析，嵌入现有K线、市场周期、风控、自选及板块位置。 */
(() => {
  "use strict";
  const esc = v => String(v ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const finite = v => v != null && Number.isFinite(Number(v));
  const fmt = (v, n=2) => finite(v) ? Number(v).toFixed(n) : "—";
  const pct = v => finite(v) ? `${Number(v)>0?"+":""}${fmt(v)}%` : "—";
  const colors = {advance:"rgba(196,61,54,.06)", decline:"rgba(25,135,84,.07)", base:"rgba(47,111,221,.05)", top:"rgba(168,109,0,.07)"};
  const sectorColors = {leading:"#c43d36", improving:"#2f6fdd", weakening:"#a86d00", lagging:"#198754", unknown:"#96a3b4"};
  const plans = new Map(), pending = new Map(), charts = new Map();
  let activeCode = "", pulseSignature = "", selectedSector = "", selectedState = "all";
  const sectorKey = r => `${r.sector_type || ""}:${r.sector}`;

  async function fetchPlan(code, force=false) {
    const cached = plans.get(code);
    if (!force && cached && Date.now()-cached.time < 60000) return cached.plan;
    if (pending.has(code)) return pending.get(code);
    const promise = fetchJson(`/api/trading-plans?code=${encodeURIComponent(code)}`).then(data => {
      const plan = data.items?.[0];
      if (!plan) throw new Error("没有返回该股票的计划");
      plans.set(code,{time:Date.now(),plan});
      return plan;
    }).finally(() => pending.delete(code));
    pending.set(code,promise);
    return promise;
  }

  // 小图/网络库不可用时仍能展示真实OHLC，绝不以涨跌幅合成假K线。
  function candleSvg(payload, {mini=false, compact=false, levels=null, limit=60}={}) {
    const rows = (payload.records || []).filter(r => [r.open,r.high,r.low,r.close].every(finite)).slice(-Math.min(limit,compact?45:limit));
    if (!rows.length) return '<div class="op-error">暂无有效K线</div>';
    const op = payload.operation || {}, l = levels || op.levels || {};
    const w=mini?300:compact?480:920, h=mini?120:370, left=mini?3:14, right=mini?8:compact?95:116, top=mini?6:25, bottom=mini?12:55;
    const prices = rows.flatMap(r => [Number(r.low),Number(r.high)]);
    if (!mini) [l.stop_loss,l.entry_low,l.entry_high,l.target1].filter(finite).forEach(v=>prices.push(Number(v)));
    let low=Math.min(...prices), high=Math.max(...prices), gap=(high-low)*.08 || high*.01;
    low-=gap; high+=gap;
    const y = v => top+(high-Number(v))/(high-low)*(h-top-bottom), dx=(w-left-right)/rows.length, x=i=>left+(i+.5)*dx;
    let body="";
    if (!mini && finite(l.entry_low) && finite(l.entry_high)) body+=`<rect x="${left}" y="${y(l.entry_high)}" width="${w-left-right}" height="${Math.max(1,y(l.entry_low)-y(l.entry_high))}" fill="rgba(47,111,221,.13)"/>`;
    if (!mini) for(let i=0;i<4;i++){ const value=low+(high-low)*i/3; body+=`<path d="M${left},${y(value)} H${w-right}" stroke="#eef2f6"/><text x="${w-right+8}" y="${y(value)+4}" fill="#96a3b4" font-size="11">${fmt(value)}</text>`; }
    rows.forEach((r,i)=>{
      const color=Number(r.close)>=Number(r.open)?"#c43d36":"#198754", yy=Math.min(y(r.open),y(r.close)), height=Math.max(1,Math.abs(y(r.open)-y(r.close)));
      body+=`<g><title>${esc(r.date)} 开${fmt(r.open)} 高${fmt(r.high)} 低${fmt(r.low)} 收${fmt(r.close)}</title><path d="M${x(i)},${y(r.high)} V${y(r.low)}" stroke="${color}"/><rect x="${x(i)-dx*.3}" y="${yy}" width="${Math.max(.7,dx*.6)}" height="${height}" fill="${color}"/></g>`;
    });
    if (!mini) {
      const dates=new Map(rows.map((r,i)=>[r.date,i]));
      const points=(op.fractals||[]).filter(p=>dates.has(p.date));
      if(points.length) body+=`<polyline points="${points.map(p=>`${x(dates.get(p.date))},${y(p.price)}`).join(" ")}" stroke="#6f55d8" stroke-width="1.5" opacity=".6" fill="none"/>`;
      [[l.entry_high,"触发区上沿","#2f6fdd"],[l.stop_loss,"结构失效","#198754"],[l.target1,"第一目标区","#a86d00"]].forEach(([price,label,color])=>{
        if(!finite(price))return;
        body+=`<path d="M${left},${y(price)} H${w-right}" stroke="${color}" stroke-dasharray="5 4"/><text x="${w-right+7}" y="${y(price)-5}" fill="${color}" font-size="12">${label}</text><text x="${w-right+7}" y="${y(price)+10}" fill="${color}" font-size="12">${fmt(price)}</text>`;
      });
      (op.events||[]).filter(e=>dates.has(e.date)).slice(-7).forEach(e=>{
        const xx=x(dates.get(e.date)), yy=y(e.price), color=e.kind==="risk"?"#198754":"#2f6fdd";
        body+=`<circle cx="${xx}" cy="${yy}" r="3" fill="${color}"/><text x="${xx}" y="${yy+(e.kind==="risk"?-10:18)}" text-anchor="middle" font-size="11" fill="${color}">${esc(e.label)}</text>`;
      });
      body+=`<text x="${left}" y="${h-12}" fill="#96a3b4" font-size="11">${esc(rows[0].date)}</text><text x="${w-right}" y="${h-12}" text-anchor="end" fill="#96a3b4" font-size="11">${esc(rows.at(-1).date)}</text>`;
    }
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(payload.name||payload.code||"行情")} ${mini?"近期K线":"K线、结构触发区与失效线"}">${body}</svg>`;
  }

  function drawChart(element, payload, options={}) {
    if (!element) return;
    for (const old of charts.keys()) if (!old.isConnected) { resizeObserver.unobserve(old); charts.delete(old); }
    const operation=payload.operation||{}, levels=options.levels||operation.levels||{}, all=payload.records||[], rows=all.slice(-(options.limit||120));
    charts.set(element,{payload,options});
    if(!rows.length){element.innerHTML='<div class="op-error">暂无K线数据</div>';return;}
    if(!window.Plotly || !element.getBoundingClientRect().width){element.innerHTML=candleSvg(payload,{levels,compact:element.getBoundingClientRect().width<650,limit:options.limit||120});return;}
    const traces=buildKlineTraces(rows,options.mode||"modal").filter(t=>!/^MA(5|10)$/.test(t.name));
    const layout=buildKlineLayout(payload,rows,{mode:options.mode||"modal"});
    layout.shapes=[]; layout.annotations=[]; layout.margin={l:20,r:116,t:45,b:30};
    layout.legend={orientation:"h",x:0,y:1.12,font:{size:10,color:"#6b778a"}};
    layout.modebar={orientation:"h",bgcolor:"rgba(255,255,255,.8)",color:"#6b778a"};
    layout.xaxis.rangeselector=undefined; layout.xaxis.rangeslider={visible:false};
    layout.height=options.height||380; layout.uirevision=`${payload.code||payload.name||"chart"}:${options.model||"default"}`;
    const first=rows[0].date, last=rows.at(-1).date, dates=new Set(rows.map(r=>r.date));
    const stages=(operation.stage_history||[]).filter(s=>dates.has(s.date));
    let block=null;
    for(const s of stages){
      if(!block||block.key!==s.key){if(block)layout.shapes.push({type:"rect",xref:"x",yref:"paper",x0:block.start,x1:block.end,y0:.28,y1:1,line:{width:0},fillcolor:colors[block.key]||"transparent",layer:"below"});block={key:s.key,start:s.date,end:s.date};}
      else block.end=s.date;
    }
    if(block)layout.shapes.push({type:"rect",xref:"x",yref:"paper",x0:block.start,x1:block.end,y0:.28,y1:1,line:{width:0},fillcolor:colors[block.key]||"transparent",layer:"below"});
    for(const z of operation.zones||[]) if(z.to>=first && z.from<=last) layout.shapes.push({type:"rect",xref:"x",yref:"y",x0:z.from<first?first:z.from,x1:z.to,y0:z.low,y1:z.high,line:{color:"#96a3b4",width:1,dash:"dot"},fillcolor:"rgba(150,163,180,.04)",layer:"below"});
    if(finite(levels.entry_low)&&finite(levels.entry_high))layout.shapes.push({type:"rect",xref:"paper",yref:"y",x0:.72,x1:1,y0:levels.entry_low,y1:levels.entry_high,line:{width:0},fillcolor:"rgba(47,111,221,.13)",layer:"below"});
    [[levels.entry_high,"触发区上沿","#2f6fdd"],[levels.stop_loss,options.locked?"锁定失效线":"结构失效线","#198754"],[levels.target1,"第一目标区","#a86d00"]].forEach(([v,label,color])=>{
      if(!finite(v))return;
      layout.shapes.push({type:"line",xref:"paper",yref:"y",x0:.58,x1:1,y0:v,y1:v,line:{color,width:1.5,dash:"dash"}});
      layout.annotations.push({xref:"paper",yref:"y",x:1.01,y:v,text:`${label}<br><b>${fmt(v)}</b>`,showarrow:false,xanchor:"left",font:{color,size:11},bgcolor:"rgba(255,255,255,.88)"});
    });
    const points=(operation.fractals||[]).filter(p=>dates.has(p.date));
    if(points.length)traces.push({type:"scatter",mode:"lines+markers",x:points.map(p=>p.date),y:points.map(p=>p.price),name:"已确认波段",line:{color:"#6f55d8",width:1.4},marker:{size:4},text:points.map(p=>`${p.kind==="high"?"波段高点":"波段低点"}<br>确认于 ${esc(p.confirmed_at)}`),hovertemplate:"%{x}<br>%{y:.2f}<br>%{text}<extra></extra>"});
    const events=(operation.events||[]).filter(e=>dates.has(e.date)).slice(-8);
    for(const kind of ["breakout","risk"]){const subset=events.filter(e=>e.kind===kind);if(subset.length)traces.push({type:"scatter",mode:"markers",x:subset.map(e=>e.date),y:subset.map(e=>e.price),name:kind==="risk"?"支撑破坏":"放量突破",marker:{size:10,symbol:kind==="risk"?"triangle-down":"triangle-up",color:kind==="risk"?"#198754":"#2f6fdd"},text:subset.map(e=>`${e.label} · ${e.confirmed_at} 收盘确认`),hovertemplate:"%{text}<extra></extra>"});}
    if(operation.provisional)layout.annotations.push({xref:"paper",yref:"paper",x:1,y:1.06,text:"当根K线未收盘 · 信号待确认",showarrow:false,xanchor:"right",font:{size:11,color:"#a86d00"}});
    const priceExtent=rows.flatMap(r=>[Number(r.low),Number(r.high)]).concat([levels.stop_loss,levels.entry_high,levels.target1].filter(finite).map(Number));
    const lo=Math.min(...priceExtent), hi=Math.max(...priceExtent), pad=(hi-lo)*.08||hi*.01;
    layout.yaxis.range=[lo-pad,hi+pad];
    Plotly.react(element,traces,layout,{responsive:true,displaylogo:false,scrollZoom:true,displayModeBar:options.mode==="modal"?"hover":false,modeBarButtonsToRemove:["lasso2d","select2d"]}).catch(()=>{element.innerHTML=candleSvg(payload,{levels,compact:element.getBoundingClientRect().width<650});});
  }

  const resizeObserver=new ResizeObserver(entries=>{for(const entry of entries){const data=charts.get(entry.target);if(data&&entry.contentRect.width>80&&window.Plotly&&entry.target.classList.contains("js-plotly-plot"))Plotly.Plots.resize(entry.target);}});
  function chartAt(id,payload,options={}){const e=document.getElementById(id);if(e){drawChart(e,payload,options);resizeObserver.observe(e);}}
  window.renderKlineChart=(id,payload,options={})=>chartAt(id,payload,{...options,height:options.mode==="preview"?320:480});

  function modelHtml(op, selected) {
    return `<div class="op-models">${(op.models||[]).map(m=>`<div class="op-model ${m.key===selected?"selected":""}"><div class="op-model-title"><b>${esc(m.label)}</b><small>${m.matched}/${m.total} 条件</small></div><div class="op-checks" aria-label="${m.matched}项通过">${m.checks.map(c=>`<i class="${c.passed===true?"pass":""}" title="${esc(c.label)}"></i>`).join("")}</div><div class="op-check-list">${m.checks.map(c=>`<span class="${c.passed===true?"pass":""}">${esc(c.label)}${c.passed==null?" · 数据待补":""}</span>`).join("")}</div></div>`).join("")}</div>`;
  }
  function analysisHtml(plan, prefix, risk=false) {
    const chart=plan.chart||{}, op=chart.operation||plan.candidate?.operation||{}, l=plan.levels||op.levels||{}, available=plan.available&&op.available;
    return `<section class="op-analysis"><div class="op-head"><div><h3>${risk?"条件操作计划":"趋势与波段"}</h3><small>${esc(op.stage?.label||"数据待补齐")} · ${esc(op.dow?.label||"")} · ${esc(op.as_of||plan.data_as_of||"—")}</small></div><span class="op-status" data-state="${esc(plan.status)}">${esc(plan.status_label||"结构观察")}${plan.locked?" · 已锁定":""}</span></div>
      ${available?`<div class="op-price-grid"><div><small>买入触发区</small><strong>${fmt(l.entry_low)}–${fmt(l.entry_high)}</strong></div><div><small>${plan.locked?"锁定结构失效线":"结构失效线"}</small><strong class="op-risk">${fmt(l.stop_loss)}</strong></div><div><small>第一目标区</small><strong>${fmt(l.target1)}</strong></div><div><small>首目标盈亏比</small><strong>${fmt(l.risk_reward)}R</strong></div></div>`:`<p class="op-error">${esc(plan.candidate?.reason||op.reason||plan.reason||"结构资料不足，保留行情供观察")}</p>`}
      <div id="${prefix}Chart" class="op-chart"></div><div class="op-legend"><span>蓝色：触发区</span><span class="op-risk">绿色：结构失效</span><span class="op-target">黄色：目标情景</span><span class="op-swing">紫色：确认波段</span></div>
      ${modelHtml(op,op.selected_model)}
      ${risk?`<div class="op-gates"><span class="${plan.candidate?.model?.state==="triggered"?"pass":""}">① 模型确认</span><span class="${plan.market?.entry_allowed?"pass":""}">② 大盘环境</span><span class="${plan.sector_plan?.entry_allowed?"pass":""}">③ 板块共振</span><span class="${plan.new_position_pct>0?"pass":""}">④ 可用风险预算</span></div><p class="op-note">单票目标上限 ${fmt(plan.max_position_pct)}% · 本次试仓额度 ${fmt(plan.trial_position_pct||0)}% · 加仓额度 ${fmt(plan.add_position_pct||0)}%（模拟账户净值）</p>`:""}
      <div class="op-footer"><span>${esc(op.stage?.basis||"")} · ${esc(op.fundamentals?.label||"成长资料待核实")}</span>${risk?`<button type="button" class="button secondary compact" data-op-lock="${esc(plan.code)}" ${plan.candidate?.available?"":"disabled"}>${plan.locked?"复核并重定计划":"锁定当前计划"}</button>`:`<button type="button" class="button secondary compact" data-op-risk>查看操作计划</button>`}</div>
      <details class="op-details"><summary>触发依据与待确认条件</summary><p>${esc(plan.model?.theory||op.theory_note||"")}</p>${(plan.blockers||[]).map(t=>`<p>${esc(t)}</p>`).join("")}<p>${esc(l.target_basis||"")}。锁定只保存价位，不产生委托。</p></details></section>`;
  }

  function mountStock(prefix, risk=false) {
    const host=document.getElementById(risk?"suitePaneRiskControl":"suitePaneMarketCycle");
    if(!host||!activeCode)return;
    let mount=host.querySelector(`[data-op-stock="${prefix}"]`);
    if(!mount){mount=document.createElement("div");mount.dataset.opStock=prefix;host.prepend(mount);}
    const code=activeCode;
    if(mount.dataset.code===code&&mount.dataset.loaded==="1")return;
    mount.dataset.code=code;mount.innerHTML='<div class="op-loading">正在计算价格结构与操作条件…</div>';
    fetchPlan(code).then(plan=>{
      if(code!==activeCode||!mount.isConnected)return;
      mount.innerHTML=analysisHtml(plan,prefix,risk);mount.dataset.loaded="1";
      chartAt(`${prefix}Chart`,plan.chart||{}, {levels:plan.levels,locked:plan.locked,height:400});
      mount.querySelector("[data-op-risk]")?.addEventListener("click",()=>document.querySelector('[data-suite-tab="risk_control"]')?.click());
      mount.querySelector("[data-op-lock]")?.addEventListener("click",async event=>{
        const button=event.currentTarget;button.disabled=true;
        try{await fetchJson("/api/trading-plans/lock",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code,expected_revision:plan.revision,basis_key:plan.candidate.basis_key})});await fetchPlan(code,true);mount.dataset.loaded="0";mountStock(prefix,risk);}
        catch(error){button.disabled=false;let message=mount.querySelector(".op-save-error");if(!message){message=document.createElement("p");message.className="op-error op-save-error";mount.append(message);}message.textContent=error.message;}
      });
    }).catch(error=>{if(code===activeCode&&mount.isConnected)mount.innerHTML=`<div class="op-error">计划读取失败：${esc(error.message)} <button type="button" class="button secondary compact" data-op-retry>重试</button></div>`;mount.querySelector("[data-op-retry]")?.addEventListener("click",()=>mountStock(prefix,risk));});
  }

  const previousDimension=window.renderSuiteDimensionPane;
  window.renderSuiteDimensionPane=function(payload,dimension){if(dimension!=="market_cycle")return previousDimension(payload,dimension);const host=document.getElementById("suitePaneMarketCycle");if(host){host.innerHTML="";host.dataset.rendered="1";mountStock("opCycle");}};
  const previousRisk=window.renderSuiteRiskControl;
  window.renderSuiteRiskControl=function(payload){previousRisk(payload);document.querySelector("#suitePaneRiskControl .suite-risk-col")?.remove();mountStock("opRisk",true);};
  const previousOpen=window.openStockContext;
  window.openStockContext=function(context){const response=previousOpen(context);activeCode=state.currentStockCode||"";mountStock("opCycle");mountStock("opRisk",true);document.querySelector('[data-suite-tab="market_cycle"]')?.click();return response;};
  document.getElementById("stockSuiteTabs")?.addEventListener("click",event=>{
    const tab=event.target.closest("[data-suite-tab]")?.dataset.suiteTab;
    activeCode=state.currentStockCode||activeCode;
    if(tab==="market_cycle")mountStock("opCycle");if(tab==="risk_control")mountStock("opRisk",true);
    requestAnimationFrame(()=>{for(const [element,data] of charts)if(element.isConnected&&element.getBoundingClientRect().width>80)drawChart(element,data.payload,data.options);});
  });

  let queue=[], workers=0;
  const observedRows=new Set();
  function inlinePlan(row,plan) {
    if(!row.isConnected)return;
    const op=plan.chart?.operation||{}, l=plan.levels||{}, code=plan.code;
    let root=row.querySelector(".op-mini");if(!root){root=document.createElement("div");root.className="op-mini";row.append(root);}
    root.innerHTML=`<div class="op-inline-chart" tabindex="0" role="button" aria-label="查看${esc(plan.name||code)}结构K线">${candleSvg(plan.chart||{},{mini:true,limit:35})}</div><div class="op-mini-copy"><strong>${esc(op.stage?.label||"数据不足")} · ${esc(plan.model?.label||"结构观察")}</strong><span>${esc(plan.status_label)}${finite(l.entry_high)?` · 触发 ${fmt(l.entry_low)}–${fmt(l.entry_high)} · 失效 ${fmt(l.stop_loss)}`:""}</span><small>${esc(op.as_of||plan.data_as_of||"—")} · ${esc(op.provisional?"盘中待确认":plan.candidate?.reason||op.reason||"点击K线查看结构")}</small></div>`;
    const target=root.querySelector(".op-inline-chart");
    const open=event=>{event.stopPropagation();openStockKlineModal(code,240,{stockName:plan.name}).catch(()=>{});};
    target?.addEventListener("click",open);target?.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();open(event);}});
  }
  async function workQueue(){while(workers<2&&queue.length){const {row,code}=queue.shift();if(!row.isConnected)continue;workers++;fetchPlan(code).then(plan=>inlinePlan(row,plan)).catch(error=>{if(row.isConnected){const root=row.querySelector(".op-mini");if(root)root.textContent=`结构读取失败：${error.message}`;}}).finally(()=>{workers--;workQueue();});}}
  const rowObserver=new IntersectionObserver(entries=>{for(const entry of entries){if(!entry.isIntersecting)continue;rowObserver.unobserve(entry.target);observedRows.delete(entry.target);queue.push({row:entry.target,code:entry.target.dataset.wlCode||entry.target.dataset.stockCode});}workQueue();},{rootMargin:"100px"});
  function hydrateRows(root){for(const old of observedRows)if(!old.isConnected){rowObserver.unobserve(old);observedRows.delete(old);}root?.querySelectorAll("[data-wl-code], [data-stock-code]").forEach(row=>{if(row.dataset.opObserved)return;row.dataset.opObserved="1";const mount=document.createElement("div");mount.className="op-mini op-meta";mount.textContent="价格结构计算中…";row.append(mount);rowObserver.observe(row);observedRows.add(row);});}
  const previousWatchlist=window.renderWatchlistPage;
  window.renderWatchlistPage=function(...args){const result=previousWatchlist(...args);hydrateRows(document.getElementById("watchlistTable"));return result;};
  const previousOpportunities=window.renderOpportunities;
  window.renderOpportunities=function(...args){const result=previousOpportunities(...args);hydrateRows(document.getElementById("opportunityList"));return result;};
  // 原脚本部分刷新回调已捕获旧渲染器，观察实际列表更新也能保持原位挂载。
  for (const id of ["watchlistTable", "opportunityList"]) {
    const host=document.getElementById(id);
    if(host)new MutationObserver(()=>hydrateRows(host)).observe(host,{childList:true,subtree:true});
  }

  function rotationSvg(rows) {
    const valid=rows.map((r,index)=>({...r,index})).filter(r=>finite(r.relative20)&&finite(r.momentum)&&!r.stale);
    const sx=Math.max(1,...valid.map(r=>Math.abs(r.relative20))), sy=Math.max(1,...valid.map(r=>Math.abs(r.momentum)));
    const x=v=>235+Number(v)/sx*180, y=v=>160-Number(v)/sy*110;
    const labeled=new Set(), positions=[];
    for(const r of [...valid.filter(r=>sectorKey(r)===selectedSector),...valid.filter(r=>r.eligible)]){
      if(labeled.size>=5)break;
      const xx=x(r.relative20), yy=y(r.momentum);
      if(positions.some(p=>Math.abs(p.x-xx)<65&&Math.abs(p.y-yy)<20))continue;
      labeled.add(sectorKey(r));positions.push({x:xx,y:yy});
    }
    return `<svg viewBox="0 0 470 330" class="op-rotation" role="img" aria-label="板块相对强度与动量四象限"><rect x="40" y="35" width="195" height="125" fill="#edf3ff"/><rect x="235" y="35" width="195" height="125" fill="#fff0ed"/><rect x="40" y="160" width="195" height="125" fill="#edf6f0"/><rect x="235" y="160" width="195" height="125" fill="#fff5e5"/><path d="M40 160H430 M235 35V285" stroke="#c6d0dd"/><g fill="#6b778a" font-size="12"><text x="50" y="54">改善</text><text x="380" y="54">领涨</text><text x="50" y="277">落后</text><text x="380" y="277">转弱</text><text x="155" y="316">20日相对强度 →</text><text x="9" y="173" transform="rotate(-90 9 173)">相对动量 →</text></g>${valid.map(r=>`<g data-sector-index="${r.index}" tabindex="0" role="button" aria-label="${esc(r.sector)} ${esc(r.sector_type)} ${esc(r.state_label)}"><title>${esc(r.sector)}（${esc(r.sector_type)}）：相对强度 ${pct(r.relative20)}，动量 ${pct(r.momentum)}；${esc(r.data_status)}</title><circle cx="${x(r.relative20)}" cy="${y(r.momentum)}" r="${sectorKey(r)===selectedSector?7:4}" fill="${sectorColors[r.state]}" opacity=".8"/>${labeled.has(sectorKey(r))||sectorKey(r)===selectedSector?`<text x="${x(r.relative20)>345?x(r.relative20)-7:x(r.relative20)+7}" y="${y(r.momentum)-6}" text-anchor="${x(r.relative20)>345?"end":"start"}" fill="${sectorColors[r.state]}" font-size="10">${esc(r.sector)}</text>`:""}</g>`).join("")}</svg>`;
  }
  function curveSvg(row) {
    const values=(row.curve||[]).slice(-40), valid=values.filter(v=>finite(v.nav));
    if(valid.length<2)return '<div class="op-error">有效走势历史不足</div>';
    const low=Math.min(...valid.map(v=>v.nav)), high=Math.max(...valid.map(v=>v.nav)), x=i=>12+i/(values.length-1)*640,y=v=>20+(high-v)/(high-low||1)*84;
    let path="",gap=true;values.forEach((r,i)=>{if(!finite(r.nav)){gap=true;return;}path+=`${gap?"M":"L"}${x(i)},${y(r.nav)} `;gap=false;});
    return `<svg viewBox="0 0 680 130" role="img" aria-label="${esc(row.sector)}成分股聚合收益走势"><path d="M12 107H650" stroke="#dbe3ee"/><path d="${path}" fill="none" stroke="${sectorColors[row.state]}" stroke-width="2"/><text x="12" y="126" font-size="11" fill="#96a3b4">${esc(values[0].date)}</text><text x="650" y="126" text-anchor="end" font-size="11" fill="#96a3b4">${esc(values.at(-1).date)}</text></svg>`;
  }
  function sectorDetail(host,row) {
    if(!row)return;
    host.innerHTML=`<div class="op-head"><b>${esc(row.sector)}（${esc(row.sector_type)}） · ${esc(row.state_label)}</b><span class="op-meta">${esc(row.data_status)} · ${esc(row.trade_date)}</span></div>${curveSvg(row)}<p class="op-note">20日相对 ${pct(row.relative20)} · 上涨广度 ${finite(row.breadth)?fmt(row.breadth*100,0)+"%":"—"} · 5日净流 ${fmt(row.flow5,0)} 万元 · 连续流入 ${row.inflow_streak} 日</p><p class="op-note">${esc(row.next_condition)}。${esc(row.basis)}</p>`;
  }
  function renderSectors(host,data) {
    const rows=data.sectors?.landscape||[], coverage=data.sectors?.coverage||{};
    const statuses={no_data:"尚未形成板块日序列，请在资金数据入库后刷新",data_error:"板块数据读取失败，请稍后重试",validation_error:"拐点校验结果读取失败，趋势状态仍可观察",unvalidated:"拐点规则未通过校验，趋势状态仍可观察",no_trigger:"今日无已验证新拐点，持续趋势见下方",active:"已验证拐点与持续趋势分别展示"};
    const filtered=rows.filter(r=>selectedState==="all"||r.state===selectedState);
    host.innerHTML=`<div class="op-sector-toolbar">${[["all","全部"],["leading","领涨"],["improving","改善"],["weakening","转弱"],["lagging","落后"]].map(([key,label])=>`<button type="button" data-op-state="${key}" class="${selectedState===key?"active":""}">${label}${key==="all"?` ${rows.length}`:` ${rows.filter(r=>r.state===key).length}`}</button>`).join("")}<span class="op-meta">共振观察 ${coverage.eligible||0} · 滞后 ${coverage.stale||0}</span></div>
      ${rows.length?`<div class="op-sector-layout"><div>${rotationSvg(rows)}<p class="op-note">相对强度 × 动量 · 点击板块查看走势。相对领涨不等同于绝对上涨。</p></div><div class="op-sector-table"><table><thead><tr><th>板块</th><th>状态</th><th>相对20日</th><th>上涨广度</th></tr></thead><tbody>${filtered.map(r=>`<tr><td><button type="button" data-op-sector="${esc(sectorKey(r))}">${esc(r.sector)}${r.stale?" · 滞后":""}<small class="op-meta"> ${esc(r.sector_type)}</small></button></td><td>${esc(r.state_label)}</td><td class="${r.relative20>=0?"op-up":"op-down"}">${pct(r.relative20)}</td><td>${finite(r.breadth)?fmt(r.breadth*100,0)+"%":"—"}</td></tr>`).join("")||'<tr><td colspan="4">该状态暂无板块，可切换全部</td></tr>'}</tbody></table></div></div><div class="op-sector-detail"></div>`:""}
      <div class="op-events">${[...(data.sectors?.fired||[]),...(data.sectors?.watch||[])].map(r=>`<span class="op-event">${esc(r.sector)} · ${r.provisional?"盘中待确认":(data.sectors.fired||[]).includes(r)?"拐点触发":"临界观察"}</span>`).join("")}</div><p class="op-note">${esc(statuses[data.sectors?.status]||"等待板块状态")} · ${esc(data.as_of?.sector||"日期未知")}</p>`;
    host.querySelectorAll("[data-op-state]").forEach(b=>b.addEventListener("click",()=>{selectedState=b.dataset.opState;renderSectors(host,data);}));
    const choose=row=>{selectedSector=sectorKey(row);sectorDetail(host.querySelector(".op-sector-detail"),row);host.querySelector(".op-rotation").outerHTML=rotationSvg(rows);bindRotation();};
    function bindRotation(){host.querySelectorAll("[data-sector-index]").forEach(g=>{const action=()=>choose(rows[Number(g.dataset.sectorIndex)]);g.addEventListener("click",action);g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();action();}});});}
    bindRotation();host.querySelectorAll("[data-op-sector]").forEach(b=>b.addEventListener("click",()=>choose(rows.find(r=>sectorKey(r)===b.dataset.opSector))));
    sectorDetail(host.querySelector(".op-sector-detail"),filtered.find(r=>sectorKey(r)===selectedSector)||filtered[0]||rows[0]);
  }
  function renderMarketPulse(host,data) {
    if(!host)return;
    const signature=JSON.stringify([data.as_of,data.indices?.map(i=>i.close),data.sectors]);
    if(signature===pulseSignature&&host.querySelector("[data-op-market]"))return;
    pulseSignature=signature;
    host.innerHTML=`<section class="panel" data-op-market><div class="panel-header"><h2 class="panel-title">指数风向</h2><span class="op-meta">${esc(data.as_of?.index||"数据待补齐")}</span></div><div class="panel-body"><div class="op-index-grid">${(data.indices||[]).map((r,i)=>`<div class="mp-index-card"><div class="mp-name">${esc(r.name)}</div><div class="op-index-number">${fmt(r.close)} <small class="op-meta">5日 ${pct(r.chg_5d)}</small></div><div class="op-index-stage"><span>${esc(r.operation?.stage?.label||r.pulse_label)}</span><span>${esc(r.operation?.dow?.label||"")}</span></div><button type="button" class="op-index-candle" data-op-index="${i}" aria-label="展开${esc(r.name)}趋势K线">${candleSvg(r,{mini:true,limit:40})}</button><div class="op-meta">${esc(r.operation?.provisional?"当日K线待收盘":"收盘结构")} · 点击K线展开</div></div>`).join("")||'<div class="op-error">指数行情暂不可用</div>'}</div><p class="op-note">${esc(data.style?.label||"")} · ${esc(data.position_advice||"")}</p><div data-op-index-detail hidden></div></div></section><section class="panel"><div class="panel-header"><h2 class="panel-title">板块机会与拐点</h2><span class="op-meta">相对强度 · 广度 · 资金持续性</span></div><div class="panel-body" id="opSectorBody"></div></section>`;
    host.querySelectorAll("[data-op-index]").forEach(button=>button.addEventListener("click",()=>{
      const index=data.indices[Number(button.dataset.opIndex)], detail=host.querySelector("[data-op-index-detail]");
      detail.hidden=false;
      detail.innerHTML=`<div class="op-head"><b>${esc(index.name)} · ${esc(index.operation?.stage?.label||index.pulse_label)}</b><button class="button secondary compact" type="button" data-op-close-index>收起</button></div><div id="opIndexChart" class="op-chart"></div><p class="op-note">${esc(index.as_of)} · ${esc(index.operation?.stage?.basis||"")} · 指数结构用于环境观察；价位为结构条件与风险情景。</p>`;
      chartAt("opIndexChart",index,{height:380});
      detail.querySelector("[data-op-close-index]").addEventListener("click",()=>{detail.hidden=true;});
    }));
    renderSectors(host.querySelector("#opSectorBody"),data);
  }
  window.LumoOperations={renderMarketPulse};
  // 私有市场面板首次响应可能先于本脚本加载，补一次同源缓存读取。
  if(document.getElementById("marketPulseHost"))fetchJson("/api/market-pulse").then(data=>renderMarketPulse(document.getElementById("marketPulseHost"),data)).catch(()=>{});
  hydrateRows(document.getElementById("watchlistTable"));hydrateRows(document.getElementById("opportunityList"));
})();
