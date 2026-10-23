/* 板块研究独立使用自己的披露日，绝不随大盘视图切换而改写日期。 */
(() => {
  "use strict";
  const root=document.getElementById("mhView-sectors");
  if (!root) return;
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num=(v,d=1)=>v==null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
  const pct=v=>v==null ? "—" : `${num(v)}%`;
  const money=v=>v==null ? "—" : `${v>0?"+":""}${num(v/1e8,2)}亿`;
  const kpi=(label,value,note)=>`<div class="mh-kpi"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const tone=v=>v>0?"mh-positive":v<0?"mh-negative":"mh-neutral";
  const q=new URLSearchParams(location.search);
  let data=null,selected=q.get("sector")||"",details=null,requestId=0,poll=null,started=false,auto=false;
  if (["行业","概念"].includes(q.get("sectorType"))) $("sdType").value=q.get("sectorType");
  async function api(url,opts={}) {
    const response=await fetch(url,{...opts,headers:{"Content-Type":"application/json"}});
    const value=await response.json();
    if (!response.ok) throw new Error(value.error||"板块接口读取失败");
    return value;
  }
  function error(message="") { $("sdError").hidden=!message;$("sdError").textContent=message; }
  function remember() {
    const url=new URL(location.href);url.searchParams.set("sector",selected);url.searchParams.set("sectorType",$("sdType").value);history.replaceState(null,"",url);
  }
  function group() {return (data?.rows||[]).filter(r=>r.type===$("sdType").value);}
  function filtered() {
    const keyword=$("sdSearch").value.trim(),key=$("sdSort").value;
    return group().filter(r=>(!$("sdPositive").checked||r.net>0)&&`${r.name} ${r.code}`.includes(keyword)).sort((a,b)=>(b[key]??-Infinity)-(a[key]??-Infinity));
  }
  function renderRank() {
    const rows=filtered(),all=group(),positive=all.filter(r=>r.net>0),leader=[...positive].sort((a,b)=>b.net-a.net)[0];
    const sustained=[...positive].filter(r=>r.positive5>=4&&r.flow5>0&&r.excess5>0).sort((a,b)=>(b.score??-1)-(a.score??-1))[0];
    const live=(data.live?.rows||[]).filter(r=>r.type===$("sdType").value).sort((a,b)=>(b.net??-Infinity)-(a.net??-Infinity)).slice(0,20);
    $("sdLive").innerHTML=live.length?`<article class="mh-box"><div class="mh-box-head"><div><h3>最新资金观测 · ${esc(data.live.source)}</h3><p>按源报价时点展示；下方持续性排名独立采用已披露收盘日。</p></div></div><div class="mh-table-wrap"><table><thead><tr><th>板块</th><th>最新净额</th><th>净占比</th><th>实际报价时间 / 类型</th></tr></thead><tbody>${live.map(r=>`<tr><td>${esc(r.name)}</td><td>${money(r.net)}</td><td>${pct(r.rate)}</td><td>${esc(r.quote_at)} · ${esc(r.timing)}</td></tr>`).join("")}</tbody></table></div></article>`:"";
    $("sdSummary").innerHTML=`<div class="mh-kpis">${kpi("当日净流入最强",leader?.name||"无正流入板块",leader?`${money(leader.net)} · ${data.as_of}`:"按真实净额筛选")}${kpi("持续共振候选",sustained?.name||"本轮无符合项",sustained?`五日${sustained.positive5}/5日流入 · 跑赢沪深300 ${pct(sustained.excess5)}`:"五日≥4日流入、五日净额及相对收益均为正")}${kpi("正流入板块",`${positive.length} / ${all.length}`,`${$("sdType").value}内部比较 · 成分可能重叠`)}${kpi("历史完整度",`${data.calendar?.length||0}个交易日`,`${data.calendar?.[0]||"未取得"} 至 ${data.as_of||"未取得"}`)}</div>`;
    $("sdRank").innerHTML=`<div class="mh-box-head"><div><h3>资金去向 · ${esc($("sdType").value)}</h3><p>${esc(data.as_of||"未取得")} 已披露收盘资金 · 点击板块穿透成分与持续性 · 当前${rows.length}项</p></div></div><div class="mh-table-wrap sd-rank-wrap"><table><thead><tr><th>板块 / 阶段</th><th>净额 / 排名</th><th>净占比</th><th>五日净额</th><th>二十日净额</th><th>五日正流入</th><th>连续流入</th><th>五日跑赢沪深300</th><th>持续性刻度</th><th>承接强度</th></tr></thead><tbody>${rows.map(r=>`<tr class="${r.code===selected?"sd-selected":""}"><td><button type="button" class="mh-inline-link" data-sd-code="${esc(r.code)}">${esc(r.name)}</button><small>${esc(r.code)} · ${esc(r.phase)}</small></td><td class="${tone(r.net)}">${money(r.net)}<small>第${r.rank}/${r.universe} · 五日名次变动 ${r.rank_change5==null?"—":`${r.rank_change5>0?"+":""}${r.rank_change5}`}</small></td><td>${pct(r.rate)}</td><td class="${tone(r.flow5)}">${money(r.flow5)}</td><td class="${tone(r.flow20)}">${money(r.flow20)}</td><td>${r.positive5==null?"窗口缺日":`${r.positive5}/5日`}</td><td>${r.streak}日</td><td class="${tone(r.excess5)}">${pct(r.excess5)}</td><td>${num(r.persistence,0)}/100</td><td>${num(r.score,0)}/100</td></tr>`).join("")||'<tr><td colspan="10">当前筛选没有匹配项，可取消“仅当日净流入”查看流出与退潮板块。</td></tr>'}</tbody></table></div>`;
    $("sdRank").insertAdjacentHTML("beforeend",'<p class="mh-note">短历史或窗口缺日显示“—”，不补零、不计算该窗口。持续性刻度使用完整五日证据；二十日净额单独要求完整二十日，不因五日有效而补齐。</p>');
    $("sdRotation").innerHTML=`<div class="mh-box-head"><div><h3>十日资金轮动路径</h3><p>同一交易日去重，列出该类型当日正流入前五；同名反复出现可核对驻留。</p></div></div><div class="mh-table-wrap"><table><thead><tr><th>已披露交易日</th><th>正流入前五</th></tr></thead><tbody>${(data.rotation||[]).filter(r=>r.type===$("sdType").value).reverse().map(r=>`<tr><td>${esc(r.date)}</td><td>${r.leaders.map(v=>`<button class="sd-chip" type="button" data-sd-code="${esc(v.code)}">${esc(v.name)} ${money(v.net)}</button>`).join("")||"当日无正流入板块"}</td></tr>`).join("")}</tbody></table></div>`;
  }
  function plot(rows,key,label,scale=1,invert=false) {
    const valid=rows.filter(r=>r[key]!=null);if(valid.length<2)return '<p class="mh-empty">该窗口不足两期有效观测</p>';
    const values=valid.map(r=>r[key]/scale*(invert?-1:1)),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1;
    const x=i=>55+i*890/(valid.length-1),y=v=>20+(hi-v)*170/span;
    return `<svg class="mh-chart" viewBox="0 0 1000 240" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>${[0,.5,1].map(p=>{const v=lo+span*p;return `<line class="mh-chart-grid" x1="55" x2="945" y1="${y(v)}" y2="${y(v)}"/><text x="48" y="${y(v)+4}" text-anchor="end">${num(v*(invert?-1:1),invert?0:1)}</text>`;}).join("")}<path class="mh-chart-line" d="${values.map((v,i)=>`${i?"L":"M"}${x(i)},${y(v)}`).join(" ")}"/><text x="55" y="225">${esc(valid[0].date)}</text><text x="945" y="225" text-anchor="end">${esc(valid.at(-1).date)}</text></svg>`;
  }
  function renderDetail(value) {
    const r=value.sector;let cumulative=0;
    const history=(r.history||[]).map(v=>({...v,cumulative:cumulative+=v.net}));
    const risk=[...r.risks,...value.risks];
    const t=value.technical||{};
    $("sdDetail").innerHTML=`<article class="mh-box mh-space"><div class="mh-box-head"><div><h3>${esc(r.name)} · ${esc(r.phase)}</h3><p>${esc(r.code)} / ${esc(r.type)} · 板块资金 ${esc(r.date)} · 成分 ${esc(value.member_as_of)} · 个股资金 ${esc(value.as_of)}</p></div><button class="button secondary" id="sdDetailRefresh" type="button">重读成分穿透</button></div>
      ${value.error?`<p class="mh-notice">${esc(value.error)}</p>`:""}<div class="mh-kpis">${kpi("当日资金",money(r.net),`同类型第${r.rank}/${r.universe} · 净占比${pct(r.rate)}`)}${kpi("五日 / 十日净额",money(r.flow5),`十日 ${money(r.flow10)} · 20日 ${money(r.flow20)}`)}${kpi("五日资金留存",pct(r.retention5),"五日净额 / 五日绝对净额之和；负值归零")}${kpi("承接加速 / 减速",money(r.acceleration),"最近五日净额减此前五日净额")}</div>
      <p class="mh-note">${r.positive5==null?"五日窗口缺日，暂不能判断完整持续性。":`五日正流入 ${r.positive5}/5 日，连续 ${r.streak} 个交易日流入，五日中 ${r.top20_days5}/5 日位于同类资金前20%。`} 五日复合涨幅 ${pct(r.ret5)}，相对沪深300 ${pct(r.excess5)}；二十日相对收益 ${pct(r.excess20)}。</p>
      <div class="mh-grid"><div><h3>观测区间累计净额 · 亿元</h3>${plot(history,"cumulative","板块历史累计净额",1e8)}</div><div><h3>资金排名路径 · 越靠上越强</h3>${plot(history,"rank","同类型板块资金排名",1,true)}</div></div>
      <div class="mh-kpis">${kpi("超大单净额",money(r.elg),`大单 ${money(r.lg)}`)}${kpi("资金异常强度",num(r.flow_z,2),"相对20日历史中位数/MAD的稳健刻度")}${kpi("板块日成交额",money(t.amount),`${t.as_of||"接口未返回"} · dc_daily`)}${kpi("当日 / 前五日均量",`${num(t.volume5,2)}倍`,`换手率 ${pct(t.turnover)} · ${t.as_of||"接口未返回"}`)}</div>
      <p class="mh-note">${esc(data.disclosure||"")}。资金净额不等于机构持仓增减，持续共振只描述已发生的资金和价格配合。</p></article>
      <article class="mh-box mh-space"><div class="mh-box-head"><div><h3>成分扩散与龙头集中度</h3><p>${esc(value.source)} · 覆盖${value.covered}/${value.members}只 · 实际资金日${esc(value.as_of)}</p></div></div><div class="mh-kpis">${kpi("成分净流入比例",pct(value.flow_breadth),"以有效个股资金样本为分母")}${kpi("成分上涨比例",pct(value.price_breadth),`涨跌中位 ${pct(value.median_change)}`)}${kpi("前三只正流入占比",pct(value.top3_share),`正流入HHI ${num(value.hhi,3)}；越高越集中`)}${kpi("有效资金覆盖率",pct(value.coverage),`成分净额合计 ${money(value.member_net)}`)}</div><p class="mh-note">集中度分母为全部正流入成分的正净额之和。成分合计与供应商板块净额可能存在口径差异，分别展示。</p>
      ${risk.length?`<ul class="sd-risks">${risk.map(v=>`<li>${esc(v)}</li>`).join("")}</ul>`:'<p class="mh-note">当前观测未命中所列背离、减速、单日集中和弱扩散条件；仍需后续交易日验证。</p>'}
      <label class="mh-controls" for="sdMemberSearch">筛选成分<input id="sdMemberSearch" type="search" placeholder="代码或名称"></label><div id="sdMemberTable"></div></article>
      <details class="mh-box"><summary>逐日资金原始依据（最近20个交易日）</summary><div class="mh-table-wrap"><table><thead><tr><th>日期</th><th>主力净额</th><th>净占比</th><th>板块涨跌</th><th>同类排名</th></tr></thead><tbody>${[...history].slice(-20).reverse().map(v=>`<tr><td>${esc(v.date)}</td><td>${money(v.net)}</td><td>${pct(v.rate)}</td><td>${pct(v.pct)}</td><td>${v.rank}</td></tr>`).join("")}</tbody></table></div></details>`;
    function members() {
      const text=$("sdMemberSearch").value.trim(),stocks=value.stocks.filter(v=>`${v.code} ${v.name}`.includes(text));
      $("sdMemberTable").innerHTML=`<div class="mh-table-wrap sd-member-wrap"><table><thead><tr><th>成分 / 分析</th><th>同日主力净额</th><th>净占比</th><th>涨跌幅</th><th>实际数据日</th></tr></thead><tbody>${stocks.map(v=>`<tr><td><button type="button" class="mh-inline-link" data-sd-stock="${esc(v.code)}" data-sd-name="${esc(v.name)}">${esc(v.name)} ${esc(v.code)}</button></td><td class="${tone(v.net)}">${money(v.net)}</td><td>${pct(v.rate)}</td><td>${pct(v.pct)}</td><td>${esc(v.date)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    members();$("sdMemberSearch").addEventListener("input",members);$("sdDetailRefresh").addEventListener("click",()=>choose(selected,true));
  }
  async function choose(code,refresh=false) {
    if (!(data?.rows||[]).some(r=>r.code===code)) return;
    const id=++requestId;selected=code;details=null;remember();renderRank();
    $("sdDetail").innerHTML='<div class="mh-box mh-empty" role="status">正在核对同日成分、个股资金与板块量价…</div>';
    try {const value=await api(`/api/market-health/sectors/detail?code=${encodeURIComponent(code)}${refresh?"&refresh=1":""}`);if(id!==requestId)return;details=value;renderDetail(value);}
    catch(e){if(id===requestId)$("sdDetail").innerHTML=`<div class="mh-box mh-notice" role="alert">${esc(e.message)} <button type="button" class="mh-inline-link" data-sd-code="${esc(code)}">重试读取</button></div>`;}
  }
  async function load() {
    clearTimeout(poll);
    try {
      const value=await api("/api/market-health/sectors"),changed=!data||value.updated_at!==data.updated_at;data=value;error();
      $("sdMeta").textContent=`资金实际披露日 ${data.as_of||"未取得"} · ${data.as_of===data.today?"今日收盘数据":"历史披露数据，非今日盘中"} · 来源 ${data.source||"现有配置接口"} · 采集 ${data.updated_at||"尚未完成"}`;
      const job=data.job,busy=job&&["queued","running"].includes(job.status);
      $("sdRefresh").disabled=Boolean(busy);$("sdExport").disabled=!data.rows.length;
      $("sdStatus").textContent=busy?`板块研究更新中 ${job.progress||0}% · ${job.logs?.at(-1)||"准备历史回补"}`:job?.status==="failed"?job.error:[data.live?.note,...(data.errors||[])].filter(Boolean).join("；");
      if(data.rows.length) {
        renderRank();
        if(changed||!selected) {const row=data.rows.find(r=>r.code===selected)||filtered()[0]||group()[0];if(row)choose(row.code);}
      }
      if(busy) poll=setTimeout(load,2500);
      if(!auto&&!busy&&(!data.rows.length||data.as_of<data.today)) {auto=true;refresh();}
    } catch(e){error(`${e.message}。已读取的板块研究保留。`);$("sdRefresh").disabled=false;}
  }
  async function refresh() {
    error();$("sdRefresh").disabled=true;$("sdStatus").textContent="正在启动41个交易日完整研究与同日成分穿透…";
    try {await api("/api/market-health/sectors/refresh",{method:"POST",body:"{}"});await load();}
    catch(e){error(e.message);$("sdRefresh").disabled=false;}
  }
  root.addEventListener("click",e=>{const button=e.target.closest("[data-sd-code]");if(button)choose(button.dataset.sdCode);const stock=e.target.closest("[data-sd-stock]");if(stock&&typeof window.openStockKlineModal==="function")window.openStockKlineModal(stock.dataset.sdStock,240,{stockName:stock.dataset.sdName}).catch(e=>error(e.message));});
  for(const id of ["sdSearch","sdSort","sdPositive"]) $(id).addEventListener(id==="sdSearch"?"input":"change",()=>data&&renderRank());
  $("sdType").addEventListener("change",()=>{if(!data)return;renderRank();const r=filtered()[0]||group()[0];if(r)choose(r.code);});
  $("sdRefresh").addEventListener("click",refresh);
  document.addEventListener("market-health-full-refresh",()=>{if(started&&!$("sdRefresh").disabled)refresh();});
  $("sdExport").addEventListener("click",()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({research:data,selected_detail:details},null,2)],{type:"application/json"}));const a=document.createElement("a");a.href=url;a.download=`板块深度_${data.as_of}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  function activate(){if(!root.hidden&&!started){started=true;load();}}
  new MutationObserver(activate).observe(root,{attributes:true,attributeFilter:["hidden"]});activate();
  window.addEventListener("pagehide",()=>clearTimeout(poll));
})();
