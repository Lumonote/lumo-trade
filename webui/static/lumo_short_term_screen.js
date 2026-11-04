// 模块依赖继承入口版本，避免刷新后仍命中旧的场景脚本。
await import(new URL("./lumo_short_term_flow.js?v="+document.getElementById("stScreen").dataset.flowV,import.meta.url));
await import(new URL("./lumo_short_term_scene.js?v="+document.getElementById("stScreen").dataset.sceneV,import.meta.url));
/* 短线大屏：各分区保留真实数据日期，所有点选均可追溯到完整记录。 */
(() => {
  "use strict";
  const $=id=>document.getElementById(id),root=$("stScreen");if(!root)return;
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const formats=new Map();
  const n=(v,d=0)=>{if(v==null||!Number.isFinite(Number(v)))return "—";if(!formats.has(d))formats.set(d,new Intl.NumberFormat("zh-CN",{maximumFractionDigits:d}));return formats.get(d).format(Number(v));};
  const pct=v=>v==null?"—":`${Number(v)>0?"+":""}${n(v,2)}%`;
  const money=v=>v==null?"—":`${v>0?"+":""}${n(v/1e8,2)}亿`;
  const tone=v=>v>0?"st-positive":v<0?"st-negative":"st-neutral";
  const sourceNames={jinshi:"金十快讯",ths_news:"同花顺快讯",sina_news:"新浪财经",eastmoney_news:"东方财富",radar:"盘口异动",sectors:"板块资金",limits:"实际涨跌停",calendar:"财经与会议日程",popularity:"股吧人气排行"};
  const kindLabels={U:"涨停",D:"跌停",Z:"炸板",O:"跌停开板"};
  const EMPTY='<p class="st-empty">当前筛选暂无有效记录。缺失来源不会补成零值或编造消息。</p>';
  let data=null,loadSequence=0,orderTimer=null,orderSequence=0,orderBusy=false,scrolling=true,graphKey="",focusSector="",drawerState=null,backStack=[];
  const scrollStates=new Map(),feedStates=new Map(),fieldRevisions=new Map(),arrivalGlows=new Map(),arrivalNodeTimers=new Map();let disposed=false,incomingUpdate=false,orderHydrated=false,sceneLayoutKey="";
  const feedConfig={radar:[60000,1600],news:[45000,4200],popularity:[180000,7800],sectors:[120000,11500],limits:[90000,17300]};
  const maps={};const pageSize=30;
  const scene=new window.LumoShortTermScene($("stScene"),$("stNodes"),(kind,id)=>{scene.setSelected(id);open(kind,id);});
  root.dataset.motion=scene.motion?"on":"off";
  $("stMotion").setAttribute("aria-pressed",String(scene.motion));$("stMotion").textContent=scene.motion?"动效开":"动效关";
  function timeText(value){const text=String(value||"");const match=text.match(/(\d{2}:\d{2})(?::\d{2})?/);return match?match[0]:text||"时间待补";}
  function error(message=""){ $("stError").hidden=!message;$("stError").textContent=message; }
  async function api(url,options={}){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),30000);try{const response=await fetch(url,{...options,headers:{"Content-Type":"application/json"},signal:controller.signal});const value=await response.json();if(!response.ok)throw new Error(value.error||"数据请求失败");return value;}finally{clearTimeout(timer);}}
  function button(kind,id,text){return `<button type="button" data-st-kind="${kind}" data-st-id="${esc(id)}">${esc(text)}</button>`;}
  function chips(kind,ids){return `<div class="st-chips">${ids.map(id=>{const row=maps[kind]?.get(id);return row?button(kind,id,row.name||row.title||id):"";}).join("")||'<span>尚无明确关联记录</span>'}</div>`;}
  function metrics(items){return `<div class="st-detail-metrics">${items.map(([label,value])=>`<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>`;}
  function matches(row,keyword){return !keyword||[row.name,row.code,row.title,row.summary,row.industry,row.source,...(row.themes||[])].join(" ").toLowerCase().includes(keyword);}
  function rowsFor(kind){const rows=data?.[kind]||[];return kind==="sectors"?rankSectors(sectorUniverse(rows)):kind==="stocks"?rows.filter(r=>!r.membership_only):kind==="popularity_rises"?rows.filter(r=>r.hot_rank<=Number($("stRankCeiling").value)&&r.hot_rank_change>=Number($("stRankGain").value)):rows;}
  function visible(kind){const keyword=$("stSearch").value.trim().toLowerCase();return rowsFor(kind).filter(r=>matches(r,keyword));}
  function rankChange(row){return row.hot_rank_change==null?"变动待补":row.hot_rank_change>0?`↑${n(row.hot_rank_change)}名`:row.hot_rank_change<0?`↓${n(-row.hot_rank_change)}名`:"持平";}
  function streakText(row){return `${row.streak_lower_bound?"≥":""}${n(row.streak)}${row.kind==="U"?"连板":"连跌停"}`;}
  function sectorQuote(row){return row.quote||{pct:row.pct,net:row.net,pct_label:"板块涨跌",net_label:"板块净额",pct_at:row.date,net_at:row.date,note:""};}
  function sectorSort(){return $("stSectorSort").value;}
  function sectorSortLabel(){return {heat:"交易热度",flow:"行业资金",concept_flow:"概念资金",component_flow:"成分资金观察"}[sectorSort()];}
  function sectorUniverse(rows){const mode=sectorSort();return rows.filter(row=>{const q=sectorQuote(row);if(mode==="heat")return sectorHeat(row)!=null;return q.net!=null&&String(q.net_at||"").slice(0,10)===data.today&&(mode==="component_flow"?q.net_label==="成分净额":q.net_label!=="成分净额"&&row.type===(mode==="concept_flow"?"概念":"行业"));});}
  function sectorHeat(row){return row.trading_heat??row.activity??null;}
  function membershipText(row){return row.membership?.complete?`${row.membership.total}成分`:(row.stock_codes.length?`覆盖${row.stock_codes.length}股 · 成分待补`:"成分待补");}
  function signalDetail(row,key){return row.signal_details?.[key]||{value:null,unit:key==="资金流入"?"元":key==="消息提及"?"条":"股",basis:"旧快照未保存该信号的统计依据，请查看新版快照。",stock_codes:[],complete:false};}
  function signalValue(meta,key){if(meta.value==null)return "—";const value=Number(meta.value),text=key==="资金流入"?(value!==0&&Math.abs(value)<1e6?`${value<0?"−":""}<0.01`:`${value>0?"+":""}${n(value/1e8,2)}`):n(value);return text+(meta.complete?"":"*");}
  function renderMatrix(sectors){
    const matrix=sectors.slice(0,7),keys=data.signals,labels={拉抬:"拉抬股",砸盘:"砸盘股",涨停:"涨停股",跌停:"跌停股",资金流入:"净流入",消息提及:"直接消息"};
    const maxima=new Map(keys.map(key=>[key,Math.max(1,...matrix.map(row=>Math.abs(Number(signalDetail(row,key).value)||0)))]));
    updateList("stMatrix",`<div class="st-matrix"><span>板块</span>${keys.map(key=>`<span>${esc(labels[key]||key)}<small>${key==="资金流入"?"亿元":key==="消息提及"?"条":"股"}</small></span>`).join("")}${matrix.map(row=>`${button("sector",row.id,row.name).replace('<button ','<button class="st-matrix-name" ')}${keys.map(key=>{const meta=signalDetail(row,key),value=meta.value,ratio=Math.abs(Number(value)||0)/maxima.get(key),color=value<0?"#70514d":ratio>.6?"#547f89":ratio>.25?"#365b6b":"#263d4e",coverage=meta.total!=null?`覆盖${meta.covered??"未取得"}/${meta.total}股`:"覆盖范围见明细",title=`${row.name} · ${key} ${value??"未取得"}${meta.unit} · ${meta.date||"日期未取得"} · ${coverage}；${meta.basis}`;return `<button type="button" class="${value==null?"st-cell-missing":value!==0?"st-cell-hit":""}" style="--cell-color:${color}" title="${esc(title)}" aria-label="${esc(title)}" data-st-kind="signal" data-st-id="${esc(row.id+"|"+key)}">${esc(signalValue(meta,key))}</button>`;}).join("")}`).join("")}</div>`);
    $("stMatrixNote").textContent=`${data.today} ${data.current_only?"当日累计":"快照（各列日期见明细）"} · *样本 · —未取得；按列着色`;
  }
  function renderSignal(id){
    const split=id.lastIndexOf("|"),row=maps.sector.get(id.slice(0,split)),key=id.slice(split+1);if(!row)return '<p>该板块已退出当前快照。</p>';
    const meta=signalDetail(row,key),member=row.membership||{},count=key==="资金流入"&&meta.complete?"板块整体报价":meta.total!=null?`${meta.covered??"—"} / ${meta.total}股`:meta.covered!=null?`${meta.covered}${key==="消息提及"?"条消息":"股"}`:"未取得";
    return `${metrics([[key+(key==="资金流入"?"（亿元）":"（"+meta.unit+"）"),signalValue(meta,key)],["实际数据日期",meta.date||"未取得"],["统计覆盖",count]])}<h3>统计依据</h3><p>${esc(meta.basis)}</p><p>${meta.value==null?"数据未取得，不能解释为0。":meta.complete?"当前列已取得完整统计依据；0表示没有命中。":"* 为已覆盖样本的观测值，不能当作整个板块的完整统计。"}</p><p>成分来源：${esc(member.source||"未取得")}；成分日期：${esc(member.date||"未取得")}；${esc(member.complete?`完整成分 ${member.total}股`:member.error||"完整成分待补")}。</p>${key==="资金流入"?`<h3>资金来源</h3><p>${esc(sectorQuote(row).source||"未取得")}<br>原始净额 ${n(meta.value,2)}元（负值为净流出）。</p>`:key==="消息提及"?`<h3>直接提及的消息 · ${(meta.news_ids||[]).length}条</h3>${newsCards(meta.news_ids||[])}`:`<h3>命中股票 · ${(meta.stock_codes||[]).length}只</h3>${meta.stock_codes?.length?chips("stock",meta.stock_codes):`<p>${meta.value==null?"尚无可核验记录。":"当前覆盖范围内没有命中股票。"}</p>`}`}<h3>完整板块档案</h3>${button("sector",row.id,row.name)}`;
  }
  function sectorScore(row,mode){return mode==="flow"?sectorQuote(row).net:sectorHeat(row);}
  function rankSectors(rows){const mode=sectorSort()==="heat"?"heat":"flow",secondary=mode==="heat"?"flow":"heat",compare=(a,b)=>(b==null?-Infinity:Number(b))-(a==null?-Infinity:Number(a)),date=row=>String(mode==="flow"?sectorQuote(row).net_at||"":row.heat_date||row.radar_date||data?.radar_as_of||"").slice(0,10);return [...rows].sort((a,b)=>Number(sectorScore(a,mode)==null)-Number(sectorScore(b,mode)==null)||(date(a)<date(b)?1:date(a)>date(b)?-1:0)||compare(sectorScore(a,mode),sectorScore(b,mode))||compare(sectorScore(a,secondary),sectorScore(b,secondary))||(a.name<b.name?-1:a.name>b.name?1:a.id<b.id?-1:a.id>b.id?1:0));}
  function applyIncoming(paint){incomingUpdate=true;try{paint();}finally{incomingUpdate=false;}}
  function pulseArrival(node){
    if(!node||disposed||document.hidden||root.dataset.motion!=="on")return false;
    if(arrivalGlows.has(node))return true;if(arrivalGlows.size>=8)return false;
    node.classList.add("st-data-arrival");const timer=setTimeout(()=>{node.classList.remove("st-data-arrival");arrivalGlows.delete(node);},5000);arrivalGlows.set(node,timer);return true;
  }
  function clearArrivals(){for(const [node,timer] of arrivalGlows){clearTimeout(timer);node.classList.remove("st-data-arrival");}arrivalGlows.clear();for(const timer of arrivalNodeTimers.values())clearTimeout(timer);arrivalNodeTimers.clear();for(const state of scrollStates.values()){state.arrivals.clear();state.arrivalObserver?.disconnect();}}
  function observeArrivals(host,state){
    if(!state.arrivalObserver)return;state.arrivalObserver.disconnect();
    for(const [key,expires] of state.arrivals)if(expires<Date.now())state.arrivals.delete(key);
    for(const row of [...state.content.children,...state.copy.children])if(state.arrivals.has(row.dataset.stId||row.dataset.stCopyId))state.arrivalObserver.observe(row);
  }
  function announceArrival(host,keys){
    if(!keys.length||document.hidden||root.dataset.motion!=="on")return;
    pulseArrival(host.closest(".st-panel")?.querySelector("header"));const state=scrollStates.get(host);if(!state)return;
    for(const key of keys.slice(0,4))state.arrivals.set(key,Date.now()+60000);
    while(state.arrivals.size>12)state.arrivals.delete(state.arrivals.keys().next().value);
    observeArrivals(host,state);
    const stockCodes=new Set(),sectorIds=new Set();for(const key of keys.slice(0,4)){
      if(maps.stock?.has(key))stockCodes.add(key);if(maps.sector?.has(key))sectorIds.add(key);
      const order=maps.order?.get(key),news=maps.news?.get(key);if(order)stockCodes.add(order.code);for(const code of news?.stock_codes||[])stockCodes.add(code);for(const id of news?.sector_ids||[])sectorIds.add(id);
    }
    for(const code of stockCodes)for(const id of maps.stock?.get(code)?.sector_ids||[])sectorIds.add(id);
    queueMicrotask(()=>{if(disposed||root.dataset.motion!=="on")return;for(const hit of scene.pulseUpdates?.({stockCodes:[...stockCodes],sectorIds:[...sectorIds]})||[])if(!arrivalNodeTimers.has(hit.id)&&arrivalNodeTimers.size<12){arrivalNodeTimers.set(hit.id,setTimeout(()=>{arrivalNodeTimers.delete(hit.id);const node=[...$("stNodes").querySelectorAll("[data-st-node]")].find(node=>node.dataset.stNode===hit.id);pulseArrival(node);},hit.delay));}});
  }
  function limitList(rows){return rows.map(r=>`<button type="button" class="st-limit-row" data-st-kind="stock" data-st-id="${esc(r.code)}"><strong>${esc(r.name)}<small>${esc(r.code)}</small></strong><span class="${tone(r.pct)}">${pct(r.pct)}</span><small>${esc(streakText(r))} · ${r.one_price?"一字板":r.first_time?"首封 "+esc(r.first_time):"封板时间待补"}</small><small>${n(r.close,2)}元 · 开板${n(r.open_times)}次</small></button>`).join("")||'<p class="st-empty">当前快照或筛选暂无该类核验记录。</p>';}
  function updateList(id,markup){
    const host=$(id);if(host.__markup===markup)return;const state=scrollStates.get(host),target=state?.content||host,old=new Map([...target.children].map(row=>[row.dataset.stId,row]));
    let anchor=null,anchorTop=0;if(state&&state.period){const offset=scrollOffset(host,state)%state.period,base=target.offsetTop;for(const row of target.children)if(row.offsetTop-base+row.offsetHeight>offset){anchor=row;anchorTop=row.offsetTop-base;break;}}
    const template=document.createElement("template");template.innerHTML=markup;let cursor=target.firstElementChild;const added=[],changed=[];
    for(const fresh of [...template.content.children]){const key=fresh.dataset.stId,previous=key&&old.get(key),html=fresh.outerHTML;let row=previous&&previous.__rowMarkup===html?previous:fresh;
      if(row===fresh){row.__rowMarkup=html;if(incomingUpdate&&host.__markup!==undefined&&key)(previous?changed:added).push(key);}
      if(row!==cursor)target.insertBefore(row,cursor);
      if(previous&&previous!==row)previous.remove();cursor=row.nextElementSibling;if(key)old.delete(key);
    }
    for(const row of old.values())if(row.isConnected)row.remove();
    host.__markup=markup;if(state)measureScroll(host,state,true,anchor?.isConnected?anchor.offsetTop-target.offsetTop-anchorTop:0);
    if(incomingUpdate)announceArrival(host,[...added,...changed]);
  }
  function setValue(id,value){const host=$(id),text=String(value);if(host.textContent!==text){host.textContent=text;host.animate?.([{opacity:.35,transform:"translateY(3px)"},{opacity:1,transform:"translateY(0)"}],{duration:400,easing:"ease-out"});}}
  function orderMoney(value){return value==null?"金额待补":value>=1e8?n(value/1e8,2)+"亿":value>=1e4?n(value/1e4,2)+"万":n(value,2)+"元";}
  function renderOrders(update=true){
    maps.order=new Map([...(data.big_buys||[]),...(data.big_sells||[])].map(r=>[r.id,r]));
    for(const row of maps.order.values())if(!maps.stock.has(row.code))maps.stock.set(row.code,{code:row.code,name:row.name,date:row.date,price:row.price??null,change_pct:row.change_pct??null,sector_ids:[],news_ids:[],event_ids:[],limits:[],timeline:[],order_events:[row],membership_basis:"盘口异动源未提供板块归属"});
    if(!update)return;
    for(const [side,key,host,count,stamp] of [["buy","big_buys","stBuyList","stBuyCount","stBuyAsOf"],["sell","big_sells","stSellList","stSellCount","stSellAsOf"]]){
      const rows=visible(key),meta=data.order_meta?.[side]||{};setValue(count,rows.length);
      $(stamp).textContent=meta.date?`${meta.date} · ${meta.fetched_at?timeText(meta.fetched_at):"时间待补"} · ${meta.error?"保留缓存":"滚动展示最新200条"}`:meta.error||"等待有效盘口记录 · 自动连接";
      $(stamp).title=meta.error||"异动源只给时分秒，交易日经指数报价校验；不猜测委托金额。";
      updateList(host,rows.slice(0,200).map(row=>`<button type="button" class="st-order-row" data-st-kind="order" data-st-id="${esc(row.id)}"><time>${esc(row.time)}</time><span><strong>${esc(row.name)} <small>${esc(row.code)}</small></strong><small>${esc(row.label)} · ${row.price==null?"价格待补":n(row.price,2)+"元"} · ${pct(row.change_pct)}</small></span><i class="st-order-${side}">${orderMoney(row.amount)}<small>${side==="buy"?"BUY":"SELL"}</small></i></button>`).join("")||'<p class="st-empty">当前筛选或来源快照暂无该类异动。系统会自动更新。</p>');
    }
  }
  function scrollOffset(host,state){return state.animation?((Number(state.animation.currentTime)||0)%state.duration)/state.duration*state.period:host.scrollTop;}
  function syncScroll(host,state){
    const paused=!scrolling||document.hidden||$("stDrawer").open||state.hover||state.focus||performance.now()<state.manual;
    const status=state.period<=host.clientHeight+2?"idle":paused?"paused":"active";
    if(host.dataset.scrolling!==status)host.dataset.scrolling=status;
    if(status!=="active"){
      if(state.animation){state.position=scrollOffset(host,state);state.animation.cancel();state.animation=null;state.native=true;host.scrollTop=state.position;}
      return;
    }
    if(!state.animation){state.position=host.scrollTop%state.period;state.native=false;host.scrollTop=0;state.duration=state.period/.018;
      state.animation=state.track.animate([{transform:"translate3d(0,0,0)"},{transform:`translate3d(0,${-state.period}px,0)`}],{duration:state.duration,iterations:Infinity,easing:"linear"});
      state.animation.currentTime=state.position/.018;
    }
  }
  function syncAllScroll(){for(const [host,state] of scrollStates)syncScroll(host,state);}
  function measureScroll(host,state,changed=false,adjust=0){
    const position=scrollOffset(host,state)+adjust,period=state.content.scrollHeight,overflow=period>host.clientHeight+2;
    if(changed||period!==state.period){
      if(state.animation){state.animation.cancel();state.animation=null;}
      state.period=period;state.position=position;state.copy.hidden=!overflow;
      if(overflow){state.copy.replaceChildren(...[...state.content.children].map(row=>{const copy=row.cloneNode(true);copy.classList.remove("st-data-arrival");return copy;}));for(const node of state.copy.querySelectorAll("[data-st-kind]")){node.dataset.stCopyKind=node.dataset.stKind;node.dataset.stCopyId=node.dataset.stId;delete node.dataset.stKind;delete node.dataset.stId;node.tabIndex=-1;}}
      else state.copy.replaceChildren();
      host.scrollTop=period?position%period:0;
    }
    state.track.style.willChange=overflow?"transform":"auto";syncScroll(host,state);observeArrivals(host,state);
  }
  function setupScroll(){for(const host of root.querySelectorAll(".st-panel .st-scroll")){
    const track=document.createElement("div"),content=document.createElement("div"),copy=document.createElement("div");track.className="st-scroll-track";content.className="st-scroll-content";copy.className="st-scroll-copy";copy.setAttribute("aria-hidden","true");content.append(...host.childNodes);track.append(content,copy);host.append(track);
    const state={track,content,copy,period:0,position:0,duration:1,animation:null,manual:0,hover:false,focus:false,native:false,arrivals:new Map(),arrivalObserver:null};scrollStates.set(host,state);
    if(window.IntersectionObserver)state.arrivalObserver=new IntersectionObserver(entries=>{for(const entry of entries){if(!entry.isIntersecting)continue;const row=entry.target,key=row.dataset.stId||row.dataset.stCopyId;if((state.arrivals.get(key)||0)>Date.now()&&pulseArrival(row))state.arrivals.delete(key);state.arrivalObserver.unobserve(row);}},{root:host,threshold:.15});
    Object.defineProperty(host,"__stScrollOffset",{get:()=>scrollOffset(host,state)});
    host.addEventListener("pointerenter",()=>{state.hover=true;syncScroll(host,state);});host.addEventListener("pointerleave",()=>{state.hover=false;syncScroll(host,state);});
    host.addEventListener("focusin",()=>{state.focus=true;syncScroll(host,state);});host.addEventListener("focusout",event=>{state.focus=host.contains(event.relatedTarget);syncScroll(host,state);});
    host.addEventListener("scroll",()=>{if(state.native)state.position=host.scrollTop;},{passive:true});
    state.observer=new ResizeObserver(()=>measureScroll(host,state));state.observer.observe(host);measureScroll(host,state);
  }}
  function spark(history){const values=history.map(r=>r.net).filter(v=>v!=null).slice(-18);if(values.length<2)return "";const lo=Math.min(...values),span=Math.max(...values)-lo||1;return `<svg class="st-mini-line" viewBox="0 0 200 14" preserveAspectRatio="none" aria-hidden="true"><path d="${values.map((v,i)=>`${i?"L":"M"}${i*200/(values.length-1)},${12-(v-lo)/span*10}`).join(" ")}" fill="none" stroke="#9bbfc0" stroke-width="1"/></svg>`;}
  function newsList(rows,kind="news"){return rows.map(r=>`<button class="st-news-row" type="button" data-st-kind="news" data-st-id="${esc(r.id)}"><time>${esc(timeText(r.published_at))}</time><span><strong>${esc(r.title)}</strong><small>${esc(r.source)} · ${esc(r.date||"发表日期未齐")} · ${esc(r.themes.join(" / ")||r.category)}</small></span></button>`).join("")||EMPTY;}
  function sceneCapacity(){const width=$("stScene").clientWidth,height=$("stScene").clientHeight,slots=width>=960?{sectors:12,columns:8}:width>=680?{sectors:10,columns:6}:width>=460?{sectors:8,columns:4}:{sectors:4,columns:3};return {...slots,width,height};}
  function render(parts=null,owner=""){
    const owned={radar:["stocks","sectors"],news:["news"],popularity:["hot_stocks","popularity_rises"],sectors:["sectors"],limits:["summary","limit_up","limit_down"]}[owner];
    const has=key=>(!parts||parts.includes(key))&&(!owned||owned.includes(key));
    maps.sector=new Map(data.sectors.map(r=>[r.id,r]));maps.stock=new Map(data.stocks.map(r=>[r.code,r]));maps.news=new Map(data.news.map(r=>[r.id,r]));maps.event=new Map(data.calendar.map(r=>[r.id,r]));
    const sectors=visible("sectors"),stocks=visible("stocks"),news=visible("news");
    if(has("sectors"))setValue("stSectorCount",sectors.length);if(has("stocks"))setValue("stStockCount",stocks.length);if(has("news"))setValue("stNewsCount",news.length);
    if(has("sectors"))updateList("stSectorList",sectors.slice(0,18).map((r,index)=>{const q=sectorQuote(r);return `<button class="st-sector-row" data-st-kind="sector" data-st-id="${esc(r.id)}" type="button" aria-pressed="${r.id===focusSector}" title="${esc(`热度 ${n(sectorHeat(r),1)} · ${r.heat_date||r.radar_date||'日期待补'}；${r.heat_basis||'雷达平均活跃度'}；${q.pct_label} ${q.pct_at||'待补'}；${q.net_label} ${q.net_at||'待补'}。${q.note}`)}"><strong><i class="st-sector-rank">${index+1}</i>${esc(r.name)}</strong><span class="st-numeric ${tone(q.pct)}">${pct(q.pct)}</span><small>热度${n(sectorHeat(r),1)} · ${esc(membershipText(r))} · ${esc(q.pct_label)} · ${esc(q.pct_at||"日期待补")}</small><small class="st-numeric">${money(q.net)} · ${esc(q.net_label)}</small>${spark(r.history||[])}</button>`;}).join("")||EMPTY);
    if(has("stocks"))updateList("stStockList",stocks.slice(0,20).map(r=>`<button class="st-stock-row" type="button" data-st-kind="stock" data-st-id="${esc(r.code)}"><strong>${esc(r.name)} <small>${esc(r.code)}</small></strong><span class="st-numeric ${tone(r.change_pct)}">${pct(r.change_pct)}</span><small>${esc(r.direction||r.limits.map(e=>kindLabels[e.kind]).join(" / ")||"待核对方向")} · 活跃${n(r.activity)} · 异动${n(r.changes_total)}次</small><small class="st-numeric">${money(r.main_net_inflow)}</small></button>`).join("")||EMPTY);
    const s=data.summary;if(has("summary"))updateList("stEmotion",`<div class="st-emotion-grid"><div><span>涨停 / 跌停</span><strong>${data.limit_as_of?`${s.limit_up}/${s.limit_down}`:"—/—"}</strong></div><div><span>炸板</span><strong>${data.limit_as_of?n(s.broken):"—"}</strong></div><div><span>最高连板</span><strong>${n(s.max_streak)}</strong></div></div>`);
    $("stLimitDate").textContent=data.limit_as_of||"尚无核验限价";
    const up=visible("limit_up"),down=visible("limit_down"),hot=visible("hot_stocks"),rapid=visible("popularity_rises");
    if(has("limit_up"))setValue("stUpCount",up.length);if(has("limit_down"))setValue("stDownCount",down.length);if(has("hot_stocks"))setValue("stHotCount",hot.length);if(has("popularity_rises"))setValue("stRapidCount",rapid.length);
    const limitStamp=data.limit_as_of?`交易日 ${data.limit_as_of} · ${data.limit_provisional?"盘中动态池 · 采集 "+timeText(data.limit_fetched_at):"已核验收盘"}`:"今日涨跌停正在自动连接";
    $("stUpAsOf").textContent=limitStamp;$("stDownAsOf").textContent=limitStamp;
    if(has("limit_up"))updateList("stUpList",limitList(up));if(has("limit_down"))updateList("stDownList",limitList(down));
    $("stHotAsOf").textContent=data.hot_fetched_at?`股吧人气原始排名 · 抓取 ${data.hot_fetched_at.slice(5,19).replace("T"," ")}`:"股吧人气排名待更新";
    if(has("hot_stocks"))updateList("stHotList",hot.map(r=>`<button type="button" class="st-rank-row" data-st-kind="stock" data-st-id="${esc(r.code)}"><strong><small>#${n(r.hot_rank)}</small> ${esc(r.name)}<small>${esc(r.code)}</small></strong><span class="${r.hot_rank_change>0?"st-rank-gain":""}">${esc(rankChange(r))}</span><small class="st-rank-path">${r.hot_rank_before==null?"原始人气名次":`昨日 #${n(r.hot_rank_before)} → 当前 #${n(r.hot_rank)}`}</small><small>较昨日</small></button>`).join("")||'<p class="st-empty">暂无有效人气榜，后台将自动重试。</p>');
    $("stRapidAsOf").textContent=`当前名次优先 · 较昨日 · ${data.hot_fetched_at?data.hot_fetched_at.slice(5,16).replace("T"," ")+"抓取":"待更新"}`;
    if(has("popularity_rises"))updateList("stRapidList",rapid.map(r=>`<button type="button" class="st-rank-row" data-st-kind="stock" data-st-id="${esc(r.code)}"><strong>${esc(r.name)}<small>${esc(r.code)}</small></strong><span class="st-rank-gain">↑${n(r.hot_rank_change)}名</span><small class="st-rank-path">昨日 #${n(r.hot_rank_before)} → 当前 #${n(r.hot_rank)}</small><small>当前第${n(r.hot_rank)}名</small></button>`).join("")||'<p class="st-empty">暂无符合当前排名范围和上升名数的股票，可调整筛选。缺少对比数据时不推算名次。</p>');
    if(has("news"))updateList("stNews",newsList(news.slice(0,30)));
    if(!parts||parts.includes("sectors")||owner==="ranking")renderMatrix(sectors);
    if(!parts||has("sectors")||owner==="scene"){
    const capacity=sceneCapacity();sceneLayoutKey=JSON.stringify(capacity);const graphSectors=sectors.slice(0,capacity.sectors);
    const graphCodes=[],graphCodeSet=new Set(),stockLimit=capacity.columns*3,maxMembers=Math.max(0,...graphSectors.map(row=>row.stock_codes.length));
    for(let round=0;round<maxMembers&&graphCodes.length<stockLimit;round++)for(const sector of graphSectors){const code=sector.stock_codes[round];if(code&&!graphCodeSet.has(code)){graphCodes.push(code);graphCodeSet.add(code);}if(graphCodes.length>=stockLimit)break;}
    const filteredGraph={...data,graph:{...data.graph,sector_ids:graphSectors.map(r=>r.id),stock_codes:graphCodes,
      columns:capacity.columns,viewport:[capacity.width,capacity.height],
      edges:graphSectors.flatMap(r=>r.stock_codes.filter(c=>graphCodeSet.has(c)).map(c=>({source:r.id,target:"stock:"+c,kind:"membership"})))}};
    const key=JSON.stringify(filteredGraph.graph);if(key!==graphKey){graphKey=key;scene.setData(filteredGraph);if(focusSector)scene.setSelected(focusSector);}
    const coverage=data.sector_status?.coverage?.[sectorSort()==="concept_flow"?"概念":"行业"];
    $("stGraphMeta").textContent=`${sectorSortLabel()} · 前 ${filteredGraph.graph.sector_ids.length} 板块 / ${graphCodes.length} 标的\n${coverage&&sectorSort()!=="heat"?`资金报价 ${coverage.received}/${coverage.total||"待补"}`:"今日有效数据"} · 完整成分 ${graphSectors.filter(r=>r.membership?.complete).length}/${graphSectors.length}板块`;
    $("stGraphMeta").title="按真实板块代码取得完整成分，行业、概念分别排名。资金报价覆盖与成分覆盖独立显示；成分未取得时保留板块并自动补齐，不编造关系。";
    }
    const sectorRanks=new Map(sectors.map((r,index)=>[r.id,index+1]));
    if(!owner||["radar","sectors","popularity","scene","ranking"].includes(owner))for(const node of $("stNodes").querySelectorAll("[data-st-node]")){const id=node.dataset.stNode,sector=maps.sector.get(id),stock=maps.stock.get(id),row=sector||stock;if(!row)continue;const title=node.querySelector("strong"),small=node.querySelector("small"),caption=sector?`#${sectorRanks.get(id)} ${sectorSort()==="heat"?"热度"+n(sectorHeat(sector),1):money(sectorQuote(sector).net)}${sector.stock_codes.length?"":" · 成分待补"}`:`${stock.code} · ${stock.change_pct==null?"待报价":Number(stock.change_pct).toFixed(2)+"%"}`;if(title&&title.textContent!==row.name)title.textContent=row.name;if(small&&small.textContent!==caption)small.textContent=caption;if(sector)node.title=`${sector.name} · 第${sectorRanks.get(id)}名 · 热度${n(sectorHeat(sector),1)} · 净额${money(sectorQuote(sector).net)} · ${sector.stock_codes.length}只关联股票`;}renderOrders(!parts);
    const failed=(data.sources||[]).filter(r=>r.state!=="已读取");
    $("stSourceSummary").textContent=`${data.sources.length}个来源 · ${failed.length}项待补 · 更新 ${data.updated_at||data.generated_at} · ${data.stocks.length}股 / ${data.sectors.length}板块`;
    $("stStatusDot").style.background=failed.length?"#b9ab83":"#8fb6b3";root.setAttribute("aria-busy","false");
  }
  function focus(row){focusSector=row.id;scene.setSelected(row.id);$("stFocus").innerHTML=`${button("sector",row.id,"完整档案 →")}<strong>${esc(row.name)}</strong>${esc(row.observation)}`;}
  function renderList(kind){
    const state=drawerState,keyword=state.keyword||"",rows=rowsFor(kind).filter(r=>matches(r,keyword));
    const pages=Math.max(1,Math.ceil(rows.length/pageSize));state.page=Math.min(Math.max(1,state.page||1),pages);const subset=rows.slice((state.page-1)*pageSize,state.page*pageSize);
    let header=[],rowHtml;
    if(kind==="sectors"){header=["排名 / 板块","涨跌 / 净额与口径","涨停 / 跌停","拉抬 / 砸盘","关联股 / 消息 / 日程","实际数据日"];rowHtml=(r,index)=>{const q=sectorQuote(r);return `<td>${button("sector",r.id,r.name)}<small>#${(state.page-1)*pageSize+index+1} · 热度${n(sectorHeat(r),1)} · ${esc(r.type)} ${esc(r.code)}</small></td><td>${pct(q.pct)} · ${esc(q.pct_label)}<small>${money(q.net)} · ${esc(q.net_label)}</small></td><td>${n(r.signals["涨停"])} / ${n(r.signals["跌停"])}</td><td>${n(r.signals["拉抬"])} / ${n(r.signals["砸盘"])}</td><td>${r.stock_codes.length} / ${r.news_ids.length} / ${r.event_ids.length}</td><td>${esc(q.pct_at||"待补")}<small>资金 ${esc(q.net_at||"待补")}</small></td>`;};}
    else if(kind==="stocks"){header=["股票","涨跌 / 现价","方向 / 活跃","异动次数","主力净额","实际数据日"];rowHtml=r=>`<td>${button("stock",r.code,r.name)}<small>${esc(r.code)}</small></td><td>${pct(r.change_pct)}<small>${n(r.price,2)}</small></td><td>${esc(r.direction||"待补")} / ${n(r.activity)}</td><td>${n(r.changes_total)}</td><td>${money(r.main_net_inflow)}</td><td>${esc(r.date||"待补")}</td>`;}
    else if(kind==="calendar"){header=["日程 / 日期","核验状态","来源 / 时间口径"];rowHtml=r=>`<td style="white-space:normal;min-width:230px">${button("event",r.id,r.title)}<small>${esc(r.date)} 至 ${esc(r.end_date)} · ${esc(r.state)}</small></td><td>${r.verified?"官方已核验":"待官方核验"}<small>核验 ${esc(r.verified_at||"未取得")}</small></td><td>${esc(r.source)}<small>${esc(r.timezone)}</small></td>`;}
    else if(["limits","limit_up","limit_down"].includes(kind)){header=["股票 / 日期","事件 / 连续数","价格 / 涨跌","开板 / 封单","首次 / 最后封板"];rowHtml=r=>`<td>${button("stock",r.code,r.name)}<small>${esc(r.code)} · ${esc(r.date)}</small></td><td>${esc(kindLabels[r.kind])}<small>${esc(streakText(r))}</small></td><td>${n(r.close,2)} / ${pct(r.pct)}</td><td>${n(r.open_times)} / ${money(r.fd_amount)}</td><td>${esc(r.first_time||"—")} / ${esc(r.last_time||"—")}</td>`;}
    else if(kind==="hot_stocks"){header=["原始人气名次","股票","排名来源 / 抓取时间","盘口行情日"];rowHtml=r=>`<td>#${n(r.hot_rank)}</td><td>${button("stock",r.code,r.name)}<small>${esc(r.code)}</small></td><td>${esc(r.hot_source)}<small>${esc(r.hot_fetched_at)}（抓取时刻）</small></td><td>${esc(r.date||"源未给出")}<small>${pct(r.change_pct)}</small></td>`;}
    else if(kind==="popularity_rises"){header=["股票","昨日 → 当前","上升名数 / 比较口径","抓取时刻"];rowHtml=r=>`<td>${button("stock",r.code,r.name)}<small>${esc(r.code)}</small></td><td>#${n(r.hot_rank_before)} → #${n(r.hot_rank)}</td><td>↑${n(r.hot_rank_change)}名<small>较昨日 · 当前名次优先</small></td><td>${esc(r.hot_fetched_at)}</td>`;}
    else if(kind==="big_buys"||kind==="big_sells"){header=["时间 / 交易日","股票 / 异动","来源"];rowHtml=r=>`<td>${esc(r.time)}<small>${esc(r.date)}</small></td><td>${button("order",r.id,r.name)}<small>${esc(r.code)} · ${esc(r.label)}</small></td><td>${esc(r.source)}<small>${esc(r.date_basis)}</small></td>`;}
    else{header=["消息 / 发表时间","来源 / 关联"];rowHtml=r=>`<td style="white-space:normal;min-width:280px">${button("news",r.id,r.title)}<small>${esc(r.published_at||"来源未给完整时间")} · ${esc(r.time_basis)}</small></td><td>${esc(r.sources.join(" / "))}<small>${r.sector_ids.length}板块 · ${r.stock_codes.length}直接个股</small></td>`;}
    return `<label>检索本表<input id="stDetailSearch" class="st-detail-search" type="search" value="${esc(keyword)}" placeholder="名称、代码、关键词或来源"></label><div id="stListResult"><p class="st-detail-note">共${rows.length}条；分页不截断完整导出。数值缺失显示“—”，关联详情保留全部记录。</p><div class="st-detail-table"><table><thead><tr>${header.map(x=>`<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>${subset.map(r=>`<tr>${rowHtml(r)}</tr>`).join("")||`<tr><td colspan="${header.length}">暂无匹配记录</td></tr>`}</tbody></table></div><div class="st-pager"><span>${state.page}/${pages}页 · ${rows.length}条</span><button type="button" data-st-page="${state.page-1}" ${state.page<=1?"disabled":""}>上一页</button><button type="button" data-st-page="${state.page+1}" ${state.page>=pages?"disabled":""}>下一页</button><button type="button" data-st-export-list>导出当前表CSV</button></div></div>`;
  }
  function eventCards(ids){return ids.map(id=>maps.event.get(id)).filter(Boolean).map(r=>`<div class="st-checklist"><div>${button("event",r.id,r.title)}<p>${esc(r.date)} · ${r.verified?"官方已核验":"待官方核验"} · ${esc(r.source)}</p></div></div>`).join("")||'<p>暂无已匹配的未来催化。</p>';}
  function newsCards(ids){return newsList(ids.map(id=>maps.news.get(id)).filter(Boolean));}
  function renderDetail(kind,id){
    const r=maps[kind]?.get(id);if(!r)return '<p class="st-empty">该记录未在当前快照中，可等待下一次自动更新。</p>';
    if(kind==="sector"){
      const q=sectorQuote(r);focus(r);return `${metrics([[q.pct_label,pct(q.pct)],[q.net_label,money(q.net)],["关联股票",r.stock_codes.length],["拉抬 / 砸盘",`${n(r.signals["拉抬"])} / ${n(r.signals["砸盘"])}`],["涨停 / 跌停",`${n(r.signals["涨停"])} / ${n(r.signals["跌停"])}`],["交易热度",n(sectorHeat(r),1)]])}<h3>板块发生了什么</h3><p>${esc(r.observation)}</p><p>涨跌时点 ${esc(q.pct_at||"未取得")}；资金时点 ${esc(q.net_at||"未取得")}；盘口日期 ${esc(r.radar_date||data.radar_as_of||"未取得")}。</p><p class="st-detail-note">热度 ${esc(r.heat_date||r.radar_date||"日期待补")}：${esc(r.heat_basis||"雷达平均活跃度")}，有效成分 ${n(r.heat_coverage)}只。${esc(q.note||q.source||"")}</p><p>成分：${esc(membershipText(r))}；${esc(r.membership?.date||"日期未取得")}；${esc(r.membership?.source||"完整成分待补")}。</p><h3>后续验证</h3><p>${esc(r.verification)}</p><h3>全部关联股票 · ${r.stock_codes.length}</h3>${chips("stock",r.stock_codes)}<h3>相关消息 · ${r.news_ids.length}</h3>${newsCards(r.news_ids)}<h3>未来事件 · ${r.event_ids.length}</h3>${eventCards(r.event_ids)}<h3>统计口径</h3><p>${esc(data.method)}</p>`;
    }
    if(kind==="stock"){
      const ranking=r.hot_rank?`<p class="st-detail-note">股吧人气原始排名 #${n(r.hot_rank)} · 抓取 ${esc(r.hot_fetched_at)}。抓取时间不是排名生成时间，盘口报价另以实际行情日为准。<br><a href="https://guba.eastmoney.com/rank/" target="_blank" rel="noopener noreferrer" data-st-external>查看人气榜来源 ↗</a></p>`:"";
      const rankRise=r.hot_rank_change!=null?`<p class="st-detail-note">人气名次：昨日 #${n(r.hot_rank_before)} → 当前 #${n(r.hot_rank)} · ${esc(rankChange(r))}（较昨日）。</p>`:"";
      const boardNews=[...new Set(r.sector_ids.flatMap(s=>maps.sector.get(s)?.news_ids||[]))].filter(n=>!r.news_ids.includes(n));
      const events=[...new Set([...r.event_ids,...r.sector_ids.flatMap(s=>maps.sector.get(s)?.event_ids||[])])];
      return `${ranking}${rankRise}${metrics([["涨跌",pct(r.change_pct)],["价格",n(r.price,2)],["盘口活跃",n(r.activity,1)],["异动 / 多 / 空",`${n(r.changes_total)} / ${n(r.changes_bull)} / ${n(r.changes_bear)}`],["主力净额",money(r.main_net_inflow)],["量比 / 换手",`${n(r.volume_ratio,2)} / ${pct(r.turnover)}`]])}<h3>异动说明与实际时点</h3><p>${esc(r.name)} ${esc(r.code)} · ${esc(r.date||"日期未取得")} · ${esc(r.direction||"方向待核对")}<br>${esc((r.reasons||[]).join("；")||"当前快照没有详细异动原因，不能据此补写原因。")}</p><p class="st-detail-note">盘口标签是公开数据构造的描述性代理，不代表确认了机构身份、操纵行为或未来涨跌。</p><h3>全部关联板块</h3>${chips("sector",r.sector_ids)}<p>关联依据：${esc(r.membership_basis)}</p><h3>封板记录</h3>${r.limits.length?`<div class="st-detail-table"><table><thead><tr><th>日期 / 事件</th><th>连板 / 连跌停</th><th>实际限价</th><th>封单 / 开板</th></tr></thead><tbody>${r.limits.map(e=>`<tr><td>${esc(e.date)} · ${esc(kindLabels[e.kind])}</td><td>${e.streak_lower_bound?"至少 ":""}${n(e.streak)}</td><td>${n(e.up_limit,2)} / ${n(e.down_limit,2)}</td><td>${money(e.fd_amount)} / ${n(e.open_times)}</td></tr>`).join("")}</tbody></table></div>`:'<p>当前封板日没有该股有效事件；这不代表此前从未涨跌停。</p>'}<h3>盘口时间线 · ${r.timeline.length}条</h3>${r.timeline.length?`<div class="st-detail-table"><table><thead><tr><th>时间</th><th>事件</th><th>价格</th><th>涨跌</th></tr></thead><tbody>${r.timeline.map(e=>`<tr><td>${esc(e[0])}</td><td>${esc(({rocket:"火箭发射",rebound:"快速反弹",big_buy:"大笔买入",buy_queue:"有大买盘",seal_up:"封涨停板",dive:"高台跳水",plunge:"加速下跌",big_sell:"大笔卖出",sell_queue:"有大卖盘",seal_down:"封跌停板",open_up:"打开涨停板",open_down:"打开跌停板"})[e[1]]||e[1])}</td><td>${n(e[2],2)}</td><td>${pct(e[3])}</td></tr>`).join("")}</tbody></table></div>`:'<p>此快照未保存逐笔事件时间线，未用日线反推盘口事件。</p>'}<h3>直接提及个股的消息</h3>${newsCards(r.news_ids)}<h3>板块相关消息 · 间接关联</h3>${newsCards(boardNews)}<h3>相关未来催化 · 需核对个股实际业务</h3>${eventCards(events)}<div class="st-detail-actions"><button type="button" data-st-kline="${esc(r.code)}">读取日K与量价走势</button></div><div id="stKline"></div>`;
    }
    if(kind==="order")return `${metrics([["股票",r.name+" "+r.code],["异动类型",r.label],["发生时间",r.occurred_at],["异动金额",orderMoney(r.amount)],["事件价格",n(r.price,2)],["源数量",n(r.volume)]])}<h3>大笔买卖监控</h3><p>${esc(r.source)} · ${esc(r.date_basis)}。</p><p>这是源提供的盘口异动提示。金额及数量仅在源四字段通过数量×价格一致性校验时展示，未通过校验的字段保持空缺。</p><h3>来源原始信息</h3><p>${esc(r.raw_info||"源未提供额外描述")}</p><h3>股票与关联信息</h3>${chips("stock",[r.code])}${sourceLink(r)}`;
    if(kind==="news")return `<p class="st-detail-note">${esc(r.published_at||"源未给完整发表时间")} · ${esc(r.source)}<br>${esc(r.time_basis)}；由 ${esc(r.sources.join(" / "))} 取得并按标题去重。</p><h3>${r.is_core?"当日核心消息":"短线快讯"} · ${esc(r.event_type||r.category)}</h3><p>${esc(r.classification_basis||"来源线索，需继续核验")}</p><p>${esc(r.impact_note||r.core_exclusion||"")}</p><h3>消息内容</h3><p>${esc(r.summary)}</p><h3>后续观察</h3><p>${esc(r.watch_points||"核对原文披露和实际行情反馈。")}</p><h3>涉及板块 · 关键词关联</h3>${chips("sector",r.sector_ids)}<h3>原文直接提及的股票</h3>${chips("stock",r.stock_codes)}<h3>如何核验</h3><p>${esc(r.association_basis)}。请核对消息原文、披露主体、发布时间与后续公告，再对照板块成交、资金和持续性。</p><p>当前接口提供快讯摘要，完整内容以来源原文为准；未补写来源没有给出的细节。</p>${sourceLink(r)}`;
    return `${metrics([["开始日期",r.date],["结束日期",r.end_date],["倒计时",r.days_until?`${r.days_until}天`:r.state]])}<h3>事件与安排</h3><p>${esc(r.summary||r.title)}</p><p>地点 / 国家：${esc(r.location||r.country||"源未提供")}<br>类别：${esc(r.category)}<br>时间：${esc(r.time||"未提供具体时刻")}<br>时区：${esc(r.timezone)}<br>核验：${r.verified?"官方已核验":"待官方核验"} · ${esc(r.verified_at||"未取得核验日")}<br>来源：${esc(r.source)}</p>${r.forecast||r.previous?metrics([["预期值",r.forecast||"—"],["前值",r.previous||"—"],["实际值",r.value||"尚未公布"]]):""}<h3>短线观察与验证点</h3><p>${esc(r.watch||"以主办方或发布机构的正式公告为准，核对实际业务与资金反馈。")}</p><h3>相关板块 · 题材关联</h3>${chips("sector",r.sector_ids)}<h3>原文直接提及的个股</h3>${chips("stock",r.stock_codes)}<p class="st-detail-note">日程存在变更可能。媒体预告与数据商日程未标记为官方确认；没有原文点名的股票，不视作参会或直接受益主体。</p>${sourceLink(r)}`;
  }
  function sourceLink(r){const url=r.url||r.source_url;return url?`<div class="st-detail-actions"><a href="${esc(url)}" target="_blank" rel="noopener noreferrer" data-st-external>${r.url?"查看来源原文 ↗":"查看来源主页（接口未给文章链接）↗"}</a></div>`:"";}
  function open(kind,id="",remember=true){
    if(!data)return;
    if(remember&&drawerState)backStack.push({...drawerState});
    drawerState={kind,id,page:1,keyword:""};renderDrawer();if(!$("stDrawer").open)$("stDrawer").showModal();syncAllScroll();
  }
  function renderDrawer(){
    const {kind,id}=drawerState;const list=kind.startsWith("list:");const listKind=list?kind.slice(5):"";
    const titles={big_buys:"大单买入监控 · 已采集记录",big_sells:"大笔卖出监控 · 已采集记录",sectors:"全部板块异动",stocks:"全部个股异动",news:"全部来源消息",short_news:"短线快讯流 · 已排除核心",core_news:"当日核心消息 · 重大事实研判",calendar:"完整未来催化日程",limits:"实际涨跌停记录",limit_up:"当日涨停股票 · "+(data.limit_as_of||"待更新"),limit_down:"当日跌停股票 · "+(data.limit_as_of||"待更新"),hot_stocks:"股票热度排行 · 原始人气名次",popularity_rises:`人气快速攀升 · 当前${$("stRankCeiling").value==="999999"?"全部已覆盖":"前"+$("stRankCeiling").value+"名"} · 上升≥${$("stRankGain").value}名`};
    $("stDrawerEyebrow").textContent=list?"FULL DATA / 可检索与导出":kind==="sources"?"SOURCE / 数据时点与接口状态":"EVIDENCE / 关联记录与原始依据";
    $("stDrawerTitle").textContent=list?titles[listKind]:kind==="sources"?"所有数据来源":kind==="method"?"口径、覆盖与缺失":kind==="signal"?`${maps.sector.get(id.slice(0,id.lastIndexOf("|")))?.name||"板块"} · ${id.slice(id.lastIndexOf("|")+1)}依据`:maps[kind]?.get(id)?.name||maps[kind]?.get(id)?.title||"完整档案";
    let body;
    if(list)body=renderList(listKind);
    else if(kind==="sources")body=`<p>${esc(data.method)}</p>${(data.sources||[]).map(s=>`<div class="st-source-row"><strong>${esc(sourceNames[s.key]||s.key)}</strong><div>${esc(s.state)}<small>成功抓取 ${esc(s.fetched_at||"未取得")}<br>最近尝试 ${esc(s.attempted_at||s.fetched_at||"未取得")}<br>${esc(s.error||"")}</small></div></div>`).join("")||'<p>尚无独立更新记录，当前显示已有本地快照和已核验日程基线。</p>'}<h3>待补事项</h3><p>${esc(data.notes.join("\n")||"当前没有来源错误记录。")}</p>`;
    else if(kind==="method")body=`<button type="button" data-st-sources>查看各来源状态与实际时点 →</button><p>${esc(data.method)}</p><div class="st-checklist"><div><strong>异动与资金</strong><p>异动、成交报价、板块资金各自保留源数据日；新抓取时间不会替代行情日期。盘口活跃度是描述刻度，未识别实际交易者。</p></div><div><strong>封板</strong><p>使用实际涨跌停价核对未复权收盘价。快速更新最多衔接前六个收盘日，左端仍连续或缺日时保留“至少”下界。盘中触及与收盘封板分别展示。</p></div><div><strong>消息</strong><p>多个来源按标题去重，核心消息必须具有当日完整日期，并符合重大政策、产业实质变化或公司重大披露条件，排除预测和评论。短线快讯按时间排列，排除已入选的核心消息；只有时分的消息不会冒充当日核心。</p></div><div><strong>未来日程</strong><p>官方核验会议、数据商日历、从媒体标题提取的未来会议预告分别标识。时区未明确的记录不猜测北京时间；过期会议自动退出未来列表。</p></div><div><strong>关联网络与矩阵</strong><p>实线表示已取得的行业/历史成分归属，光束只是这些关系的可视化。消息与题材匹配不代表因果。矩阵统计覆盖样本内的数量，不推算整个板块真实买卖户数。</p></div></div>`;
    else body=kind==="signal"?renderSignal(id):renderDetail(kind,id);
    $("stDrawerBody").innerHTML=`${backStack.length?'<button type="button" data-st-back>← 返回上级明细</button><br><br>':""}${body}`;
    if(list)$("stDetailSearch").addEventListener("input",event=>{const position=event.target.selectionStart;drawerState.keyword=event.target.value.toLowerCase();drawerState.page=1;renderDrawer();$("stDetailSearch").focus();$("stDetailSearch").setSelectionRange(position,position);});
  }
  function download(content,name,type){const url=URL.createObjectURL(new Blob([content],{type})),a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function exportList(){const kind=drawerState.kind.slice(5),rows=rowsFor(kind).filter(r=>matches(r,drawerState.keyword));const keys=kind==="sectors"?["name","code","type","date","pct","net","observation"]:kind==="stocks"?["name","code","date","price","change_pct","direction","activity","changes_total","main_net_inflow"]:kind==="calendar"?["title","date","end_date","time","timezone","verified","verified_at","source","url","summary","watch"]:["limits","limit_up","limit_down"].includes(kind)?["name","code","date","kind","streak","streak_lower_bound","fall_days","close","pct","up_limit","down_limit","first_time","last_time","open_times","fd_amount"]:kind==="hot_stocks"?["hot_rank","hot_rank_before","hot_rank_change","hot_rank_period","name","code","hot_source","hot_fetched_at"]:kind==="popularity_rises"?["name","code","hot_rank_before","hot_rank","hot_rank_change","hot_rank_period","hot_fetched_at","hot_source"]:["big_buys","big_sells"].includes(kind)?["occurred_at","time","date","code","name","label","amount","price","volume","change_pct","raw_info","source","date_basis","amount_basis"]:["title","published_at","date","time_basis","source","url","summary"];
    if(kind==="sectors")keys.push("rank","trading_heat","heat_date","heat_coverage","display_pct","pct_label","pct_at","display_net","net_label","net_at","coverage","quote_note");
    const exportRows=kind==="sectors"?rows.map((r,index)=>{const q=sectorQuote(r);return {...r,rank:index+1,trading_heat:sectorHeat(r),display_pct:q.pct,display_net:q.net,pct_label:q.pct_label,pct_at:q.pct_at,net_label:q.net_label,net_at:q.net_at,coverage:JSON.stringify(q.coverage||{}),quote_note:q.note};}):rows;
    const cell=value=>{let s=String(value??"");if(/^[=+\-@]/.test(s)&&!/^[-+]?\d+(\.\d+)?$/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};download("\uFEFF"+[keys,...exportRows.map(r=>keys.map(k=>r[k]))].map(r=>r.map(cell).join(",")).join("\r\n"),`短线大屏_${kind}_${data.today}.csv`,"text/csv;charset=utf-8");}
  function liveStatus(){const historical=data?.historical;$("stLiveState").textContent=historical?"历史快照 · 自动滚动":"今日实时监控 · 分区独立更新";$("stLiveState").title="盘口15秒 · 个股异动1分钟 · 消息45秒 · 人气3分钟 · 板块资金2分钟 · 盘中涨跌停90秒；首屏立即错峰连接，旧日记录仅留在历史查询";}
  function stopFeeds(){for(const state of feedStates.values())clearTimeout(state.timer);feedStates.clear();fieldRevisions.clear();for(const clock of root.querySelectorAll(".st-feed-clock"))clock.remove();}
  function markFeed(kind,seconds,value={}){const hosts={radar:["stStockList"],news:["stNews"],popularity:["stHotList","stRapidList"],sectors:[],limits:["stUpList","stDownList","stEmotion"],orders:["stBuyList","stSellList"]};if(kind===(sectorSort()==="heat"?"radar":"sectors"))hosts[kind].push("stSectorList");for(const id of hosts[kind]||[]){const header=$(id).closest(".st-panel")?.querySelector("header");if(!header)continue;let clock=header.querySelector(".st-feed-clock");if(!clock){clock=document.createElement("small");clock.className="st-feed-clock";header.querySelector("h2").after(clock);}clock.textContent=seconds>=60?`${seconds/60}分`:`${seconds}秒`;clock.dataset.state=value.error?"error":value.running?"loading":"ready";clock.title=`每${seconds}秒独立检查${value.error?" · "+value.error:value.running?" · 后台采集中":""}${value.checked_at?" · 最近成功检查 "+value.checked_at:""}；行情日期以分区记录为准`;}}
  function startFeeds(){if(data?.historical||disposed)return;for(const [kind,[interval,offset]] of Object.entries(feedConfig))if(!feedStates.has(kind)){const state={version:"",busy:false,timer:null};feedStates.set(kind,state);markFeed(kind,interval/1000);state.timer=setTimeout(()=>loadFeed(kind,state,interval),offset);}}
  async function loadFeed(kind,state,interval){
    if(disposed||feedStates.get(kind)!==state||data?.historical)return;
    if(document.hidden||state.busy){state.timer=setTimeout(()=>loadFeed(kind,state,interval),interval);return;}
    state.busy=true;let delay=interval;const selected=$("stDate").value;
    try{const value=await api("/api/short-term/feed?"+new URLSearchParams({kind,after:state.version}));if(disposed||feedStates.get(kind)!==state||selected!==$("stDate").value)return;
      if(value.changed&&value.payload&&data){const changed=[];for(const [key,content] of Object.entries(value.payload))if((fieldRevisions.get(key)||0)<=(value.revision||0)){data[key]=content;fieldRevisions.set(key,value.revision||0);changed.push(key);}state.version=value.version;if(changed.length)applyIncoming(()=>render(changed,kind));}
      if(value.running)delay=3000;state.error=value.error||"";
      markFeed(kind,interval/1000,value);
    }catch(e){state.error="自动重试中";}
    finally{state.busy=false;if(!disposed&&feedStates.get(kind)===state)state.timer=setTimeout(()=>loadFeed(kind,state,interval),delay);}
  }
  async function load(){const id=++loadSequence;try{const value=await api("/api/short-term/overview?"+new URLSearchParams({date:$("stDate").value}));if(disposed||id!==loadSequence)return;data=value;error();render();liveStatus();loadOrders();startFeeds();}catch(e){if(id===loadSequence&&!disposed){error(e.name==="AbortError"?"读取超时，系统将自动重试；已有数据保留":e.message);root.setAttribute("aria-busy","false");setTimeout(()=>id===loadSequence&&!disposed&&load(),15000);}}}
  async function loadOrders(){
    if(disposed||orderBusy)return;clearTimeout(orderTimer);if(document.hidden){orderTimer=setTimeout(loadOrders,15000);return;}orderBusy=true;const id=++orderSequence,selected=$("stDate").value;let delay=15000;
    try{const value=await api("/api/short-term/orders?"+new URLSearchParams({date:selected}));if(id!==orderSequence||selected!==$("stDate").value)return;
      if(data){data.big_buys=value.big_buys||[];data.big_sells=value.big_sells||[];data.order_meta=value.order_meta||{};if(orderHydrated)applyIncoming(()=>renderOrders());else renderOrders();orderHydrated=true;}
      if(value.refreshing)delay=2000;
      if(value.error)$("stLiveState").textContent="盘口连接重试中 · 保留原记录";
      if(!data?.historical)markFeed("orders",15,{running:value.refreshing,error:value.error||value.order_meta?.buy?.error||value.order_meta?.sell?.error});
    }catch(e){$("stLiveState").textContent="盘口连接重试中 · 保留原记录";}
    finally{orderBusy=false;if(!disposed&&!data?.historical)orderTimer=setTimeout(loadOrders,delay);}
  }
  async function kline(code,buttonElement){buttonElement.disabled=true;const host=$("stKline");host.innerHTML='<p>正在读取实际日K…</p>';try{const value=await api(`/api/stock-kline/${encodeURIComponent(code)}?period=daily&limit=120`);if(drawerState?.kind!=="stock"||drawerState.id!==code)return;const payload=value.data||{},rows=(payload.records||[]).filter(r=>r.close!=null&&(!data.historical||String(r.date||r.day||"").slice(0,10)<=data.today));if(rows.length<2){host.innerHTML='<p>该快照没有足够的可用日线。</p>';return;}const lo=Math.min(...rows.map(r=>Number(r.low||r.close))),hi=Math.max(...rows.map(r=>Number(r.high||r.close))),span=hi-lo||1;host.innerHTML=`<p>来源 ${esc(payload.source||"未给出")} · ${rows.length}根日K · ${esc(rows[0].date||rows[0].day||"")} 至 ${esc(rows.at(-1).date||rows.at(-1).day||"")}</p><svg viewBox="0 0 680 230" role="img" aria-label="${esc(code)}收盘价格走势" style="width:100%;background:#070b11"><path d="${rows.map((r,i)=>`${i?"L":"M"}${20+i*640/(rows.length-1)},${200-(Number(r.close)-lo)/span*180}`).join(" ")}" fill="none" stroke="#a9c6d0" stroke-width="1.6"/><text x="12" y="17" fill="#9aa7b4" font-size="11">${n(hi,2)}</text><text x="12" y="222" fill="#9aa7b4" font-size="11">${n(lo,2)}</text></svg>`;}catch(e){if(drawerState?.id===code)host.innerHTML=`<p class="st-detail-note">${esc(e.message)}</p>`;}finally{buttonElement.disabled=false;}}
  document.addEventListener("click",event=>{
    const entity=event.target.closest("[data-st-kind],[data-st-copy-kind]");if(entity)open(entity.dataset.stKind||entity.dataset.stCopyKind,entity.dataset.stId||entity.dataset.stCopyId);
    const list=event.target.closest("[data-st-list]");if(list)open("list:"+list.dataset.stList);
    const page=event.target.closest("[data-st-page]");if(page&&drawerState){drawerState.page=Number(page.dataset.stPage);renderDrawer();}
    if(event.target.closest("[data-st-back]")){drawerState=backStack.pop();renderDrawer();}
    if(event.target.closest("[data-st-export-list]"))exportList();
    if(event.target.closest("[data-st-sources]"))open("sources");
    const k=event.target.closest("[data-st-kline]");if(k)kline(k.dataset.stKline,k);
    const external=event.target.closest("[data-st-external]");if(external&&document.body.dataset.desktopMode==="1"){event.preventDefault();api("/api/open-url",{method:"POST",body:JSON.stringify({url:external.href})}).catch(e=>error(e.message));}
  });
  $("stDrawerClose").addEventListener("click",()=>$("stDrawer").close());$("stDrawer").addEventListener("close",()=>{drawerState=null;backStack=[];syncAllScroll();});
  $("stRankCeiling").addEventListener("change",()=>data&&render());$("stRankGain").addEventListener("change",()=>data&&render());
  for(const select of root.querySelectorAll("[data-st-sector-sort]")){select.title="交易热度按同日覆盖成分股雷达活跃度平均值；资金按展示净流入。最新数据日优先、缺值置后，两处同步排名。";select.addEventListener("change",()=>{for(const other of root.querySelectorAll("[data-st-sector-sort]"))other.value=select.value;if(!data)return;render(["sectors"],"ranking");const kind=sectorSort()==="heat"?"radar":"sectors";if(!data.historical)markFeed(kind,feedConfig[kind][0]/1000,{error:feedStates.get(kind)?.error});if(drawerState?.kind==="list:sectors")renderDrawer();});}
  $("stMethod").addEventListener("click",()=>open("method"));
  let searchTimer;$("stSearch").addEventListener("input",()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>data&&render(),120);});
  function changeDate(){loadSequence++;orderSequence++;stopFeeds();clearArrivals();orderHydrated=false;data=null;graphKey="";$("stDrawer").close();load();}
  $("stDate").addEventListener("change",changeDate);$("stLatest").addEventListener("click",()=>{$("stDate").value="";changeDate();});
  $("stMotion").addEventListener("click",()=>{scene.setMotion(!scene.motion);root.dataset.motion=scene.motion?"on":"off";if(!scene.motion)clearArrivals();$("stMotion").setAttribute("aria-pressed",String(scene.motion));$("stMotion").textContent=scene.motion?"动效开":"动效关";});
  $("stFullscreen").addEventListener("click",()=>{if(parent!==window)parent.postMessage({type:"lumo-short-term-fullscreen"},location.origin);else if(document.fullscreenElement)document.exitFullscreen().catch(e=>error(e.message));else root.requestFullscreen?.().catch(()=>error("浏览器未允许原生全屏，当前仍为铺满窗口的大屏。"));});
  $("stReturn").addEventListener("click",()=>{if(parent!==window)parent.postMessage({type:"lumo-short-term-return"},location.origin);else location.href="/desktop/features";});
  $("stScrollToggle").addEventListener("click",()=>{scrolling=!scrolling;$("stScrollToggle").setAttribute("aria-pressed",String(scrolling));$("stScrollToggle").textContent=scrolling?"暂停滚动":"继续滚动";syncAllScroll();});
  root.addEventListener("wheel",event=>{const host=event.target.closest(".st-scroll"),state=host&&scrollStates.get(host);if(state){state.manual=performance.now()+8000;syncScroll(host,state);clearTimeout(state.manualTimer);state.manualTimer=setTimeout(()=>syncScroll(host,state),8100);}},{passive:true});
  document.addEventListener("visibilitychange",()=>{syncAllScroll();if(!document.hidden){if(!data)load();else loadOrders();}});
  let sceneResizeTimer;const sceneLayoutObserver=new ResizeObserver(()=>{clearTimeout(sceneResizeTimer);if(data&&!disposed&&JSON.stringify(sceneCapacity())!==sceneLayoutKey)sceneResizeTimer=setTimeout(()=>{if(data&&!disposed)render(["graph"],"scene");},140);});sceneLayoutObserver.observe($("stScene"));
  setupScroll();load();
  window.addEventListener("pagehide",()=>{disposed=true;sceneLayoutObserver.disconnect();stopFeeds();clearArrivals();clearTimeout(sceneResizeTimer);clearTimeout(orderTimer);clearTimeout(searchTimer);for(const state of scrollStates.values()){state.animation?.cancel();state.observer.disconnect();clearTimeout(state.manualTimer);}++loadSequence;++orderSequence;scene.destroy();});
})();
