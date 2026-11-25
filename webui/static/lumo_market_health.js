/* 收盘体检：评分统一来自后端；日期、缺失与确认状态直接展示。 */
(() => {
  "use strict";
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const finite=v=>typeof v==="number"&&Number.isFinite(v);
  const num=(v,d=1)=>finite(v)?v.toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d}):"—";
  const pct=v=>finite(v)?`${num(v)}%`:"—";
  const signed=(v,unit="%")=>finite(v)?`${v>0?"+":""}${num(v,2)}${unit}`:"—";
  const trendKeys=[["above_ma20","站上MA20"],["above_ma60","站上MA60"],["above_ma250","站上MA250"],["newlow60","创60日收盘新低"],["newhigh60","创60日收盘新高"]];
  const stamp=v=>`${v.as_of||"未取得日期"} · ${v.status==="missing"?"缺少观测":v.status==="stale"?v.provisional?"历史盘中":"历史收盘":v.provisional?"盘中观测":"收盘观测"}`;
  const provenance=v=>[v.captured_at?`采集 ${window.LumoDate?.format(v.captured_at)||v.captured_at}`:"",v.source].filter(Boolean).join(" · ");
  function report(payload){
    if(payload.breadth_valuation)return payload.breadth_valuation;
    const b=payload.breadth||{},market=payload.market||{},m=market.values||{};
    const status=(date,exists)=>!exists?"missing":date===payload.as_of?"current":"stale";
    return {as_of:payload.as_of,provisional:payload.provisional,summary:{title:"看清市场参与、趋势与估值覆盖",text:"各部分保留自己的观测日期；不同样本仅作交叉观察。"},
      participation:{as_of:market.as_of,captured_at:market.captured_at,source:market.source,provisional:market.provisional,status:status(market.as_of,finite(m.change_sample)),sample:m.change_sample,up:m.up,down:m.down,flat:m.flat,up_ratio:m.up_ratio,net_advances:finite(m.up)&&finite(m.down)?m.up-m.down:null,median_return:m.chg_median,weighted_return:m.mv_weighted_chg,label:"实际涨跌分布",note:"上涨占比以有效涨跌幅样本为分母，包含平盘。"},
      trend:{as_of:b.as_of,captured_at:b.captured_at,source:b.source,provisional:b.provisional,status:status(b.as_of,finite(b.sample)),sample:b.sample,cohort:b.cohort,method:b.method,label:"抽样趋势观察",note:b.note,comparison_note:"此快照未保存可比变化，不按混合样本推算。",rows:trendKeys.map(([key,label])=>({key,label,value:b[key],denominator:b.denominators?.[key],change_pp:null,comparable:false}))},
      valuation:{as_of:market.as_of,captured_at:market.captured_at,source:market.source,provisional:market.provisional,status:status(market.as_of,finite(m.total)),total:m.total,pb:{median:m.pb_median,sample:m.pb_sample,distribution:null},pe:{median:m.pe_median,sample:m.pe_sample,distribution:null},below_pb1_ratio:m.below_pb1_ratio,note:"仅统计正值有效样本，不等于交易所加权平均估值。"},notes:[]};
  }
  function overview(r){
    const p=r.participation||{},t=r.trend||{},v=r.valuation||{};
    const ma20=t.rows?.find(row=>row.key==="above_ma20");
    const card=(key,title,label,value,note,part)=>`<article class="mh-breadth-summary" data-mh-breadth-summary="${key}"><span>${esc(title)}</span><h4>${esc(label||"待补证据")}</h4><strong>${esc(value)}</strong><small>${esc(note)}</small><small class="mh-breadth-stamp">${esc(stamp(part))}</small>${provenance(part)?`<small>${esc(provenance(part))}</small>`:""}</article>`;
    return `<span class="mh-eyebrow">市场结构 / 参与 · 趋势 · 估值</span><h3>${esc(r.summary?.title||"广度与估值观察")}</h3><p class="mh-note">${esc(r.summary?.text||"")} ${r.provisional?"盘中结果仍会变化，以收盘确认作跨日判断。":"收盘观测按各部分实际数据日展示。"}</p><div class="mh-breadth-summary-grid">${card("participation","市场参与度",p.label,`上涨 ${pct(p.up_ratio)}`,`涨 / 跌 / 平 ${num(p.up,0)} / ${num(p.down,0)} / ${num(p.flat,0)} · 有效 ${num(p.sample,0)} 只`,p)}${card("trend","趋势广度",t.label,`MA20上方 ${pct(ma20?.value)}`,`有效分母 ${num(ma20?.denominator,0)} 只 · ${t.comparison_note||"保留实际采样口径"}`,t)}${card("valuation","估值覆盖","正值样本的横截面",`PB ${num(v.pb?.sample,0)} / PE ${num(v.pe?.sample,0)}`,`行情样本 ${num(v.total,0)} 只 · 中位数不能代表全部股票`,v)}</div><p class="mh-note">${esc(p.note||"")}</p>`;
  }
  function trend(r){
    const t=r.trend||{};
    return `<div class="mh-box-head"><div><h3>价格广度</h3><p>${esc(stamp(t))} · 抽样 ${num(t.sample,0)} 只</p><p>${esc(provenance(t))}</p></div></div><div class="mh-breadth-bars">${(t.rows||[]).map(row=>{
      const valid=finite(row.value)&&row.value>=0&&row.value<=100;
      const compare=row.comparable&&finite(row.change_pp)?`较 ${t.previous_date||"可比收盘"} ${signed(row.change_pp," 个百分点")}`:"暂无可比收盘变化";
      return `<div class="mh-breadth-bar" data-mh-breadth-key="${esc(row.key)}"><div class="mh-breadth-bar-label"><span>${esc(row.label)}</span><strong>${valid?pct(row.value):"—"}</strong></div><div class="mh-bar-track${valid?"":" mh-breadth-missing"}" ${valid?`style="--position:${row.value}%"`:'aria-label="数据缺失，不按零显示"'}>${valid?"<i></i>":""}</div><small>实际分母 ${num(row.denominator,0)} 只 · ${esc(compare)}</small></div>`;
    }).join("")}</div><p class="mh-note">${esc(t.comparison_note||"")}</p><p class="mh-note">${esc(t.note||"")} 采样方法 ${esc(t.method||"未记录")} · 样本组 ${esc(t.cohort||"未记录")}</p>`;
  }
  function valuation(r,key="pb"){
    key=key==="pe"?"pe":"pb";
    const v=r.valuation||{},metric=v[key]||{},d=metric.distribution,label=key.toUpperCase();
    const buttons=["pb","pe"].map(k=>`<button type="button" data-mh-valuation="${k}" aria-pressed="${key===k}">${k.toUpperCase()} 分布</button>`).join("");
    const header=`<div class="mh-box-head"><div><h3>估值与交易结构</h3><p>${esc(stamp(v))} · 行情样本 ${num(v.total,0)} 只</p><p>${esc(provenance(v))}</p></div><div class="mh-segment" aria-label="估值指标切换">${buttons}</div></div><div class="mh-valuation-kpis"><div><span>正 ${label} 中位数</span><strong>${num(metric.median,key==="pb"?3:2)} <small>倍</small></strong></div><div><span>有效正值样本</span><strong>${num(metric.sample,0)} <small>只</small></strong></div><div><span>正值覆盖率</span><strong>${pct(metric.coverage_pct??d?.coverage_pct)}</strong></div></div>`;
    const quantiles=d?`<p class="mh-note">横截面分位（非历史分位）</p><div class="mh-valuation-quantiles" aria-label="${label}分位数">${["p10","p25","p50","p75","p90"].map(q=>`<div><span>${q.toUpperCase()}</span><strong>${num(d.quantiles?.[q],key==="pb"?3:2)}</strong><small>倍</small></div>`).join("")}</div>`:"";
    const histogram=d?`<div class="mh-valuation-buckets" aria-label="${label}正值样本分布">${(d.buckets||[]).map(bucket=>{
      const valid=finite(bucket.pct)&&bucket.pct>=0&&bucket.pct<=100;
      return `<div class="mh-valuation-bucket"><span>${esc(bucket.label)}</span><div class="mh-bar-track${valid?"":" mh-breadth-missing"}" ${valid?`style="--position:${bucket.pct}%"`:'aria-label="分桶占比缺失"'}>${valid?"<i></i>":""}</div><strong>${num(bucket.count,0)} <small>只 · ${pct(bucket.pct)}</small></strong></div>`;
    }).join("")}</div><p class="mh-note">分布分母 ${num(d.sample,0)} 个正值 · 非正值 ${num(d.nonpositive,0)} 个 · 缺失 ${num(d.missing,0)} 个。分桶占比以正值样本为分母；覆盖率以行情样本为分母。区间左闭右开，首档仅包含大于零的值。</p>`:'<p class="mh-empty mh-valuation-empty">此快照未保存估值分布；保留已有中位数，更新完整统计后查看分位与分桶。</p>';
    return header+quantiles+histogram+`<p class="mh-note">正PB样本破净率 ${pct(v.below_pb1_ratio)}。横截面分位描述同一快照内股票之间的分布，不等于历史分位或未来收益判断。${esc(v.note||"")}</p>`;
  }
  function recordRows(observations,t,scope){
    const rows=scope==="all"?observations:observations.filter(row=>t.cohort&&t.method&&row.cohort===t.cohort&&row.method===t.method);
    return [...rows].sort((a,b)=>String(b.as_of||"").localeCompare(String(a.as_of||"")));
  }
  function records(payload,r,scope="cohort"){
    const t=r.trend||{},all=payload.observations||[],rows=recordRows(all,t,scope);
    const buttons=[["cohort","当前同组"],["all","全部记录"]].map(([key,label])=>`<button type="button" data-mh-breadth-scope="${key}" aria-pressed="${scope===key}">${label}</button>`).join("");
    const note=scope==="all"?"全部记录可能包含不同样本组、方法与盘中观测，不可直接连比。":t.cohort&&t.method?`当前组 ${t.cohort} · ${t.method}；同组仍需核验各项分母，盘中观测不代替收盘。`:"当前快照未保存样本组与方法，无法确认同组记录；可切换全部记录查看历史。";
    return `<div class="mh-box-head"><div><h3>广度观测记录</h3><p>显示 ${rows.length} / ${all.length} 条 · 完整更新回算最近5个交易日</p></div><div class="mh-segment" aria-label="广度记录样本范围">${buttons}</div></div><p class="mh-note mh-breadth-record-note">${esc(note)}</p><div class="mh-table-wrap"><table><thead><tr><th>日期 / 时点</th><th>样本</th><th>MA20上方</th><th>MA250上方</th><th>60日新低 / 新高</th><th>高点回撤中位</th><th>数据口径 / 组号</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(row.as_of)}<small>${row.provisional?"盘中观测":"收盘观测"}</small></td><td>${num(row.sample,0)}</td><td>${pct(row.above_ma20)}<small>分母 ${num(row.denominators?.above_ma20,0)}</small></td><td>${pct(row.above_ma250)}<small>分母 ${num(row.denominators?.above_ma250,0)}</small></td><td>${pct(row.newlow60)} / ${pct(row.newhigh60)}</td><td>${signed(row.dd52_median)}</td><td>${esc(row.method)}<small>${esc(row.cohort||"未记录")}${row.backfilled?" · 历史回算":""}</small></td></tr>`).join("")||`<tr><td colspan="7">${scope==="all"?"暂无观测记录":"暂无可确认的同组记录"}</td></tr>`}</tbody></table></div>`;
  }
  window.LumoBreadthView={report,overview,trend,valuation,records,recordRows};
})();
(() => {
  "use strict";
  const root = document.getElementById("marketHealthRoot");
  if (!root) return;
  const chartDate = window.LumoMarketChartDate;
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const dateText = v => window.LumoDate?.format(v) || String(v ?? "");
  const dateOnly = v => window.LumoDate?.date(v) || String(v ?? "");
  const textDates = v => window.LumoDate?.formatText(v) || String(v ?? "");
  const num = (v, d = 1) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN", {minimumFractionDigits:d, maximumFractionDigits:d});
  const pct = v => v == null ? "—" : `${num(v)}%`;
  const breadthPct = v => v == null ? "—" : `${num(v,2)}%`;
  const signed = v => v == null ? "—" : `${v > 0 ? "+" : ""}${num(v)}%`;
  const turnover = v => v == null ? "—" : `${num(v/1e8,2)} 亿元`;
  const pp = v => v == null ? "—" : `${v > 0 ? "+" : ""}${num(v,2)} 个百分点`;
  const countFields = [["上涨家数","up"],["下跌家数","down"],["平盘家数","flat"]];
  const tone = v => v == null || v === 0 ? "mh-neutral" : v > 0 ? "mh-positive" : "mh-negative";
  const badge = (text, cls = "") => `<span class="mh-badge ${cls}">${esc(text)}</span>`;
  const stat = (label, value, note, valueClass = "") => `<div class="mh-kpi"><span>${esc(label)}</span><strong class="${esc(valueClass)}">${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const q = new URLSearchParams(location.search);
  const tabs = [...$("mhTabs").querySelectorAll("[data-mh-tab]")].map(b => b.dataset.mhTab);
  let section = tabs.includes(q.get("section")) ? q.get("section") : "overview";
  let symbol = q.get("index") || "sh000001";
  const indexWindow = {window:['60','120','250','custom'].includes(q.get('window')) ? q.get('window') : 'all', from:q.get('indexFrom'), to:q.get('indexTo')};
  const iceWindow = {window:'all'};
  const breadthView={valuation:"pb",scope:"cohort",details:{}},breadthRenderer=window.LumoBreadthView;
  const dailyStart = q.get("dailyStart") || "", dailyEnd = q.get("dailyEnd") || "";
  const daily = {draft:{start:dailyStart,end:dailyEnd}, query:dailyStart ? {start:dailyStart,end:dailyEnd} : null,
    report:null, selected:"", status:"", loading:false, requestId:0, backfill:null, autoFill:null};
  let payload = null, envelope = null, pollTimer = null, liveTimer = null, requestId = 0, ruleFilter = "all", ruleSearch = "", horizon = 60, autoSession = null;
  let viewMode=q.get("view") === "close" ? "close" : "latest";
  let autoStructure = false, lastStructureRequest = 0, autoInternalsSession = null;
  let closeSession = null, lastCloseRequest = 0, refreshing = false;

  function error(message = "") { $("mhError").hidden = !message; $("mhError").textContent = message; }
  function remember() {
    const next = new URL(location.href);
    next.searchParams.set("section", section);
    next.searchParams.set("index", symbol);
    next.searchParams.set("window", indexWindow.window);
    for (const [k,v] of [['indexFrom',indexWindow.from],['indexTo',indexWindow.to]]) {
      if (indexWindow.window==='custom' && v) next.searchParams.set(k,v); else next.searchParams.delete(k);
    }
    next.searchParams.set("view",viewMode);
    for (const [key,value] of [["dailyStart",daily.query?.start],["dailyEnd",daily.query?.end]]) {
      if (value) next.searchParams.set(key,value); else next.searchParams.delete(key);
    }
    if (section === "correlation") next.searchParams.set("networkSection", corrSection);
    history.replaceState(null, "", next);
  }
  function selectTab(tab, focus = false) {
    section = tabs.includes(tab) ? tab : "overview";
    if (section !== "correlation") restoreCorrelationSection();
    for (const key of tabs) {
      const button = $(`mhTab-${key}`), active = key === section;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      $(`mhView-${key}`).hidden = !active && !(section === "correlation" && key === corrSection);
      if (active && focus) button.focus();
    }
    if (section === "correlation") {
      if (!corrOverview) {
        corrOverview = new window.LumoMarketCorrelationScene($("corrOverview"));
        corrOverview.setSections(corrGroups, showCorrelationSection, corrSection);
        updateCorrelationSummaries();
      }
      if (corrSection === "correlation" && !corrSymbol) loadCorrelation();
      else if (corrSection !== "correlation" && !corrSectionHome) mountCorrelationSection();
    }
    remember();
    if (section === "breadth" && daily.query && !daily.report && !daily.loading && !daily.status) searchDaily(daily.query);
  }
  function boxHead(title, description = "", extra = "") {
    return `<div class="mh-box-head"><div><h3>${esc(title)}</h3>${description ? `<p>${esc(description)}</p>` : ""}</div>${extra}</div>`;
  }
  function dateState(date, dataAt="", provisional=false) {
    return date && date === payload.as_of ? badge(`${dateText(dataAt || date)} · ${provisional ? "盘中采样" : "同日"}`) : badge(date ? `${dateOnly(date)} · 非同日，不计分` : "待补数据", "warn");
  }
  function currentIndex(code) { return payload.indices.find(r => r.symbol === code) || {}; }
  function summaryReasons() {
    const sh = currentIndex("sh000001"), cy = currentIndex("sz399006");
    const market = payload.market?.values || {}, breadth = payload.breadth || {};
    return [
      ["趋势", `上证年线偏离 ${signed(sh.dev250)}，创业板 ${signed(cy.dev250)}；${sh.trend || "数据不足"}。`],
      ["广度", `MA250上方 ${pct(breadth.above_ma250)}，个股高点回撤中位 ${signed(breadth.dd52_median)}。${breadth.as_of !== payload.as_of ? "广度日期不同，未参与评分。" : `抽样 ${num(breadth.sample,0)} 只。`}`],
      ["估值", `正PB中位 ${num(market.pb_median,3)} 倍，破净率 ${pct(market.below_pb1_ratio)}。${payload.market?.as_of !== payload.as_of ? "日期不同，未参与评分。" : "超卖与低估需分别判断。"}`],
      ["确认", `底部修复 ${payload.confirmation.bottom.hit}/5，转弱信号 ${payload.confirmation.top.hit}/5；连续广度使用同组历史收盘观测。`],
    ];
  }
  function signalSummary(type) {
    const s = payload.signals[type], bottom = type === "bottom";
    const matched = s.rules.filter(r => r.hit === true);
    return `<article class="mh-box ${bottom ? "" : "mh-top"}">${boxHead(bottom ? "底部特征" : "顶部特征", "12项经验规则 · 缺失项单独列出", badge(s.known < 8 ? "覆盖不足" : "特征强度"))}
      <div class="mh-signal-score">${num(s.hit,0)}<span>/ ${s.total} 项${payload.provisional ? "当前满足" : "收盘触发"} · ${s.known} 项可判定</span></div>
      <p class="mh-note">${esc(payload.as_of)}价格极值曾触及 ${num(s.touched,0)}/${num(s.touch_known,0)} 项价格规则；当前/收盘状态与曾触及分开统计。</p>
      <div class="mh-score-track" style="--position:${s.score ?? 0}%"><i></i></div>
      <div class="mh-chips">${matched.slice(0,4).map(r => `<span class="mh-chip">${esc(r.label)}</span>`).join("") || `<small>${s.known ? "暂无规则触发" : "尚无同日可用证据"}</small>`}</div>
      <p class="mh-note">${bottom ? "超卖、缩量与悲观是底部特征；反转需要价格和广度共同修复。" : "普涨、估值扩张与加速是顶部特征；过热可持续，需等待转弱验证。"}</p>
      <p class="mh-note"><button type="button" class="mh-inline-link mh-select-index" data-mh-go="signals">查看完整评分卡 →</button></p></article>`;
  }
  function renderOverview() {
    const t = payload.temperature, b = payload.breadth || {}, m = payload.market?.values || {}, fusion=payload.fusion;
    const internals = payload.market_internals?.current;
    $("mhView-overview").innerHTML = `
      <div class="mh-hero-grid">
        <article class="mh-box mh-temperature">${boxHead("市场温度", "低值偏悲观，高值偏热", badge(`覆盖 ${num(t.coverage*100,0)}%`))}
          <div class="mh-temp-number">${num(t.score,0)}<span>/ 100</span></div><p class="mh-temp-label">${esc(t.label)}</p>
          <div class="mh-scale" style="--position:${t.score ?? 50}%"><i></i><i></i><i></i><i></i><i></i>${t.score == null ? "" : "<b></b>"}</div>
          <div class="mh-scale-labels"><span>极度悲观</span><span>中性</span><span>过热</span></div>
          <small>广度、动量、年线与估值共同计分。缺失权重超过40%时暂不输出温度。</small>
          <p><button type="button" class="mh-inline-link mh-select-index" data-mh-go="breadth">查询每日市场数据 →</button></p>
        </article>
        <article class="mh-box"><div class="mh-state-head"><span class="mh-eyebrow">七维综合研判</span>${badge(`证据覆盖 ${num((fusion?.coverage || 0)*100,0)}%`, fusion?.complete ? "" : "warn")}</div>
          <p class="mh-state-title">${esc(fusion?.title || payload.stage)}</p>
          <ul class="mh-state-list">${(fusion?.reasons || []).map((v,i) => `<li><b>${["价格状态","压力共振","低压力项","缺失维度"][i] || "观察"}</b><span>${esc(v)}</span></li>`).join("")}</ul>
          <p class="mh-note">综合压力 ${num(fusion?.pressure,0)}/100 · 指数/广度仅为部分证据，期指、两融、量化、资金共同形成结论。</p>
        </article>
      </div>
      <div class="mh-kpis">
        ${stat("MA250广度", pct(b.above_ma250), `${b.as_of || "未取得"} · 样本${num(b.sample,0)}只`)}
        ${stat("市场广度 / 阿姆斯指数", `${breadthPct(internals?.breadth_pct)} / ${num(internals?.trin,3)}`, internals ? `${dateOnly(internals.as_of)} · ${internalsStatus(internals)}` : "完整更新后获取沪深京 A 股数据")}
        ${stat("正PB中位数", `${num(m.pb_median,3)} 倍`, `破净率 ${pct(m.below_pb1_ratio)} · ${payload.market?.as_of || "未取得"}`)}
        ${stat("小盘－大盘20日收益差", signed(payload.style_spread), "中证1000减沪深300 · 正值小盘占优")}
      </div>
      <div class="mh-kpis" aria-label="沪深京 A 股市场成交">
        ${countFields.map(([label,key]) => stat(label, num(internals?.[key],0), `较上日收盘 ${signed(internals?.vs_previous?.[`${key}_pct`])}`)).join("")}
        ${stat(internals?.provisional ? "盘中累计成交额" : "每日成交额", turnover(internals?.turnover_yuan), turnoverNote(internals))}
      </div>
      <div class="mh-grid">${signalSummary("bottom")}${signalSummary("top")}</div>
      <article class="mh-box mh-space">${boxHead("七维证据矩阵", "按实际数据日期、覆盖与权重得出结果", '<button type="button" class="mh-inline-link mh-select-index" data-mh-go="cross">期货 / 两融 / 量化详情 →</button>')}${fusionTable()}</article>
      <article class="mh-box">${boxHead("七大指数全景", "当日涨跌、阶段回撤与年线偏离；点击指数进入趋势详情", `<button type="button" class="mh-inline-link mh-select-index" data-mh-go="indices">趋势详情 →</button>`)}${indexTable(false)}</article>
      ${payload.stale ? `<div class="mh-notice">当前快照距离今天已有 ${payload.age_days} 个自然日。请更新公开数据后判断最新市场状态。</div>` : ""}`;
  }
  function fusionTable() {
    const labels={current:"同日有效",lagged:"上一交易日 · 降权",stale:"日期不匹配 · 排除",unavailable:"待补数据"};
    return `<div class="mh-table-wrap"><table class="mh-fusion-table"><thead><tr><th>维度</th><th>证据日期</th><th>压力刻度</th><th>有效权重</th><th>数据状态</th></tr></thead><tbody>${(payload.fusion?.dimensions || []).map(d => `<tr><td><strong>${esc(d.label)}</strong><small>${esc(d.source)}</small></td><td>${esc(dateOnly(d.as_of) || "—")}</td><td>${num(d.score,0)} /100</td><td>${num(d.effective_weight*100)}% <small>设定 ${num(d.weight*100,0)}%</small></td><td>${badge(labels[d.status] || d.status,d.score == null ? "warn" : "")}</td></tr>`).join("")}</tbody></table></div>`;
  }
  function renderCross() {
    const f=payload.fusion, byKey=Object.fromEntries((f?.dimensions || []).map(d => [d.key,d]));
    const futures=byKey.futures?.details || {}, margin=byKey.leverage?.details || {}, quant=byKey.quant?.details || {}, capital=byKey.capital?.details || {};
    const latestMargin=(margin.series || []).at(-1) || {};
    const evidenceDate=key=>badge(`${dateOnly(byKey[key]?.as_of) || "—"} · ${byKey[key]?.status === "lagged" ? "上一交易日，已降权" : byKey[key]?.status === "current" ? "同日有效" : "日期无效，未计分"}`,byKey[key]?.score == null ? "warn" : "");
    const money=v=>v == null ? "—" : `${num(v/1e8,2)} 亿`;
    const blockNote=key=>(byKey[key]?.evidence || []).map(v=>`<p class="mh-note">${esc(v)}</p>`).join("")+(byKey[key]?.details?.error ? `<p class="mh-note">接口状态：${esc(byKey[key].details.error)}</p>` : "");
    $("mhView-cross").innerHTML=`<article class="mh-box mh-space">${boxHead(f?.title || "综合证据未齐", f?.method || "",badge(`压力 ${num(f?.pressure,0)}/100`))}${fusionTable()}
      ${(f?.conflicts || []).map(v=>`<div class="mh-notice">${esc(v)}</div>`).join("")}
      <p class="mh-note">压力高表示当前风险或出清压力强，压力低仅表示该维度压力较小，不能直接转换为见底概率或买入建议。缺失证据不会被置为0或50。</p></article>
      <article class="mh-box mh-space">${boxHead("股指期货 · 基差与前20席位", "IF / IH / IC / IM；主力按成交量选择",'<button type="button" class="mh-inline-link mh-select-index" data-mh-go="derivatives">多空概览与衍生品分析 →</button>')}
        <div class="mh-table-wrap"><table><thead><tr><th>品种 / 主力</th><th>合约交易日</th><th>同日核验升贴水</th><th>同合约较上日</th><th>持仓量</th></tr></thead><tbody>${(futures.basis || []).map(r=>`<tr><td>${esc(r.variety)} / ${esc(r.contract || "—")}<small>${esc(r.source || "")}</small></td><td>${esc(dateOnly(r.date) || "—")}</td><td class="${tone(r.basis_pct)}">${signed(r.basis_pct)}</td><td>${r.basis_change == null ? "历史接口未返回可比数据" : `${num(r.basis_change*100,1)} bp`}<small>对比 ${esc(dateOnly(r.previous_date) || "—")}</small></td><td>${num(r.position,0)}</td></tr>`).join("") || '<tr><td colspan="5">等待现有期货接口返回行情；点击跨市场分析更新。</td></tr>'}</tbody></table></div>
        <div class="mh-table-wrap"><table><thead><tr><th>品种 / 席位日</th><th>前20多单</th><th>前20空单</th><th>净多头持仓</th><th>净多头增减</th><th>多空变化</th></tr></thead><tbody>${(futures.positions || []).map(r=>`<tr><td>${esc(r.variety)} / ${esc(dateOnly(r.date) || "—")}</td><td>${num(r.long,0)}</td><td>${num(r.short,0)}</td><td>${num(r.net,0)}</td><td>${num(r.net_change,0)}</td><td>${esc(r.signal || "—")}</td></tr>`).join("") || '<tr><td colspan="6">尚无可用席位缓存；未披露时保留原交易日。</td></tr>'}</tbody></table></div>${blockNote("futures")}
        ${(futures.errors || []).map(v=>`<p class="mh-note">${esc(v)}</p>`).join("")}</article>
      <article class="mh-box mh-space">${boxHead("融资融券 · 杠杆周期", "沪深两所完整日合计 · 金额统一为元后展示亿元",evidenceDate("leverage"))}
        <div class="mh-kpis">${stat("融资余额",money(latestMargin.rzye),"融资杠杆存量")}${stat("融资净买入",money(latestMargin.net_buy),"融资买入额减偿还额")}${stat("融资余额5期变化",signed(margin.balance_change5),"六期完整观测的首尾比较")}${stat("融券余额",money(latestMargin.rqye),"单列观察，未机械计作看空")}</div>
        ${chart((margin.series || []).map(r=>({date:dateOnly(r.date),balance:r.rzye/1e8})),["balance"],"融资余额走势（亿元）")}
        <div class="mh-table-wrap"><table><thead><tr><th>日期</th><th>融资余额</th><th>买入</th><th>偿还</th><th>融资净买入</th><th>融券余额</th></tr></thead><tbody>${[...(margin.series || [])].slice(-6).reverse().map(r=>`<tr><td>${esc(dateOnly(r.date) || "—")}</td><td>${money(r.rzye)}</td><td>${money(r.rzmre)}</td><td>${money(r.rzche)}</td><td>${money(r.net_buy)}</td><td>${money(r.rqye)}</td></tr>`).join("") || '<tr><td colspan="6">等待现有TuShare客户端的两融权限与完整沪深数据。</td></tr>'}</tbody></table></div>${blockNote("leverage")}</article>
      <div class="mh-grid"><article class="mh-box">${boxHead("量化交易 · 方向与拥挤", quant.proxy ? "历史收盘行为代理 · 已按一半可靠度计入" : "复用量化雷达全量日榜，不用高频活跃度替代方向",'<a class="mh-inline-link" href="/desktop/capital_rankings">量化雷达 →</a>')}
        <dl class="mh-detail-list">${(quant.proxy ? [["价量资金有效交集",`${num(quant.sample,0)} 只`],["资金净流出占比",pct(quant.proxy.outflow_ratio)],["放量组 / 放量下跌流出",`${num(quant.proxy.active_sample,0)} / ${num(quant.proxy.selling_count,0)} 只`],["日内低位收盘占比",pct(quant.proxy.weak_close_ratio)]] : [["多向 / 空向异动",`${num(quant.bull,0)} / ${num(quant.bear,0)}`],["实际候选样本",num(quant.sample,0)],["疑似砸盘个股",num(quant.smash,0)],["多机制高压候选",num(quant.risk_count,0)]]).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
        ${(quant.sectors || []).length ? `<div class="mh-table-wrap"><table><thead><tr><th>板块</th><th>群体行为</th><th>活跃代理</th></tr></thead><tbody>${quant.sectors.map(r=>`<tr><td>${esc(r.industry)}</td><td>${esc(r.collective || "—")}</td><td>${num(r.activity)}</td></tr>`).join("")}</tbody></table></div>` : ""}${blockNote("quant")}</article>
      <article class="mh-box">${boxHead("主力资金 · 承接与流出", "使用大盘资金汇总，避免以Top榜代表全市场",evidenceDate("capital"))}
        <dl class="mh-detail-list"><div><dt>近5期主力净流入</dt><dd>${money(capital.net5)}</dd></div><div><dt>近5期净流入占比均值</dt><dd>${pct(capital.rate_mean5)}</dd></div></dl>
        ${chart((capital.series || []).map(r=>({date:dateOnly(r.trade_date),net:r.net_amount == null ? null : r.net_amount/1e8})),["net"],"大盘主力净流入（亿元）")}${blockNote("capital")}</article></div>`;
  }
  function indexTable(extended) {
    return `<div class="mh-table-wrap"><table><thead><tr><th>指数 / 日期</th><th>收盘</th><th>当日</th><th>20日</th><th>500日高点回撤</th><th>年线偏离</th><th>年量比</th>${extended ? "<th>RSI14</th><th>20日年化波动率</th><th>均线状态</th>" : ""}</tr></thead><tbody>
      ${payload.indices.map(r => `<tr class="${extended && r.symbol === symbol ? "mh-row-active" : ""}"><td><button type="button" data-mh-index="${esc(r.symbol)}" class="mh-select-index"><strong>${esc(r.name)}</strong><small>${esc(dateOnly(r.as_of) || "未取得")}${r.as_of && r.as_of !== payload.as_of ? " · 非同日" : ""}</small></button></td>
        <td>${num(r.close,2)}<small>${esc(dateText(r.quote_at || r.as_of))}${r.provisional ? " · 盘中" : " · 收盘"}</small></td><td class="${tone(r.ret1)}">${signed(r.ret1)}</td><td class="${tone(r.ret20)}">${signed(r.ret20)}</td><td class="${tone(r.drawdown)}">${signed(r.drawdown)}</td><td class="${tone(r.dev250)}">${signed(r.dev250)}</td><td>${r.provisional ? "收盘后确认" : `${num(r.volume250,2)}x`}</td>${extended ? `<td>${num(r.rsi14)}</td><td>${pct(r.volatility20)}</td><td>${esc(r.trend || "数据不足")}</td>` : ""}</tr>`).join("")}
      </tbody></table></div>`;
  }
  function ruleStatus(hit, provisional=false, near=false) {
    return hit === true ? `<span class="mh-dot yes">● <small>${provisional ? "当前满足" : "收盘触发"}</small></span>` : hit === false ? `<span class="mh-dot">○ <small>${provisional ? "当前未满足" : "收盘未触发"}${near ? " · 临界观察" : ""}</small></span>` : `<span class="mh-dot unknown">— <small>${provisional ? "本时段无有效观测" : "待补"}</small></span>`;
  }
  function ruleTable(type) {
    const all = payload.signals[type].rules;
    const filtered = all.filter(r => (ruleFilter === "all" || (ruleFilter === "hit" ? r.hit === true : ruleFilter === "touched" ? r.touched === true : ruleFilter === "miss" ? r.hit === false : r.hit == null)) && `${r.label} ${r.group} ${r.note}`.includes(ruleSearch));
    const value=r=>r.value == null ? "—" : `${num(r.value,r.unit === "日" ? 0 : r.near || Math.abs(r.value-r.threshold) < .01 ? 4 : r.unit === "倍" ? 3 : 2)}${esc(r.unit)}`;
    if (!filtered.length) return '<div class="mh-empty">没有符合筛选的指标</div>';
    const rows=filtered.map(r=>{
      const reclaimed=r.touched === true && r.hit === false ? `<small class="mh-touch">${payload.provisional ? "今日曾触发，当前已收回" : "当日曾触发，收盘未满足"}</small>` : "";
      const extreme=`<td>${r.touched == null ? "不适用价格极值" : `${r.touched ? "曾触及" : "未触及"} · ${value({...r,value:r.extreme_value})}`}<small>${r.extreme_price == null ? "按对应指标实际观测判断" : `${esc(r.extreme_kind)} ${num(r.extreme_price,2)}`}</small></td>`;
      const prior=payload.provisional ? `<td>${value({...r,value:r.closed_value})}<small>${esc(dateOnly(r.closed_as_of) || "—")} · ${r.closed_hit === true ? "收盘触发" : r.closed_hit === false ? "收盘未触发" : "无有效观测"}</small></td>` : "";
      const timing = r.timing_note || r.data_at || r.as_of || payload.as_of;
      return `<tr data-rule-key="${esc(r.key)}"><td>${ruleStatus(r.hit,payload.provisional,r.near)}${reclaimed}</td><td><strong>${esc(r.label)}</strong><small>${esc(r.group)} · ${esc(r.note)}</small><small>数据日 ${esc(dateOnly(r.as_of || payload.as_of))}</small></td><td title="未舍入值 ${esc(r.value)}">${value(r)}<small>${esc(r.timing_note ? textDates(r.timing_note) : dateText(timing))}</small></td>${extreme}${prior}<td>${esc(r.op)} ${num(r.threshold,r.unit === "倍" || r.unit === "x" ? 2 : 0)}${esc(r.unit)}${r.hit === false && r.gap != null ? `<small>还差 ${num(r.gap,4)} ${r.unit === "%" ? "个百分点" : esc(r.unit)}</small>` : ""}</td></tr>`;
    }).join("");
    return `<div class="mh-table-wrap"><table class="mh-rule-table"><thead><tr><th>当前状态</th><th>指标 / 依据</th><th>${payload.provisional ? "最新观测" : "收盘值"}</th><th>当日价格极值</th>${payload.provisional ? "<th>上一收盘</th>" : ""}<th>触发阈值</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function renderSignals() {
    $("mhView-signals").innerHTML = `<div class="mh-controls"><label for="mhRuleSearch">搜索指标</label><input id="mhRuleSearch" type="search" placeholder="名称、维度或计算说明" value="${esc(ruleSearch)}"><label for="mhRuleFilter">状态</label><select id="mhRuleFilter"><option value="all">全部指标</option><option value="hit">当前满足</option><option value="touched">今日曾触及（价格类）</option><option value="miss">当前未满足</option><option value="unknown">本时段无有效观测</option></select></div>
      <div class="mh-grid mh-signal-grid">${["bottom","top"].map(type => { const s = payload.signals[type]; return `<article class="mh-box">${boxHead(type === "bottom" ? "底部特征 · 超卖与出清" : "顶部特征 · 拥挤与加速", `${s.hit}/12${payload.provisional ? "当前满足" : "收盘触发"} · ${s.known}项可判定 · 强度 ${s.score == null ? "覆盖不足" : `${num(s.score,0)}/100`} · ${payload.as_of}价格极值曾触及${s.touched}/${s.touch_known}项`)}<div id="mhRules-${type}">${ruleTable(type)}</div></article>`; }).join("")}</div>
      <div class="mh-notice">价格类底部规则同时观察今日最低价、当前价和上一收盘；顶部规则对应最高价。“曾触及”只表示今日出现过该极值，源未提供极值发生的具体分钟。广度、估值、量能与披露数据使用各自观测，不拼接不同时间的最低点。距离阈值≤0.5个百分点只提示临界观察，不放宽触发条件。</div>
      <div class="mh-notice">每张卡至少8项有效时才计算“触发数 / 有效项数 × 100”。相关指标不能当作独立投票；阈值为研究规则，未经顶底预测能力检验。原报告的放量反转项已移至独立确认区，新增RSI14，因此计数与原报告不可直接比较。</div>`;
    $("mhRuleFilter").value = ruleFilter;
    $("mhRuleSearch").addEventListener("input", e => { ruleSearch = e.target.value.trim(); updateRuleTables(); });
    $("mhRuleFilter").addEventListener("change", e => { ruleFilter = e.target.value; updateRuleTables(); });
  }
  function updateRuleTables() { for (const type of ["bottom","top"]) $(`mhRules-${type}`).innerHTML = ruleTable(type); }

  function chart(rows, keys, title, {keepGaps = false, describe} = {}) {
    const valid = rows.filter(r => keys.some(k => Number.isFinite(r[k])));
    if (valid.length < (keepGaps ? 1 : 2)) return '<div class="mh-empty">等待有效观测</div>';
    const points = keepGaps ? rows : valid;
    const width = 1000, height = 310, left = 62, right = 24, top = 18, bottom = 38;
    const values = valid.flatMap(r => keys.map(k => r[k])).filter(v => v != null && Number.isFinite(v));
    let lo = Math.min(...values), hi = Math.max(...values), span = hi-lo || Math.abs(hi)*.05 || 1;
    lo -= span*.1; hi += span*.1; span = hi-lo;
    const positions = points.map((row,i) => row.time ?? i);
    const timeSpan = positions.at(-1) - positions[0];
    const x = i => timeSpan ? left+(positions[i]-positions[0])*(width-left-right)/timeSpan : (left+width-right)/2;
    const y = v => top+(hi-v)*(height-top-bottom)/span;
    const grid = Array.from({length:5}, (_,i) => { const v = lo+span*i/4; return `<line class="mh-chart-grid" x1="${left}" x2="${width-right}" y1="${y(v)}" y2="${y(v)}"/><text x="${left-10}" y="${y(v)+4}" text-anchor="end">${num(v,Math.abs(v)>100 ? 0 : 1)}</text>`; }).join("");
    const tickIndices = new Set(Array.from({length:5}, (_,i) => Math.round(i*(points.length-1)/4)));
    const ticks = [...tickIndices].map(at => `<text class="mh-date-tick" x="${x(at)}" y="${height-10}" text-anchor="middle">${esc(points[at].date)}</text>`).join("");
    const lines = keys.map((key, n) => {
      let started = false;
      const dots = [];
      const path = points.map((r,i) => {
        if (!Number.isFinite(r[key])) { started=false; return ""; }
        if (r.breakBefore) started = false;
        const px = x(i).toFixed(1), py = y(r[key]).toFixed(1);
        if (keepGaps) dots.push(`<circle cx="${px}" cy="${py}" r="2.4"/>`);
        const point = `${started?"L":"M"}${px},${py}`;
        started = true;
        return point;
      }).join(" ");
      const style = ["mh-chart-line","mh-chart-ma","mh-chart-long"][n] || "mh-chart-line";
      return `<path class="${style}" d="${path}"/>${dots.length ? `<g class="${style}">${dots.join("")}</g>` : ""}`;
    }).join("");
    const layout = {width,height,left,right:width-right,top,bottom:height-bottom};
    const hits = chartDate.hits(points.map((r,i) => ({x:x(i),date:r.date,title:describe?.(r)})),layout);
    return chartDate.render("mh-chart",title,layout,grid+ticks+lines+hits);
  }
  function renderIndices() {
    const r = currentIndex(symbol);
    $("mhView-indices").innerHTML = `<div class="mh-controls"><label for="mhIndexSelect">指数</label><select id="mhIndexSelect">${payload.indices.map(i => `<option value="${esc(i.symbol)}">${esc(i.name)}</option>`).join("")}</select></div>
      <article class="mh-box mh-space">${boxHead(`${r.name || "指数"} · 量价与波段`, `最新快照 ${dateText(r.quote_at || r.as_of) || "未取得"} · ${r.provisional ? "盘中" : "收盘"} ${num(r.close,2)} · ${r.trend || "数据不足"}`)}<div id="mhIndexChart"></div></article>
      <h3>最新快照指标</h3>
      <div class="mh-kpis">${stat("20日涨跌", signed(r.ret20), `60日 ${signed(r.ret60)}`)}${stat("RSI14", num(r.rsi14), "Wilder平滑 · ≤30超卖 / ≥80过热观察")}${stat("20日年化波动率", pct(r.volatility20), "对数日收益样本标准差 × √250")}${stat("当日 / 前5日均量", r.provisional ? "收盘后确认" : `${num(r.volume5,2)}x`, r.provisional ? "全天成交量和均量比仅在收盘后确认" : `当日 / 250日均量 ${num(r.volume250,2)}x`)}</div>
      <article class="mh-box mh-space">${boxHead("指数指标矩阵", "各指数保留自己的数据日期；非同日指标不合入综合评分")}${indexTable(true)}</article>
      <article class="mh-box">${boxHead("波段回撤参考位", "近500个交易日：先低后高的收盘价锚点；仅作价位观察")}
      <div class="mh-kpis">${Object.entries(r.levels || {}).map(([p,v]) => stat(`${num(Number(p)*100)}% 回撤`, num(v,1), `波段低 ${num(r.swing_low,0)} / 高 ${num(r.peak,0)}`)).join("") || '<p class="mh-empty">缺少有效波段锚点</p>'}</div>
      <p class="mh-note">参考位不是预测目标。跌破后能否收回、量价配合与广度变化，比单独触及某个点位更有意义。</p></article>`;
    $("mhIndexSelect").value = symbol;
    window.LumoPriceChart.mount($("mhIndexChart"),r,{state:indexWindow,onRange:remember,
      loadRange:bounds=>window.LumoPriceChart.indexRange(r.symbol,bounds)});
    $("mhIndexSelect").addEventListener("change", e => { symbol=e.target.value;remember();renderIndices(); });
  }
  function internalsStatus(row) {
    if (row.as_of !== payload.as_of) return "前次收盘";
    return row.provisional ? "盘中采样" : "收盘";
  }
  function turnoverNote(row, field = "turnover") {
    const previous = row?.vs_previous?.[field];
    if (row?.[`${field}_yuan`] == null) return "待补成交额";
    if (row.provisional) return previous?.progress_pct == null ? "上日全天成交额待补" : `占上日全天 ${breadthPct(previous.progress_pct)}`;
    if (previous?.change_pct == null) return "上日可比成交额待补";
    const state = {expansion:"放量", contraction:"缩量", flat:"基本持平"}[previous.state];
    return `较上一交易日 ${signed(previous.change_pct)} · ${state}`;
  }
  function countChanges(row) {
    return countFields.map(([,key]) => signed(row?.vs_previous?.[`${key}_pct`])).join(" / ");
  }
  function renderTurnoverChart(history) {
    const label = "沪深两市每日成交额（亿元）";
    const rows = history.map(r => ({date: dateOnly(r.as_of), row:r,
      total: r.sh_sz_turnover_yuan == null ? null : r.sh_sz_turnover_yuan/1e8,
      sh: r.exchange_turnover_yuan?.SH == null ? null : r.exchange_turnover_yuan.SH/1e8,
      sz: r.exchange_turnover_yuan?.SZ == null ? null : r.exchange_turnover_yuan.SZ/1e8}));
    const latest = [...rows].reverse().find(r => r.total != null)?.row;
    const describe = ({date,total,sh,sz,row}) => `${date} · 沪深合计 ${num(total,2)} · 沪市 ${num(sh,2)} · 深市 ${num(sz,2)} 亿元 · ${turnoverNote(row,"sh_sz_turnover")}`;
    return `<article class="mh-box mh-space" id="mhTurnoverChart">${boxHead(label, latest ? `${dateOnly(latest.as_of)} · ${turnover(latest.sh_sz_turnover_yuan)} · ${turnoverNote(latest,"sh_sz_turnover")}` : "等待沪深 A 股收盘数据")}
      <div class="mh-chart-legend"><span><i></i>沪深合计</span><span><i></i>沪市</span><span><i></i>深市</span></div>
      ${chart(rows,["total","sh","sz"],label,{keepGaps:true,describe})}</article>`;
  }
  function renderDirectionAnalysis(row) {
    if (!row) return "";
    const breadth = row.breadth_pct;
    const participation = breadth == null ? "等待有效涨跌样本。" : `上涨股占涨跌股 ${breadthPct(breadth)}，${breadth === 50 ? "两组家数相同" : breadth > 50 ? "上涨家数较多" : "下跌家数较多"}。`;
    const concentration = row.amount_breadth_gap_pp == null ? "上涨组与下跌组的成交额待补齐。" :
      `上涨股贡献 ${breadthPct(row.up_amount_pct)} 的涨跌组成交额，份额较其家数占比 ${pp(row.amount_breadth_gap_pp)}。`;
    const meanAmount = row.amount_trin == null ? "" : `上涨股单股成交额为下跌股的 ${num(1/row.amount_trin,2)} 倍。`;
    const meanVolume = row.trin == null ? "" : `上涨股单股成交股数为下跌股的 ${num(1/row.trin,2)} 倍。`;
    const limits = row.limits;
    const limitText = !limits ? "涨跌停待获取实际限价。" : limits.estimated ?
      `近涨停 ${num(limits.up,0)} 只，近跌停 ${num(limits.down,0)} 只（按涨跌幅估算）。` :
      `涨停 ${num(limits.up,0)} 只，跌停 ${num(limits.down,0)} 只。实际限价覆盖 ${num(limits.covered,0)} 只，另有 ${num(limits.unverified,0)} 只待核验。`;
    return `<div class="mh-table-wrap"><table aria-label="上涨家数、成交额与成交股数对照"><thead><tr><th>涨跌组结构</th><th>上涨组</th><th>下跌组</th><th>上涨组份额</th><th>较上涨广度</th><th>TRIN</th></tr></thead><tbody>
      <tr><th>家数</th><td>${num(row.up,0)}</td><td>${num(row.down,0)}</td><td>${breadthPct(breadth)}</td><td>—</td><td>—</td></tr>
      <tr><th>成交额</th><td>${turnover(row.up_amount_yuan)}</td><td>${turnover(row.down_amount_yuan)}</td><td>${breadthPct(row.up_amount_pct)}</td><td>${pp(row.amount_breadth_gap_pp)}</td><td>${num(row.amount_trin,3)}</td></tr>
      <tr><th>成交股数</th><td>${num(row.up_volume_shares,0)}</td><td>${num(row.down_volume_shares,0)}</td><td>${breadthPct(row.up_volume_pct)}</td><td>${pp(row.volume_breadth_gap_pp)}</td><td>${num(row.trin,3)}</td></tr></tbody></table></div>
      <p class="mh-note">${esc(participation + concentration + meanAmount + meanVolume)} ${esc(turnoverNote(row))}。</p>
      <p class="mh-note">净上涨家数 ${num(row.net_advances,0)} · 个股涨跌中位数 ${signed(row.median_return_pct)} · 上涨组前 10 只成交额占比 ${breadthPct(row.up_top10_amount_pct)}。${esc(limitText)}</p>`;
  }
  function renderIntradayStructure(points) {
    const title = "盘中量价结构（%）";
    const rows = points.map((r,i) => {
      const time = Date.parse(r.captured_at);
      return {date:dateText(r.captured_at).slice(11), time, breadth:r.breadth_pct, amount:r.up_amount_pct, volume:r.up_volume_pct,
        breakBefore:i > 0 && time - Date.parse(points[i-1].captured_at) > 600000};
    });
    const describe = r => `${r.date} · 上涨家数 ${breadthPct(r.breadth)} · 上涨股成交额 ${breadthPct(r.amount)} · 上涨股成交股数 ${breadthPct(r.volume)}`;
    const busy = ["queued","running"].includes(payload.job?.status);
    return `<article class="mh-box mh-space" id="mhIntradayStructure">${boxHead(title, `${dateOnly(payload.as_of)} · ${num(points.length,0)} 次分页快照`)}
      <div class="mh-controls"><button type="button" id="mhStructureRefresh" class="button secondary" ${busy ? "disabled" : ""}>采集量价快照</button><label><input type="checkbox" id="mhStructureAuto" ${autoStructure ? "checked" : ""}> 交易时段每 3 分钟采样</label></div>
      <div class="mh-chart-legend"><span><i></i>上涨家数占比</span><span><i></i>上涨股成交额占比</span><span><i></i>上涨股成交股数占比</span></div>
      ${chart(rows,["breadth","amount","volume"],title,{keepGaps:true,describe})}</article>`;
  }
  function renderMarketInternals() {
    const {current} = payload.market_internals || {};
    const trinNote = !current ? "等待有效价量样本" : current.missing_volume ? "部分股票缺少有效成交量" :
      current.trin == null ? "需要同时有上涨股和下跌股" : current.trin < 1 ? "上涨股平均成交量更大" :
      current.trin > 1 ? "下跌股平均成交量更大" : "两组平均成交量相同";
    const fields = current ? [
      ["上涨家数 / 下跌家数 / 平盘家数", countFields.map(([,key]) => num(current[key],0)).join(" / "), `较上日收盘 ${countChanges(current)}`],
      [current.provisional ? "盘中累计成交额" : "每日成交额", turnover(current.turnover_yuan), turnoverNote(current)],
      ["上涨股 / 下跌股成交量（股）", `${num(current.up_volume_shares,0)} / ${num(current.down_volume_shares,0)}`],
      ["有效样本 / 行情股票数", `${num(current.eligible,0)} / ${num(current.universe,0)}`,
        current.universe ? `涨跌分类覆盖 ${breadthPct(100*current.eligible/current.universe)} · 涨跌组成交额样本 ${num(current.directional_amount_sample,0)} 只` : ""],
      ["有效成交额 / 缺有效成交额", `${num(current.turnover_sample,0)} / ${num(current.missing_amount,0)} 只`],
      ["零成交 / 缺价格或昨收 / 缺有效成交量", `${num(current.inactive,0)} / ${num(current.missing_price,0)} / ${num(current.missing_volume,0)}`],
    ] : [];
    const time = current?.sample_started_at ? ` · 采集 ${dateText(current.sample_started_at)} 至 ${dateText(current.captured_at)}` : "";
    const previous = current?.vs_previous;
    const comparisonBasis = previous?.as_of ? ` · 对照 ${dateOnly(previous.as_of)} 收盘${previous.source ? ` · ${previous.source}` : "待补"}` : "";
    return `<article class="mh-box mh-space" id="mhMarketInternals">${boxHead("市场广度与量价结构", "沪深京 A 股 · 按行情价相对参考昨收分组", current ? badge(`${dateOnly(current.as_of)} · ${internalsStatus(current)}`, current.as_of !== payload.as_of ? "warn" : "") : badge("待补数据", "warn"))}
      <dl class="mh-detail-list"><div><dt>市场广度 · 上涨家数占比</dt><dd>${breadthPct(current?.breadth_pct)}</dd><small>上涨家数 ÷（上涨家数 + 下跌家数）</small></div>
        <div><dt>阿姆斯指数 · TRIN</dt><dd>${num(current?.trin,3)}</dd><small>${esc(trinNote)}</small></div>
        ${fields.map(([key,value,note]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd>${note ? `<small>${esc(note)}</small>` : ""}</div>`).join("")}</dl>
      <p class="mh-note">${current ? esc(`${current.source}${time}${comparisonBasis}`) : "点击完整更新获取盘中快照及已披露日线。"}</p>
      ${renderDirectionAnalysis(current)}
      <p class="mh-note">各份额仅比较上涨、下跌两组。TRIN 为下跌组单股均值除以上涨组单股均值。收盘成交额变动达到 +1% 为放量，达到 −1% 为缩量。资金净流入需用带买卖方向的成交数据判断。</p></article>`;
  }
  function renderDailyData() {
    const today = dateOnly(payload.generated_at);
    return `<article class="mh-box mh-space" id="mhDailyData">${boxHead("每日数据查询", "选择交易日查看单日明细，填写结束日期可查询区间。")}
      <form class="mh-controls" id="mhDailyForm">
        <label for="mhDailyStart">交易日 / 起始日期</label><input type="date" id="mhDailyStart" name="start" value="${esc(daily.draft.start)}" max="${esc(today)}" required>
        <label for="mhDailyEnd">结束日期（可选）</label><input type="date" id="mhDailyEnd" name="end" value="${esc(daily.draft.end)}" max="${esc(today)}">
        <button type="submit" class="button">查询</button><button type="button" id="mhDailyReset" class="button secondary">最近 20 日</button>
        <button type="button" id="mhDailyBackfill" class="button secondary">补采所选日期</button>
      </form><p class="mh-note" role="status" id="mhDailyStatus"></p><div id="mhDailyResults"></div>
      <div id="mhDailyDetail"></div></article><div id="mhDailyTurnover"></div>`;
  }
  function dailyRows() {
    return daily.report?.rows || payload.market_internals?.history || [];
  }
  function renderDailyDetail() {
    const slot = $("mhDailyDetail");
    if (!slot) return;
    const row = dailyRows().find(r => r.as_of === daily.selected);
    slot.innerHTML = row?.source ? `<h4>${esc(dateOnly(row.as_of))} 收盘明细</h4>${renderDirectionAnalysis(row)}` : "";
  }
  function updateDailyBackfillButton(job = payload?.job) {
    const button = $("mhDailyBackfill");
    if (button) button.disabled = !daily.report?.rows.length || daily.loading || Boolean(daily.backfill) || ["queued","running"].includes(job?.status);
  }
  function renderDailyResults() {
    if (!$("mhDailyResults")) return;
    const rows = dailyRows();
    const covered = daily.report?.covered ?? payload.market_internals?.covered ?? 0;
    const range = daily.report ? `${daily.report.start}${daily.report.start === daily.report.end ? "" : ` 至 ${daily.report.end}`}` : "最近 20 个交易日";
    $("mhDailyStatus").textContent = [daily.status, range, `${covered} / ${rows.length} 个交易日有记录`, daily.report?.message].filter(Boolean).join(" · ");
    updateDailyBackfillButton();
    $("mhDailyResults").innerHTML = rows.length ? `<div class="mh-table-wrap"><table aria-label="每日收盘记录"><thead><tr><th>交易日</th><th>涨 / 跌 / 平</th><th>上涨广度</th><th>TRIN（股数 / 金额）</th><th>上涨组成交占比（股数 / 金额）</th><th>沪深京成交额</th><th>沪深成交额</th><th>来源</th></tr></thead><tbody>
      ${[...rows].reverse().map(r => `<tr><td>${r.source ? `<button type="button" class="mh-inline-link mh-select-index" data-mh-daily-day="${esc(r.as_of)}">${esc(dateOnly(r.as_of))}</button>` : esc(dateOnly(r.as_of))}</td>
        <td>${r.eligible == null ? "—" : `${countFields.map(([,key]) => num(r[key],0)).join(" / ")}<small>较上日 ${countChanges(r)}</small>`}</td><td>${breadthPct(r.breadth_pct)}</td><td>${num(r.trin,3)} / ${num(r.amount_trin,3)}</td><td>${breadthPct(r.up_volume_pct)} / ${breadthPct(r.up_amount_pct)}</td>
        <td>${turnover(r.turnover_yuan)}<small>${esc(turnoverNote(r))}</small></td><td>${turnover(r.sh_sz_turnover_yuan)}<small>${esc(turnoverNote(r,"sh_sz_turnover"))}</small></td><td>${esc(r.source || "待补日线")}</td></tr>`).join("")}</tbody></table></div>` : "";
    renderDailyDetail();
    $("mhDailyTurnover").innerHTML = renderTurnoverChart(rows);
  }
  async function searchDaily(query, {notice = "", fill = false} = {}) {
    const id = ++daily.requestId;
    daily.autoFill = null;
    daily.loading = true;
    daily.status = "正在查询每日数据";
    renderDailyResults();
    try {
      const report = await request(`/api/market-health/daily-structure?${new URLSearchParams(query)}`);
      if (id !== daily.requestId) return;
      daily.query = query;
      daily.report = report;
      daily.selected = report.start === report.end ? report.start : "";
      daily.status = notice;
      remember();
      if (fill && report.pending) {
        daily.autoFill = query;
        if (["queued","running"].includes(payload.job?.status)) daily.status = "等待当前更新结束后补采";
      }
    } catch (e) {
      if (id === daily.requestId) daily.status = e.message;
    } finally {
      if (id === daily.requestId) {
        daily.loading = false;
        renderDailyResults();
      }
    }
    if (id === daily.requestId && daily.autoFill && !["queued","running"].includes(payload.job?.status)) backfillDaily();
  }
  async function backfillDaily() {
    if (!daily.query || daily.backfill) return;
    const query = daily.query;
    daily.autoFill = null;
    daily.backfill = {id:null, query};
    daily.status = "正在提交补采任务";
    renderDailyResults();
    try {
      const {job} = await request("/api/market-health/daily-structure/refresh", {method:"POST",body:JSON.stringify(query)});
      if (job.params?.mode !== "daily" || job.params.start !== query.start || job.params.end !== (query.end || query.start)) {
        payload.job = job;
        renderJob(job);
        daily.autoFill = daily.query === query ? query : null;
        throw new Error("等待当前更新结束后补采");
      }
      daily.backfill.id = job.id;
      daily.status = "正在补采所选日期";
      payload.job = job;
      renderJob(job);
      await load(true);
    } catch (e) {
      daily.backfill = null;
      daily.status = e.message;
    }
    renderDailyResults();
  }
  function renderBreadth() {
    const b = payload.breadth || {}, m = payload.market?.values || {}, report=breadthRenderer.report(payload);
    const internals = payload.market_internals || {};
    const c = payload.ice_coverage;
    const coverage = !c ? '' : `近期快照收盘观测 ${num(c.covered,0)} / ${num(c.total,0)} 个交易日` + (c.missing
      ? ` · 待补 ${num(c.missing,0)} 日（最早 ${dateOnly(c.first_missing) || "未取得"}） · ${c.running ? '正在补采' : c.retry_at ? '下次重试 '+window.LumoDate.formatEpoch(c.retry_at) : '等待补采'}` : ' · 无缺口');
    const participation=report.participation||{};
    const details = [["换手率中位数",pct(m.turnover_median)],["个股涨跌中位数",signed(participation.median_return)],["市值加权涨跌",signed(participation.weighted_return)],["市值加权有效样本",`${num(participation.weighted_sample,0)} 只`],["市值加权 − 个股中位",pp(participation.weighted_gap_pp)],["近涨停 / 近跌停",`${num(m.limit_up,0)} / ${num(m.limit_down,0)}`]];
    $("mhView-breadth").innerHTML = `<nav id="mhBreadthNav" class="mh-breadth-nav mh-space" aria-label="广度与估值内容导航">${[["mhBreadthOverview","概览"],["mhBreadthTrend","趋势广度"],["mhValuationDistribution","估值分布"],["mhBreadthDaily","每日统计"],["mhBreadthIce","情绪冰点"],["mhBreadthRecords","观测记录"]].map(([id,label])=>`<button type="button" data-mh-breadth-jump="${id}">${label}</button>`).join("")}</nav>
      <article id="mhBreadthOverview" class="mh-box mh-space" tabindex="-1">${breadthRenderer.overview(report)}</article>
      <div class="mh-grid"><article id="mhBreadthTrend" class="mh-box" tabindex="-1">${breadthRenderer.trend(report)}<p class="mh-note">个股250日高点回撤中位：${signed(b.dd52_median)}。</p></article>
      <article class="mh-box"><div id="mhValuationDistribution" tabindex="-1">${breadthRenderer.valuation(report,breadthView.valuation)}</div><details class="mh-breadth-trading" data-mh-breadth-detail="trading" ${breadthView.details.trading?"open":""}><summary>交易结构补充</summary><p class="mh-note">涨跌观测 ${esc(participation.as_of||"未取得")} · ${participation.provisional?"盘中":"收盘"}；换手与近涨跌停 ${esc(payload.market?.as_of||"未取得")}。两种收益样本可能不同，差值只作描述性对照。</p><dl class="mh-detail-list">${details.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl><p class="mh-note">${esc(participation.source||payload.market?.source || "未取得来源")}。“近涨跌停”按板块/ST涨跌幅近似，未核验实际涨跌停价，不含新股特殊规则。</p></details></article></div>
      <div id="mhBreadthDaily" tabindex="-1">${renderDailyData()}</div>${renderMarketInternals()}${renderIntradayStructure(internals.intraday_history || [])}
      <article id="mhBreadthIce" class="mh-box mh-space" tabindex="-1">${boxHead("情绪冰点线 · 上证指数", "冰点代理：站上20日均线的样本占比 ≤20%。虚线为观察阈值，不等同于市场温度或反转确认。")}
      <p class="mh-note" id="mhIceCoverage" role="status">${esc(coverage)}</p><div id="mhIceChart"></div></article>
      <article id="mhBreadthRecords" class="mh-box mh-space" tabindex="-1">${breadthRenderer.records(payload,report,breadthView.scope)}</article>
      ${(report.notes||[]).length?`<details class="mh-box mh-space mh-breadth-method" data-mh-breadth-detail="method" ${breadthView.details.method?"open":""}><summary>样本、比较与估值口径</summary>${report.notes.map(note=>`<p class="mh-note">${esc(note)}</p>`).join("")}</details>`:""}
      <div class="mh-notice">观察“指数与个股背离”：比较大盘与小盘20日收益差、指数回撤和个股回撤中位。样本口径不同只能交叉观察，不能直接把差值解释成确定的补跌空间。</div>`;
    renderDailyResults();
    window.LumoPriceChart.mount($("mhIceChart"),currentIndex('sh000001'),{state:iceWindow,
      observations:payload.ice_history ?? payload.observations ?? [],
      loadRange:bounds=>window.LumoPriceChart.indexRange('sh000001',bounds,{breadth:true})});
  }
  function renderConfirmation() {
    $("mhView-confirmation").innerHTML = `<div class="mh-notice">收盘确认截至 ${esc(payload.confirmation_as_of || payload.as_of)}。盘中预警不改写此区。以下为经验确认条件，至少3项触发后标记“条件满足”；缺失项保留待补，信号仍可能失效。跨日确认仅用已收盘交易日。</div>
      <div class="mh-grid">${["bottom","top"].map(type => { const c=payload.confirmation[type]; return `<article class="mh-box">${boxHead(type === "bottom" ? "底部反转 · 观察修复" : "顶部转弱 · 观察破位", `${c.hit}/5 项触发 · ${c.known}项可判定`, badge(c.hit >= 3 ? "条件满足" : "等待验证", c.hit >= 3 ? "" : "muted"))}
        <ul class="mh-check-list">${c.items.map(r => `<li><span class="mh-check-icon ${r.hit === true ? "yes" : r.hit == null ? "unknown" : ""}">${r.hit === true ? "✓" : r.hit == null ? "?" : "○"}</span><div><strong>${esc(r.label)} · ${r.hit === true ? "已触发" : r.hit == null ? "待补数据" : "未触发"}</strong><small>${esc(r.note)}</small></div></li>`).join("")}</ul></article>`; }).join("")}</div>
      <article class="mh-box">${boxHead("如何跟踪信号是否失效")}
        <p class="mh-note">底部修复后若指数重新跌破反转日收盘、继续创20日新低，或广度持续恶化，应重新评估。高位转弱后若价格收复MA20并伴随广度恢复，顶部判断也应重新评估。单个信号和单个支撑位都不能独立确认顶底。</p>
        <p class="mh-note">连续广度使用最近5个实际交易日、同一采样方法与固定样本组；首次完整更新即可从历史日线回算。逐日数值、实际分母及组号见广度观测记录。</p></article>`;
  }
  function renderHistory() {
    $("mhView-history").innerHTML = `<div class="mh-box mh-space">${boxHead("原报告历史条件对照", `固定研究参考 · 截止 ${dateOnly(payload.historical_as_of)} · 2015–2026`, `<a class="mh-inline-link" href="${esc(payload.report_url)}" target="_blank" rel="noopener">阅读原报告 ↗</a>`)}
      <div class="mh-controls"><span>持有窗口</span><div class="mh-segment">${[20,60,120].map(n => `<button type="button" data-mh-horizon="${n}" aria-pressed="${horizon === n}">${n}交易日</button>`).join("")}</div></div>
      <div class="mh-table-wrap"><table><thead><tr><th>指数 / 条件</th><th>触发样本</th><th>后${horizon}日中位收益</th><th>历史胜率</th><th>最差收益</th></tr></thead><tbody>${Object.entries(payload.historical || {}).flatMap(([name,v]) => (v.events || []).map(r => `<tr><td><strong>${esc(name)}</strong><small>${esc(r.label)}</small></td><td>${num(r.n,0)} ${r.n < 10 ? badge("小样本","warn") : ""}</td><td class="${tone(r[`f${horizon}_median`])}">${signed(r[`f${horizon}_median`])}</td><td>${pct(r[`f${horizon}_win`])}</td><td class="${tone(r[`f${horizon}_min`])}">${signed(r[`f${horizon}_min`])}</td></tr>`)).join("") || '<tr><td colspan="5">尚无原报告对照数据</td></tr>'}</tbody></table></div>
      <p class="mh-note">这里保留原报告结果，没有随最新价格重新筛选。每个窗口的有效样本可能因未来数据不足而不同；原报告只记录总触发数。事件间隔≥20日仍可能属于同轮行情，结果存在相关性及重叠，不能把“6次全胜”理解成未来100%胜率。</p></div>
      <article class="mh-box mh-history-note">${boxHead("报告中的情景假设", "仅记录2026-10-22的研究观点，未用于当前评分")}
        <p>原报告观察上证3450–3500、创业板2400–2520、科创50的1200–1250区域，并给出2026年11月至2027年1月的情景窗口。这些是当时的假设，未经后续行情确认。</p>
        <p>原报告将上证3290附近持续跌破视为框架失效。本菜单不沿用固定点位或日期自动宣布见底；最新判断由同日数据、动态波段参考与独立反转条件产生。</p></article>`;
  }
  function renderMethod() {
    const factors = payload.temperature.factors;
    $("mhView-method").innerHTML = `<div class="mh-grid mh-method"><article class="mh-box">${boxHead("温度与特征分如何计算")}
      <p>温度0–100：MA20广度20%、MA250广度15%、上涨家数比15%、上证RSI14占15%、上证年线偏离占15%、60日新低压力10%、正PB中位10%。低值表示悲观／超卖，高值表示偏热／拥挤。</p>
      <p>年线分=50+偏离百分点×2.5；新低压力分=100−新低占比百分点×2；PB分=(PB中位−1.5)÷3×100。各因子截断至0–100后加权。有效权重≥60%才输出温度，按有效权重归一化。</p>
      <p>≤20极度悲观，≤40偏悲观，40–60中性，≥60偏热，≥80过热。它是自定义状态刻度，不是成熟市场恐贪指数，也不是现有“恐慌卖压分”的倒数。</p>
      <p>顶、底各12项经验规则：至少8项可判定后按触发数/有效项×100显示强度。指标高度相关，未经样本外验证，强度不是概率。位置分与确认条件只生成技术侧阶段，最终综合结论还需跨市场证据。</p>
      <p>本次有效因子：${factors.map(f => `${esc(f.label)} ${num(f.weight*100,0)}%→${num(f.value)}`).join("；") || "尚无"}。</p></article>
      <article class="mh-box">${boxHead("趋势、日期与样本约束")}
      <p>MA为收盘价简单均线；年线偏离=(收盘/MA250−1)×100；500日高点回撤=(当前收盘/近500日最高收盘−1)×100；RSI14采用Wilder递推平滑；波动率为最近20个对数日收益样本标准差×√250。</p>
      <p>年量比使用当日成交量/含当日在内250日均量；5日量比使用当日量/前5日均量。因子均为全天收盘口径，15:30前更新只采用已收盘的指数日线。</p>
      <p>市场和广度与上证基准日期不同时保留展示，但不参与当天温度、特征和确认。更新失败保留原快照及原日期；间隔超过4个自然日提示滞后，周末、节假日需结合最后交易日理解。</p>
      <p>沪深京市场广度按有成交、有效参考昨收的个股计算，上涨家数除以涨跌家数之和。TRIN 比较下跌股与上涨股的平均成交股数。两项是独立量价观测，温度中的上涨家数比仍以全部有效涨跌幅样本为分母。</p>
      <p>广度按市值8层、每层最多50只沪深A股采样，从300根个股日线回算最近5个交易日，最早日也需250根历史。停牌缺日和短历史成分整个窗口排除，固定组不足200只则保留旧值。新浪未复权价格可能受除权干扰，抽样不包含北交所；估值使用全A正PE/PB有效样本。</p></article></div>
      <article class="mh-box mh-method">${boxHead("来源与研究边界")}
      <p>七维压力权重：指数25%、广度15%、估值10%、期指15%、两融10%、量化10%、资金15%。总有效权重至少65%、至少5维，并且期指／两融／量化都有效，才输出综合压力；其余状态明确为证据未齐。上一交易日的两融、席位与资金只按一半权重；量化仅用同日。</p>
      <p>期指压力使用前20席位净多头增减/多空合计，至少2个品种；同合约连续两日且现货日期已验证时才加入基差变化。绝对净空头及静态贴水不直接判空。两融压力使用融资余额5期变化与净买入/余额；量化使用空向异动和疑似砸盘样本比例；资金使用近5期净流入占比均值，详细依据在跨市场页逐维列出。</p>
      <p>两融接口与披露口径：<a href="https://tushare.pro/document/2?doc_id=58" target="_blank" rel="noopener">TuShare融资融券汇总</a>；资金金额单位参考：<a href="https://tushare.pro/document/2?doc_id=345" target="_blank" rel="noopener">大盘资金流向（元）</a>。期货净空头可能包含套期保值；期货减现货的升贴水符号与部分教材的“现货减期货”基差定义相反。</p>
      <p>历史基差接入TuShare期货日线与指数日线，比较本日主力同一合约的相邻交易日收盘。量化优先使用已保存的真实盘口日榜；缺历史事件时，接入历史日线、每日量比和个股资金流回补收盘行为代理，明确展示代理来源和公式，并减半可靠度，不伪造盘口事件。</p>
      <p>研究起点：用户提供的A股底部报告；指数与抽样日线：新浪财经；全A估值快照：新浪财经；11年历史条件：原报告的腾讯财经长历史。所有新增规则均为研究设定，尚未进行预测准确率校验。</p>
      <p>估值口径参考：<a href="https://www.sse.com.cn/market/stockdata/price/sh/" target="_blank" rel="noopener">上交所市盈率统计说明</a>。单股或市场的正值中位数与交易所加权平均有不同经济含义，不能直接混比。</p>
      <p><a href="${esc(payload.report_url)}" target="_blank" rel="noopener">打开完整原始报告</a> · 本页供量化研究，不构成投资建议。</p>
      ${(payload.notes || []).map(note => `<p>${esc(note)}</p>`).join("")}</article>`;
  }
  function renderJob(job) {
    clearTimeout(pollTimer);
    const busy = job && ["queued","running"].includes(job.status);
    $("mhIndexRefresh").disabled = Boolean(busy);
    $("mhFullRefresh").disabled = Boolean(busy);
    $("mhCrossRefresh").disabled = Boolean(busy);
    for (const button of root.querySelectorAll('[data-mh-derivatives-refresh]')) button.disabled = Boolean(busy || refreshing);
    if ($("mhStructureRefresh")) $("mhStructureRefresh").disabled = Boolean(busy);
    updateDailyBackfillButton(job);
    $("mhJob").hidden = !job;
    if (!job) return;
    const status = {queued:"已排队",running:"更新中",finished:"更新完成",failed:"更新失败"}[job.status] || job.status;
    const detail = job.error || job.result?.notes?.join("；") || job.logs?.slice(-1)[0] || (busy ? "正在准备数据" : "本轮更新已结束，请以各统计实际数据时点为准");
    $("mhJob").innerHTML = `${busy ? `<progress value="${Math.max(0,Math.min(100,job.progress || 0))}" max="100"></progress>` : ""}<strong>${esc(status)}${busy ? ` ${num(job.progress || 0,0)}%` : ""}</strong><small>${esc(detail)}</small>`;
    if (busy) pollTimer=setTimeout(() => load(true),2500);
  }
  function activatePayload() {
    const current=viewMode === "latest" ? envelope.intraday : null;
    payload={...envelope,...(current || {}),provisional:Boolean(current),job:envelope.job,stale:current ? false : envelope.stale};
  }
  function renderTiming() {
    const status=envelope.live_status || {};
    $("mhLatestMode").setAttribute("aria-pressed",String(viewMode === "latest"));
    $("mhCloseMode").setAttribute("aria-pressed",String(viewMode === "close"));
    $("mhLatestMode").disabled=false;$("mhCloseMode").disabled=false;
    $("mhQuoteStatus").textContent=`${viewMode === "close" ? `已选择收盘确认 ${dateOnly(envelope.as_of)}。` : ""}${status.message || ""}；行情时间 ${dateText(status.quote_at) || "未取得"}${status.error ? `；${status.error}` : ""}`;
    $("mhTimingBody").innerHTML=`<p class="mh-note">页面计算 ${esc(dateText(envelope.generated_at))}；实时请求 ${esc(dateText(status.requested_at) || "未请求")}。下面列的是统计输入的实际日期/时间，刷新时间不代表披露数据已更新。</p><div class="mh-table-wrap"><table><thead><tr><th>统计项目</th><th>实际数据日 / 行情时间</th><th>时点类型</th><th>来源与更新说明</th></tr></thead><tbody>${(payload.timing || []).map(r=>`<tr><td>${esc(r.label)}</td><td>${esc(dateText(r.data_at) || "未取得")}<small>交易日 ${esc(dateOnly(r.as_of) || "未取得")}</small></td><td>${badge(r.kind,/上一|非今日|历史|旧日/.test(r.kind) ? "warn" : "")}</td><td>${esc(r.source || "未取得")}<small>${esc(textDates(r.note))}</small></td></tr>`).join("")}</tbody></table></div>`;
  }
  function render() {
    const pendingClose = envelope.live_status?.close_pending;
    $("mhMeta").textContent = payload.provisional ? `${pendingClose ? "今日最新报价 · 今日收盘数据待更新" : "今日盘中预警"} · 指数行情 ${dateText(payload.quote_at)} · 收盘确认截至 ${dateOnly(payload.confirmation_as_of)}` : `收盘统计 ${dateOnly(payload.as_of) || "未取得"} · ${payload.source === "baseline" ? "研究基线" : "收盘快照"} · ${pendingClose ? "今日收盘数据待更新" : "今日实时行情另按源时间标记"}`;
    renderTiming();
    if ($("refreshMeta")) $("refreshMeta").textContent = `体检 · ${payload.as_of || "待更新"}`;
    if (!payload.indices.some(r => r.symbol === symbol)) symbol="sh000001";
    renderOverview();renderCross();renderSignals();renderIndices();renderBreadth();renderConfirmation();renderHistory();renderMethod();
    if (window.LumoMarketDerivatives) $("mhView-derivatives").innerHTML = window.LumoMarketDerivatives.render(payload.fusion?.derivatives);
    updateCorrelationSummaries();
    $("mhLoading").hidden=true;root.setAttribute("aria-busy","false");$("mhExport").disabled=false;
    selectTab(section);renderJob(payload.job);
  }
  async function request(url, options = {}) {
    const response=await fetch(url, {...options, headers:{"Content-Type":"application/json",...options.headers}});
    if (!response.ok) { let message="数据读取失败，请重试";try { message=(await response.json()).error || message; } catch {} throw new Error(message); }
    return response.json();
  }
  function scheduleLive() {
    clearTimeout(liveTimer);
    liveTimer=setTimeout(()=>{if (!document.hidden) load(true);else scheduleLive();},section === 'breadth' && payload?.ice_coverage?.running ? 8000 : 30000);
  }
  async function load(poll = false, force = false) {
    const id=++requestId;
    try {
      const value=await request(`/api/market-health?live=${force ? "force" : "1"}`);
      if (id !== requestId) return;
      const old=payload;
      envelope=value;activatePayload();error();
      const unchanged=old && old.as_of===payload.as_of && old.updated_at===payload.updated_at && old.fusion?.updated_at===payload.fusion?.updated_at && old.quote_at===payload.quote_at && old.live_status?.message===payload.live_status?.message && old.live_status?.close_pending===payload.live_status?.close_pending && JSON.stringify(old.market_internals)===JSON.stringify(payload.market_internals) && JSON.stringify(old.fusion?.derivatives)===JSON.stringify(payload.fusion?.derivatives) && JSON.stringify(old.breadth_valuation)===JSON.stringify(payload.breadth_valuation);
      if (poll && unchanged) {
        if (JSON.stringify(old.ice_coverage)!==JSON.stringify(payload.ice_coverage) ||
            JSON.stringify(old.ice_history)!==JSON.stringify(payload.ice_history)) renderBreadth();
        renderJob(value.job);
      } else render();
      if (daily.backfill?.id && value.job?.id === daily.backfill.id && ["finished","failed"].includes(value.job.status)) {
        const matched = daily.query === daily.backfill.query;
        daily.backfill = null;
        const notice = value.job.error || value.job.result?.notes?.join(" · ") || "补采已结束";
        if (matched) searchDaily(daily.query,{notice});
        else { daily.status = ""; renderDailyResults(); }
      }
      scheduleLive();
      const liveDay = value.live_status?.as_of;
      const session = liveDay || value.as_of || 'initial';
      const missingPreview = value.live_status?.available && (value.intraday?.market?.as_of !== liveDay || value.intraday?.breadth?.as_of !== liveDay);
      const missingClose = Boolean(value.live_status?.close_pending);
      const missingCloseSamples = closeSession === liveDay && value.as_of === liveDay &&
        (value.market?.as_of !== liveDay || value.breadth?.as_of !== liveDay || value.market?.provisional || value.breadth?.provisional);
      const fullSession = missingCloseSamples ? `${session}:close` : session;
      const currentInternals = value.intraday || value;
      const observation = currentInternals.market_internals?.current;
      const missingInternals = observation?.as_of !== currentInternals.as_of || observation?.sh_sz_turnover_yuan === undefined;
      const needsFull = value.source === 'baseline' || !value.as_of || !value.fusion?.updated_at || missingPreview || missingCloseSamples;
      const structureVisible = (section === "overview" || section === "breadth") && (viewMode === "latest" || !value.intraday);
      const idle = !refreshing && (!value.job || ["finished","failed"].includes(value.job.status));
      if (missingClose && idle) {
        if (closeSession !== session || Date.now() - lastCloseRequest >= 60000) {
          const mode = closeSession === session ? "indices" : "full";
          closeSession = session;
          lastCloseRequest = Date.now();
          autoSession = session;
          refresh(mode);
        }
      } else if (autoSession !== fullSession && needsFull && idle) {
        autoSession = fullSession;
        refresh('full');
      } else if (idle && daily.autoFill === daily.query && daily.autoFill && !daily.loading) {
        backfillDaily();
      } else if (idle && structureVisible && missingInternals && autoInternalsSession !== session) {
        autoInternalsSession = session;
        refresh("structure");
      } else if (idle && autoStructure && section === "breadth" && viewMode === "latest" && !document.hidden && value.live_status?.structure_sampling_open) {
        const newest = Date.parse(value.intraday?.market_internals?.intraday_history?.at(-1)?.captured_at) || 0;
        if (Date.now() - Math.max(lastStructureRequest, newest) >= 180000) refresh("structure");
      }
    } catch (e) {
      error(`${e.message}。${payload ? "已有快照继续保留显示。" : "请点击顶部刷新重试。"}`);
      $("mhLoading").hidden=true;root.setAttribute("aria-busy","false");
      if (poll) pollTimer=setTimeout(() => load(true),7000);
    }
  }
  async function refresh(mode) {
    if (refreshing) return;
    refreshing = true;
    if (mode === "full") document.dispatchEvent(new CustomEvent("market-health-full-refresh"));
    if (mode === "structure") lastStructureRequest = Date.now();
    error();$("mhIndexRefresh").disabled=true;$("mhFullRefresh").disabled=true;$("mhCrossRefresh").disabled=true;
    for (const button of root.querySelectorAll('[data-mh-derivatives-refresh]')) button.disabled = true;
    if ($("mhStructureRefresh")) $("mhStructureRefresh").disabled = true;
    try {
      if (mode !== "structure") await load(false,true);
      const value=await request("/api/market-health/refresh",{method:"POST",body:JSON.stringify({mode})});
      if (payload) payload.job=value.job;
      if (envelope) envelope.job=value.job;
      renderJob(value.job);
    } catch (e) { error(e.message);renderJob(payload?.job); }
    finally { refreshing = false;renderJob(payload?.job); }
  }
  $("mhTabs").addEventListener("click", e => { const b=e.target.closest("[data-mh-tab]");if (b) selectTab(b.dataset.mhTab); });
  $("mhTabs").addEventListener("keydown", e => {
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return;
    e.preventDefault();const i=tabs.indexOf(section);
    selectTab(e.key === "Home" ? tabs[0] : e.key === "End" ? tabs.at(-1) : tabs[(i+(e.key === "ArrowRight" ? 1 : -1)+tabs.length)%tabs.length],true);
  });
  root.addEventListener("click", e => {
    const go=e.target.closest("[data-mh-go]"), index=e.target.closest("[data-mh-index]"), h=e.target.closest("[data-mh-horizon]");
    if (go) selectTab(go.dataset.mhGo,true);
    if (index) { symbol=index.dataset.mhIndex;renderIndices();selectTab("indices",true); }
    if (h) { horizon=Number(h.dataset.mhHorizon);renderHistory(); }
    if (e.target.closest("#mhStructureRefresh")) refresh("structure");
    if (e.target.closest("[data-mh-derivatives-refresh]")) refresh("cross");
    const valuation=e.target.closest("[data-mh-valuation]"),scope=e.target.closest("[data-mh-breadth-scope]"),jump=e.target.closest("[data-mh-breadth-jump]");
    if(valuation){breadthView.valuation=valuation.dataset.mhValuation==="pe"?"pe":"pb";$("mhValuationDistribution").innerHTML=breadthRenderer.valuation(breadthRenderer.report(payload),breadthView.valuation);root.querySelector(`[data-mh-valuation="${breadthView.valuation}"]`)?.focus({preventScroll:true});}
    if(scope){breadthView.scope=scope.dataset.mhBreadthScope==="all"?"all":"cohort";$("mhBreadthRecords").innerHTML=breadthRenderer.records(payload,breadthRenderer.report(payload),breadthView.scope);root.querySelector(`[data-mh-breadth-scope="${breadthView.scope}"]`)?.focus({preventScroll:true});}
    if(jump){const target=$(jump.dataset.mhBreadthJump);if(target&&root.contains(target)){target.scrollIntoView({block:"start",behavior:"auto"});target.focus({preventScroll:true});}}
    const day = e.target.closest("[data-mh-daily-day]");
    if (day) { daily.selected = day.dataset.mhDailyDay; renderDailyDetail(); }
    if (e.target.closest("#mhDailyBackfill")) backfillDaily();
    if (e.target.closest("#mhDailyReset")) {
      daily.requestId++;
      Object.assign(daily, {draft:{start:"",end:""}, query:null, report:null, selected:"", status:"", loading:false, autoFill:null});
      remember();
      if ($("mhDailyForm")) $("mhDailyForm").reset();
      if ($("mhDailyStart")) $("mhDailyStart").value = "";
      if ($("mhDailyEnd")) $("mhDailyEnd").value = "";
      renderDailyResults();
    }
  });
  root.addEventListener("submit", e => {
    if (e.target.id !== "mhDailyForm") return;
    e.preventDefault();
    const query = {start:$("mhDailyStart").value,end:$("mhDailyEnd").value};
    daily.draft = {...query};
    searchDaily(query,{fill:true});
  });
  root.addEventListener("input", e => {
    if (e.target.id === "mhDailyStart" || e.target.id === "mhDailyEnd") daily.draft[e.target.name] = e.target.value;
  });
  root.addEventListener("toggle", e=>{const key=e.target.dataset?.mhBreadthDetail;if(key)breadthView.details[key]=e.target.open;},true);
  root.addEventListener("change", e => {
    if (e.target.id !== "mhStructureAuto") return;
    autoStructure = e.target.checked;
    if (autoStructure) load(true);
  });
  $("mhIndexRefresh").addEventListener("click", () => refresh("indices"));
  $("mhLatestMode").addEventListener("click",()=>{viewMode="latest";activatePayload();render();remember();load(false,true);});
  $("mhCloseMode").addEventListener("click",()=>{viewMode="close";activatePayload();render();remember();});
  $("mhCrossRefresh").addEventListener("click", () => refresh("cross"));
  $("mhFullRefresh").addEventListener("click", () => refresh("full"));
  // 专属页面接管共享刷新按钮，避免外壳只刷新任务列表。
  $("refreshBtn")?.addEventListener("click", e => {e.stopImmediatePropagation();refresh("full");},true);
  $("mhExport").addEventListener("click", () => {
    if (!payload) return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:"application/json;charset=utf-8"}));
    const a=document.createElement("a");a.href=url;a.download=`大盘体检_${payload.as_of || "未更新"}.json`;a.click();setTimeout(() => URL.revokeObjectURL(url),1000);
  });
  window.addEventListener("pagehide", () => {clearTimeout(pollTimer);clearTimeout(liveTimer);});

  const corrGroups = [
    ["综合研判", [["overview", "综合总览"], ["cross", "跨市场研判"], ["derivatives", "金融衍生品"], ["macro", "宏观经济"]]],
    ["指数与市场结构", [["indices", "指数趋势"], ["breadth", "广度与估值"], ["correlation", "技术指标关联"], ["limits", "涨跌停"]]],
    ["资金与板块", [["participants", "谁买谁卖"], ["financing", "融资风险"], ["sectors", "板块深度"]]],
    ["信号与验证", [["signals", "顶底信号"], ["confirmation", "反转确认"], ["history", "历史对照"], ["method", "指标口径"]]],
  ];
  const corrLabels = new Map(corrGroups.flatMap(([, nodes]) => nodes));
  const requestedCorrSection = q.get("networkSection");
  let corrOverview = null, corrSection = corrLabels.has(requestedCorrSection) ? requestedCorrSection : "correlation", corrSectionHome = null;
  let corrData = null, corrScene = null, corrNetwork = null, corrSymbol = "", corrRequest = 0;

  function updateCorrelationSummaries() {
    if (!corrOverview) return;
    const breadth = payload?.breadth;
    const shanghai = payload?.indices.find(index => index.symbol === "sh000001");
    const summaries = {
      overview: payload?.temperature?.score == null ? "" : `温度 ${num(payload.temperature.score, 0)}/100`,
      cross: payload?.fusion?.pressure == null ? "" : `综合压力 ${num(payload.fusion.pressure, 0)}/100`,
      derivatives: payload?.fusion?.derivatives?.summary?.label || "",
      indices: shanghai?.ret20 == null ? "" : `上证20日 ${signed(shanghai.ret20)}`,
      breadth: breadth?.above_ma20 == null ? "" : `站上MA20 ${pct(breadth.above_ma20)}`,
      signals: payload?.signals ? `底部 ${payload.signals.bottom.hit}/12 · 顶部 ${payload.signals.top.hit}/12` : "",
      correlation: corrData ? `多 ${corrData.metadata.bull_signal_count} · 空 ${corrData.metadata.bear_signal_count}` : "",
    };
    corrOverview.setSummaries(summaries);
  }

  function renderCorrelationSelection() {
    const technical = corrSection === "correlation";
    $("corrEvidenceTitle").textContent = corrLabels.get(corrSection);
    $("corrTechnical").hidden = !technical;
    $("corrOpenSection").hidden = technical;
  }

  function mountCorrelationSection() {
    const panel = $(`mhView-${corrSection}`);
    corrSectionHome = document.createComment("market health panel");
    panel.before(corrSectionHome);
    $("corrSectionContent").append(panel);
    panel.setAttribute("role", "region");
    panel.hidden = false;
  }

  function restoreCorrelationSection() {
    if (!corrSectionHome) return;
    const panel = $(`mhView-${corrSection}`);
    panel.hidden = true;
    panel.setAttribute("role", "tabpanel");
    corrSectionHome.replaceWith(panel);
    corrSectionHome = null;
  }

  function showCorrelationSection(key) {
    if (key !== corrSection) {
      restoreCorrelationSection();
      corrSection = key;
      renderCorrelationSelection();
      if (key !== "correlation") mountCorrelationSection();
      else if (!corrSymbol) loadCorrelation();
      remember();
    }
    $("corrEvidenceTitle").scrollIntoView({block: "nearest"});
  }

  async function loadCorrelation() {
    const index = $("corrIndex").value, id = ++corrRequest;
    corrSymbol = index;
    corrData = null;
    updateCorrelationSummaries();
    $("corrMeta").textContent = $("corrIndex").selectedOptions[0].textContent;
    $("corrExportText").disabled = true;
    for (const panel of ["corrSummary", "corrVisualization", "corrTextTree"]) $(panel).hidden = true;
    $("corrStatus").textContent = "正在加载关联分析…";
    $("corrError").hidden = true;
    try {
      const data = await request(`/api/market-health/correlation?symbol=${encodeURIComponent(index)}&format=all`);
      if (id !== corrRequest) return;
      if (!data.success) throw new Error(data.error || "加载失败");
      corrData = data;
      updateCorrelationSummaries();
      renderCorrelation($("corrFormat").value);
      $("corrExportText").disabled = !data.data.text;
      $("corrStatus").textContent = `已加载 ${data.metadata.name || index} 的关联分析`;
    } catch (e) {
      if (id !== corrRequest) return;
      $("corrError").textContent = `加载失败: ${e.message}`;
      $("corrError").hidden = false;
      $("corrStatus").textContent = "";
    }
  }

  function renderCorrelation(format) {
    if (!corrData) return;
    const meta = corrData.metadata;
    $("corrMeta").textContent = `${meta.name || ""} · ${meta.as_of || ""} 收盘 ${num(meta.close,2)}`;

    // 渲染摘要
    const bullCount = meta.bull_signal_count || 0;
    const bearCount = meta.bear_signal_count || 0;
    const diff = bullCount - bearCount;
    let bias = "中性", biasClass = "mh-neutral", biasIcon = "⚖️";
    if (diff > 2) { bias = "看多"; biasClass = "mh-positive"; biasIcon = "📈"; }
    else if (diff < -2) { bias = "看空"; biasClass = "mh-negative"; biasIcon = "📉"; }

    $("corrSummary").hidden = false;
    $("corrSummaryContent").innerHTML = `
      <div class="mh-kpis">
        ${stat("看多信号", `${bullCount} 项`, "上涨驱动因素")}
        ${stat("看空信号", `${bearCount} 项`, "下跌驱动因素")}
        ${stat("市场偏向", `${biasIcon} ${bias}`, `信号差 ${diff > 0 ? '+' : ''}${diff}`, biasClass)}
      </div>
    `;

    // 切换显示格式
    if (format === "text") {
      $("corrVisualization").hidden = true;
      $("corrTextTree").hidden = false;
      $("corrTextContent").textContent = corrData.data.text || "暂无文本树";
    } else {
      $("corrVisualization").hidden = false;
      $("corrTextTree").hidden = true;
      if (corrNetwork !== corrData.data.network) {
        corrScene ??= new window.LumoMarketCorrelationScene($("corrChart"));
        corrScene.setNetwork(corrData.data.network);
        corrNetwork = corrData.data.network;
      }
    }
  }

  window.addEventListener("pagehide", event => {
    if (event.persisted) return;
    corrOverview?.destroy();
    corrScene?.destroy();
  });

  $("corrOpenSection").addEventListener("click", () => selectTab(corrSection, true));
  // 绑定关联网络事件
  $("corrRefresh")?.addEventListener("click", loadCorrelation);
  $("corrIndex")?.addEventListener("change", loadCorrelation);
  $("corrFormat")?.addEventListener("change", () => {
    if (corrData) renderCorrelation($("corrFormat").value);
  });
  $("corrExportText")?.addEventListener("click", () => {
    if (!corrData) return;
    const text = corrData.data.text || "暂无数据";
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `关联分析_${corrData.metadata.symbol}_${corrData.metadata.as_of}.txt`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  const corrIndex = $("corrIndex");
  if (Array.from(corrIndex.options).some(option => option.value === symbol)) corrIndex.value = symbol;
  renderCorrelationSelection();
  selectTab(section);load();
})();
