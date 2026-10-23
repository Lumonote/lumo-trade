/* 收盘体检：评分统一来自后端；日期、缺失与确认状态直接展示。 */
(() => {
  "use strict";
  const root = document.getElementById("marketHealthRoot");
  if (!root) return;
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num = (v, d = 1) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toLocaleString("zh-CN", {minimumFractionDigits:d, maximumFractionDigits:d});
  const pct = v => v == null ? "—" : `${num(v)}%`;
  const signed = v => v == null ? "—" : `${v > 0 ? "+" : ""}${num(v)}%`;
  const tone = v => v == null || v === 0 ? "mh-neutral" : v > 0 ? "mh-positive" : "mh-negative";
  const badge = (text, cls = "") => `<span class="mh-badge ${cls}">${esc(text)}</span>`;
  const stat = (label, value, note) => `<div class="mh-kpi"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const q = new URLSearchParams(location.search);
  const tabs = [...$("mhTabs").querySelectorAll("[data-mh-tab]")].map(b => b.dataset.mhTab);
  let section = tabs.includes(q.get("section")) ? q.get("section") : "overview";
  let symbol = q.get("index") || "sh000001", windowSize = [60,120,250].includes(Number(q.get("window"))) ? Number(q.get("window")) : 120;
  let payload = null, pollTimer = null, requestId = 0, ruleFilter = "all", ruleSearch = "", horizon = 60, autoStarted = false;

  function error(message = "") { $("mhError").hidden = !message; $("mhError").textContent = message; }
  function remember() {
    const next = new URL(location.href);
    next.searchParams.set("section", section);
    next.searchParams.set("index", symbol);
    next.searchParams.set("window", String(windowSize));
    history.replaceState(null, "", next);
  }
  function selectTab(tab, focus = false) {
    section = tabs.includes(tab) ? tab : "overview";
    for (const key of tabs) {
      const button = $(`mhTab-${key}`), active = key === section;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      $(`mhView-${key}`).hidden = !active;
      if (active && focus) button.focus();
    }
    remember();
  }
  function boxHead(title, description = "", extra = "") {
    return `<div class="mh-box-head"><div><h3>${esc(title)}</h3>${description ? `<p>${esc(description)}</p>` : ""}</div>${extra}</div>`;
  }
  function dateState(date) {
    return date && date === payload.as_of ? badge(`${date} · 同日`) : badge(date ? `${date} · 非同日，不计分` : "待补数据", "warn");
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
      <div class="mh-signal-score">${num(s.hit,0)}<span>/ ${s.total} 项触发 · ${s.known} 项可判定</span></div>
      <div class="mh-score-track" style="--position:${s.score ?? 0}%"><i></i></div>
      <div class="mh-chips">${matched.slice(0,4).map(r => `<span class="mh-chip">${esc(r.label)}</span>`).join("") || `<small>${s.known ? "暂无规则触发" : "尚无同日可用证据"}</small>`}</div>
      <p class="mh-note">${bottom ? "超卖、缩量与悲观是底部特征；反转需要价格和广度共同修复。" : "普涨、估值扩张与加速是顶部特征；过热可持续，需等待转弱验证。"}</p>
      <p class="mh-note"><button type="button" class="mh-inline-link mh-select-index" data-mh-go="signals">查看完整评分卡 →</button></p></article>`;
  }
  function renderOverview() {
    const t = payload.temperature, b = payload.breadth || {}, m = payload.market?.values || {}, fusion=payload.fusion;
    $("mhView-overview").innerHTML = `
      <div class="mh-hero-grid">
        <article class="mh-box mh-temperature">${boxHead("市场温度", "低值偏悲观，高值偏热", badge(`覆盖 ${num(t.coverage*100,0)}%`))}
          <div class="mh-temp-number">${num(t.score,0)}<span>/ 100</span></div><p class="mh-temp-label">${esc(t.label)}</p>
          <div class="mh-scale" style="--position:${t.score ?? 50}%"><i></i><i></i><i></i><i></i><i></i>${t.score == null ? "" : "<b></b>"}</div>
          <div class="mh-scale-labels"><span>极度悲观</span><span>中性</span><span>过热</span></div>
          <small>广度、动量、年线与估值共同计分。缺失权重超过40%时暂不输出温度。</small>
        </article>
        <article class="mh-box"><div class="mh-state-head"><span class="mh-eyebrow">七维综合研判</span>${badge(`证据覆盖 ${num((fusion?.coverage || 0)*100,0)}%`, fusion?.complete ? "" : "warn")}</div>
          <p class="mh-state-title">${esc(fusion?.title || payload.stage)}</p>
          <ul class="mh-state-list">${(fusion?.reasons || []).map((v,i) => `<li><b>${["价格状态","压力共振","低压力项","缺失维度"][i] || "观察"}</b><span>${esc(v)}</span></li>`).join("")}</ul>
          <p class="mh-note">综合压力 ${num(fusion?.pressure,0)}/100 · 指数/广度仅为部分证据，期指、两融、量化、资金共同形成结论。</p>
        </article>
      </div>
      <div class="mh-kpis">
        ${stat("MA250广度", pct(b.above_ma250), `${b.as_of || "未取得"} · 样本${num(b.sample,0)}只`)}
        ${stat("上涨家数占比", pct(m.up_ratio), `${num(m.up,0)} 涨 / ${num(m.down,0)} 跌 · ${payload.market?.as_of || "未取得"}`)}
        ${stat("正PB中位数", `${num(m.pb_median,3)} 倍`, `破净率 ${pct(m.below_pb1_ratio)} · ${payload.market?.as_of || "未取得"}`)}
        ${stat("小盘－大盘20日收益差", signed(payload.style_spread), "中证1000减沪深300 · 正值小盘占优")}
      </div>
      <div class="mh-grid">${signalSummary("bottom")}${signalSummary("top")}</div>
      <article class="mh-box mh-space">${boxHead("七维证据矩阵", "按实际数据日期、覆盖与权重得出结果", '<button type="button" class="mh-inline-link mh-select-index" data-mh-go="cross">期货 / 两融 / 量化详情 →</button>')}${fusionTable()}</article>
      <article class="mh-box">${boxHead("七大指数全景", "当日涨跌、阶段回撤与年线偏离；点击指数进入趋势详情", `<button type="button" class="mh-inline-link mh-select-index" data-mh-go="indices">趋势详情 →</button>`)}${indexTable(false)}</article>
      ${payload.stale ? `<div class="mh-notice">当前快照距离今天已有 ${payload.age_days} 个自然日。请更新公开数据后判断最新市场状态。</div>` : ""}`;
  }
  function fusionTable() {
    const labels={current:"同日有效",lagged:"上一交易日 · 降权",stale:"日期不匹配 · 排除",unavailable:"待补数据"};
    return `<div class="mh-table-wrap"><table class="mh-fusion-table"><thead><tr><th>维度</th><th>证据日期</th><th>压力刻度</th><th>有效权重</th><th>数据状态</th></tr></thead><tbody>${(payload.fusion?.dimensions || []).map(d => `<tr><td><strong>${esc(d.label)}</strong><small>${esc(d.source)}</small></td><td>${esc(d.as_of || "—")}</td><td>${num(d.score,0)} /100</td><td>${num(d.effective_weight*100)}% <small>设定 ${num(d.weight*100,0)}%</small></td><td>${badge(labels[d.status] || d.status,d.score == null ? "warn" : "")}</td></tr>`).join("")}</tbody></table></div>`;
  }
  function renderCross() {
    const f=payload.fusion, byKey=Object.fromEntries((f?.dimensions || []).map(d => [d.key,d]));
    const futures=byKey.futures?.details || {}, margin=byKey.leverage?.details || {}, quant=byKey.quant?.details || {}, capital=byKey.capital?.details || {};
    const latestMargin=(margin.series || []).at(-1) || {};
    const evidenceDate=key=>badge(`${byKey[key]?.as_of || "—"} · ${byKey[key]?.status === "lagged" ? "上一交易日，已降权" : byKey[key]?.status === "current" ? "同日有效" : "日期无效，未计分"}`,byKey[key]?.score == null ? "warn" : "");
    const money=v=>v == null ? "—" : `${num(v/1e8,2)} 亿`;
    const blockNote=key=>(byKey[key]?.evidence || []).map(v=>`<p class="mh-note">${esc(v)}</p>`).join("")+(byKey[key]?.details?.error ? `<p class="mh-note">接口状态：${esc(byKey[key].details.error)}</p>` : "");
    $("mhView-cross").innerHTML=`<article class="mh-box mh-space">${boxHead(f?.title || "综合证据未齐", f?.method || "",badge(`压力 ${num(f?.pressure,0)}/100`))}${fusionTable()}
      ${(f?.conflicts || []).map(v=>`<div class="mh-notice">${esc(v)}</div>`).join("")}
      <p class="mh-note">压力高表示当前风险或出清压力强，压力低仅表示该维度压力较小，不能直接转换为见底概率或买入建议。缺失证据不会被置为0或50。</p></article>
      <article class="mh-box mh-space">${boxHead("股指期货 · 基差与前20席位", "IF / IH / IC / IM；主力按成交量选择",'<a class="mh-inline-link" href="/desktop/futures">期货完整菜单 →</a>')}
        <div class="mh-table-wrap"><table><thead><tr><th>品种 / 主力</th><th>合约交易日</th><th>同日核验升贴水</th><th>同合约较上日</th><th>持仓量</th></tr></thead><tbody>${(futures.basis || []).map(r=>`<tr><td>${esc(r.variety)} / ${esc(r.contract || "—")}<small>${esc(r.source || "")}</small></td><td>${esc(r.date || "—")}</td><td class="${tone(r.basis_pct)}">${signed(r.basis_pct)}</td><td>${r.basis_change == null ? "历史接口未返回可比数据" : `${num(r.basis_change*100,1)} bp`}<small>对比 ${esc(r.previous_date || "—")}</small></td><td>${num(r.position,0)}</td></tr>`).join("") || '<tr><td colspan="5">等待现有期货接口返回行情；点击跨市场分析更新。</td></tr>'}</tbody></table></div>
        <div class="mh-table-wrap"><table><thead><tr><th>品种 / 席位日</th><th>前20多单</th><th>前20空单</th><th>净多头持仓</th><th>净多头增减</th><th>多空变化</th></tr></thead><tbody>${(futures.positions || []).map(r=>`<tr><td>${esc(r.variety)} / ${esc(r.date || "—")}</td><td>${num(r.long,0)}</td><td>${num(r.short,0)}</td><td>${num(r.net,0)}</td><td>${num(r.net_change,0)}</td><td>${esc(r.signal || "—")}</td></tr>`).join("") || '<tr><td colspan="6">尚无可用席位缓存；未披露时保留原交易日。</td></tr>'}</tbody></table></div>${blockNote("futures")}
        ${(futures.errors || []).map(v=>`<p class="mh-note">${esc(v)}</p>`).join("")}</article>
      <article class="mh-box mh-space">${boxHead("融资融券 · 杠杆周期", "沪深两所完整日合计 · 金额统一为元后展示亿元",evidenceDate("leverage"))}
        <div class="mh-kpis">${stat("融资余额",money(latestMargin.rzye),"融资杠杆存量")}${stat("融资净买入",money(latestMargin.net_buy),"融资买入额减偿还额")}${stat("融资余额5期变化",signed(margin.balance_change5),"六期完整观测的首尾比较")}${stat("融券余额",money(latestMargin.rqye),"单列观察，未机械计作看空")}</div>
        ${chart((margin.series || []).map(r=>({date:r.date,balance:r.rzye/1e8})),["balance"],"融资余额走势（亿元）")}
        <div class="mh-table-wrap"><table><thead><tr><th>日期</th><th>融资余额</th><th>买入</th><th>偿还</th><th>融资净买入</th><th>融券余额</th></tr></thead><tbody>${[...(margin.series || [])].slice(-6).reverse().map(r=>`<tr><td>${esc(r.date)}</td><td>${money(r.rzye)}</td><td>${money(r.rzmre)}</td><td>${money(r.rzche)}</td><td>${money(r.net_buy)}</td><td>${money(r.rqye)}</td></tr>`).join("") || '<tr><td colspan="6">等待现有TuShare客户端的两融权限与完整沪深数据。</td></tr>'}</tbody></table></div>${blockNote("leverage")}</article>
      <div class="mh-grid"><article class="mh-box">${boxHead("量化交易 · 方向与拥挤", quant.proxy ? "历史收盘行为代理 · 已按一半可靠度计入" : "复用量化雷达全量日榜，不用高频活跃度替代方向",'<a class="mh-inline-link" href="/desktop/capital_rankings">量化雷达 →</a>')}
        <dl class="mh-detail-list">${(quant.proxy ? [["价量资金有效交集",`${num(quant.sample,0)} 只`],["资金净流出占比",pct(quant.proxy.outflow_ratio)],["放量组 / 放量下跌流出",`${num(quant.proxy.active_sample,0)} / ${num(quant.proxy.selling_count,0)} 只`],["日内低位收盘占比",pct(quant.proxy.weak_close_ratio)]] : [["多向 / 空向异动",`${num(quant.bull,0)} / ${num(quant.bear,0)}`],["实际候选样本",num(quant.sample,0)],["疑似砸盘个股",num(quant.smash,0)],["多机制高压候选",num(quant.risk_count,0)]]).map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
        ${(quant.sectors || []).length ? `<div class="mh-table-wrap"><table><thead><tr><th>板块</th><th>群体行为</th><th>活跃代理</th></tr></thead><tbody>${quant.sectors.map(r=>`<tr><td>${esc(r.industry)}</td><td>${esc(r.collective || "—")}</td><td>${num(r.activity)}</td></tr>`).join("")}</tbody></table></div>` : ""}${blockNote("quant")}</article>
      <article class="mh-box">${boxHead("主力资金 · 承接与流出", "使用大盘资金汇总，避免以Top榜代表全市场",evidenceDate("capital"))}
        <dl class="mh-detail-list"><div><dt>近5期主力净流入</dt><dd>${money(capital.net5)}</dd></div><div><dt>近5期净流入占比均值</dt><dd>${pct(capital.rate_mean5)}</dd></div></dl>
        ${chart((capital.series || []).map(r=>({date:String(r.trade_date).replace(/^(\d{4})(\d{2})(\d{2})$/,"$1-$2-$3"),net:r.net_amount == null ? null : r.net_amount/1e8})),["net"],"大盘主力净流入（亿元）")}${blockNote("capital")}</article></div>`;
  }
  function indexTable(extended) {
    return `<div class="mh-table-wrap"><table><thead><tr><th>指数 / 日期</th><th>收盘</th><th>当日</th><th>20日</th><th>500日高点回撤</th><th>年线偏离</th><th>年量比</th>${extended ? "<th>RSI14</th><th>20日年化波动率</th><th>均线状态</th>" : ""}</tr></thead><tbody>
      ${payload.indices.map(r => `<tr class="${extended && r.symbol === symbol ? "mh-row-active" : ""}"><td><button type="button" data-mh-index="${esc(r.symbol)}" class="mh-select-index"><strong>${esc(r.name)}</strong><small>${esc(r.as_of || "未取得")}${r.as_of && r.as_of !== payload.as_of ? " · 非同日" : ""}</small></button></td>
        <td>${num(r.close,2)}</td><td class="${tone(r.ret1)}">${signed(r.ret1)}</td><td class="${tone(r.ret20)}">${signed(r.ret20)}</td><td class="${tone(r.drawdown)}">${signed(r.drawdown)}</td><td class="${tone(r.dev250)}">${signed(r.dev250)}</td><td>${num(r.volume250,2)}x</td>${extended ? `<td>${num(r.rsi14)}</td><td>${pct(r.volatility20)}</td><td>${esc(r.trend || "数据不足")}</td>` : ""}</tr>`).join("")}
      </tbody></table></div>`;
  }
  function ruleStatus(hit) {
    return hit === true ? '<span class="mh-dot yes">● <small>触发</small></span>' : hit === false ? '<span class="mh-dot">○ <small>未触发</small></span>' : '<span class="mh-dot unknown">— <small>待补</small></span>';
  }
  function ruleTable(type) {
    const all = payload.signals[type].rules;
    const filtered = all.filter(r => (ruleFilter === "all" || (ruleFilter === "hit" ? r.hit === true : ruleFilter === "miss" ? r.hit === false : r.hit == null)) && `${r.label} ${r.group} ${r.note}`.includes(ruleSearch));
    return filtered.length ? `<div class="mh-table-wrap"><table class="mh-rule-table"><thead><tr><th>状态</th><th>指标 / 依据</th><th>当前</th><th>触发阈值</th></tr></thead><tbody>${filtered.map(r => `<tr><td>${ruleStatus(r.hit)}</td><td><strong>${esc(r.label)}</strong><small>${esc(r.group)} · ${esc(r.note)}</small></td><td>${r.value == null ? "—" : `${num(r.value,r.unit === "倍" ? 3 : r.unit === "日" ? 0 : 2)}${esc(r.unit)}`}</td><td>${esc(r.op)} ${num(r.threshold,r.unit === "倍" || r.unit === "x" ? 2 : 0)}${esc(r.unit)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="mh-empty">没有符合筛选的指标</div>';
  }
  function renderSignals() {
    $("mhView-signals").innerHTML = `<div class="mh-controls"><label for="mhRuleSearch">搜索指标</label><input id="mhRuleSearch" type="search" placeholder="名称、维度或计算说明" value="${esc(ruleSearch)}"><label for="mhRuleFilter">状态</label><select id="mhRuleFilter"><option value="all">全部指标</option><option value="hit">已触发</option><option value="miss">未触发</option><option value="unknown">待补数据</option></select></div>
      <div class="mh-grid">${["bottom","top"].map(type => { const s = payload.signals[type]; return `<article class="mh-box">${boxHead(type === "bottom" ? "底部特征 · 超卖与出清" : "顶部特征 · 拥挤与加速", `${s.hit}/12触发 · ${s.known}项可判定 · 强度 ${s.score == null ? "覆盖不足" : `${num(s.score,0)}/100`}`)}<div id="mhRules-${type}">${ruleTable(type)}</div></article>`; }).join("")}</div>
      <div class="mh-notice">每张卡至少8项有效时才计算“触发数 / 有效项数 × 100”。相关指标不能当作独立投票；阈值为研究规则，未经顶底预测能力检验。原报告的放量反转项已移至独立确认区，新增RSI14，因此计数与原报告不可直接比较。</div>`;
    $("mhRuleFilter").value = ruleFilter;
    $("mhRuleSearch").addEventListener("input", e => { ruleSearch = e.target.value.trim(); updateRuleTables(); });
    $("mhRuleFilter").addEventListener("change", e => { ruleFilter = e.target.value; updateRuleTables(); });
  }
  function updateRuleTables() { for (const type of ["bottom","top"]) $(`mhRules-${type}`).innerHTML = ruleTable(type); }

  function chart(rows, keys, title) {
    const valid = rows.filter(r => keys.some(k => r[k] != null));
    if (valid.length < 2) return '<div class="mh-empty">至少需要两期有效观测才能绘制趋势</div>';
    const width = 1000, height = 310, left = 62, right = 24, top = 18, bottom = 38;
    const values = valid.flatMap(r => keys.map(k => r[k])).filter(v => v != null && Number.isFinite(v));
    let lo = Math.min(...values), hi = Math.max(...values), span = hi-lo || Math.abs(hi)*.05 || 1;
    lo -= span*.1; hi += span*.1; span = hi-lo;
    const x = i => left+i*(width-left-right)/(valid.length-1), y = v => top+(hi-v)*(height-top-bottom)/span;
    const grid = Array.from({length:5}, (_,i) => { const v = lo+span*i/4; return `<line class="mh-chart-grid" x1="${left}" x2="${width-right}" y1="${y(v)}" y2="${y(v)}"/><text x="${left-10}" y="${y(v)+4}" text-anchor="end">${num(v,Math.abs(v)>100 ? 0 : 1)}</text>`; }).join("");
    const ticks = Array.from({length:5}, (_,i) => { const at = Math.round(i*(valid.length-1)/4); return `<text x="${x(at)}" y="${height-10}" text-anchor="middle">${esc(valid[at].date)}</text>`; }).join("");
    const lines = keys.map((key, n) => {
      let started = false;
      const path = valid.map((r,i) => { if (r[key] == null) { started=false; return ""; } const point=`${started?"L":"M"}${x(i).toFixed(1)},${y(r[key]).toFixed(1)}`;started=true;return point; }).join(" ");
      return `<path class="${["mh-chart-line","mh-chart-ma","mh-chart-long"][n] || "mh-chart-line"}" d="${path}"/>`;
    }).join("");
    return `<svg class="mh-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title>${grid}${ticks}${lines}</svg>`;
  }
  function renderIndices() {
    const r = currentIndex(symbol);
    $("mhView-indices").innerHTML = `<div class="mh-controls"><label for="mhIndexSelect">指数</label><select id="mhIndexSelect">${payload.indices.map(i => `<option value="${esc(i.symbol)}">${esc(i.name)}</option>`).join("")}</select><div class="mh-segment" aria-label="趋势窗口">${[60,120,250].map(n => `<button type="button" data-mh-window="${n}" aria-pressed="${windowSize === n}">${n}交易日</button>`).join("")}</div></div>
      <article class="mh-box mh-space">${boxHead(`${r.name || "指数"} · 收盘与均线`, `${r.as_of || "未取得"} · 当前 ${num(r.close,2)} · ${r.trend || "数据不足"}`, badge(`历史 ${num(r.bars,0)} 根`))}
      <div class="mh-chart-legend"><span><i></i>收盘</span><span><i></i>MA20</span><span><i></i>MA250</span></div>
      ${chart((r.series || []).slice(-windowSize), ["close","ma20","ma250"], `${r.name}最近${windowSize}交易日走势`)}
      <p class="mh-note">${(r.series || []).slice(-windowSize)[0]?.date || "—"} 至 ${r.as_of || "—"} · 收盘口径，横轴按交易日等距。</p></article>
      <div class="mh-kpis">${stat("20日涨跌", signed(r.ret20), `60日 ${signed(r.ret60)}`)}${stat("RSI14", num(r.rsi14), "Wilder平滑 · ≤30超卖 / ≥80过热观察")}${stat("20日年化波动率", pct(r.volatility20), "对数日收益样本标准差 × √250")}${stat("当日 / 前5日均量", `${num(r.volume5,2)}x`, `当日 / 250日均量 ${num(r.volume250,2)}x`)}</div>
      <article class="mh-box mh-space">${boxHead("指数指标矩阵", "各指数保留自己的数据日期；非同日指标不合入综合评分")}${indexTable(true)}</article>
      <article class="mh-box">${boxHead("波段回撤参考位", "近500个交易日：先低后高的收盘价锚点；仅作价位观察")}
      <div class="mh-kpis">${Object.entries(r.levels || {}).map(([p,v]) => stat(`${num(Number(p)*100)}% 回撤`, num(v,1), `波段低 ${num(r.swing_low,0)} / 高 ${num(r.peak,0)}`)).join("") || '<p class="mh-empty">缺少有效波段锚点</p>'}</div>
      <p class="mh-note">参考位不是预测目标。跌破后能否收回、量价配合与广度变化，比单独触及某个点位更有意义。</p></article>`;
    $("mhIndexSelect").value = symbol;
    $("mhIndexSelect").addEventListener("change", e => { symbol=e.target.value;remember();renderIndices(); });
  }
  function renderBreadth() {
    const b = payload.breadth || {}, m = payload.market?.values || {};
    const barSpecs = [["站上MA20",b.above_ma20],["站上MA60",b.above_ma60],["站上MA250",b.above_ma250],["创60日收盘新低",b.newlow60],["创60日收盘新高",b.newhigh60]];
    const details = [["正PB中位数",`${num(m.pb_median,3)} 倍`],["正PE中位数",`${num(m.pe_median,2)} 倍`],["正PB样本破净率",pct(m.below_pb1_ratio)],["换手率中位数",pct(m.turnover_median)],["个股涨跌中位数",signed(m.chg_median)],["市值加权涨跌",signed(m.mv_weighted_chg)],["近涨停 / 近跌停",`${num(m.limit_up,0)} / ${num(m.limit_down,0)}`],["有效正PB / 正PE样本",`${num(m.pb_sample,0)} / ${num(m.pe_sample,0)}`]];
    $("mhView-breadth").innerHTML = `<div class="mh-grid"><article class="mh-box">${boxHead("价格广度", `分层样本 ${num(b.sample,0)} 只；不同指标分母可能不同`, dateState(b.as_of))}
      <div class="mh-bars">${barSpecs.map(([label,v]) => `<div class="mh-bar-row"><span>${label}</span><div class="mh-bar-track" style="--position:${Math.max(0,Math.min(100,v??0))}%"><i></i></div><strong>${pct(v)}</strong></div>`).join("")}</div>
      <p class="mh-note">个股250日高点回撤中位：${signed(b.dd52_median)}。${esc(b.note || "基于分层抽样，并非全A完整逐股统计。")}</p>
      <p class="mh-note">${Object.entries(b.denominators || {}).map(([k,v]) => `${esc(k)}：${num(v,0)}只`).join(" · ") || "原报告未保存逐指标分母；不推算精确抽样误差。"}</p></article>
      <article class="mh-box">${boxHead("估值与交易结构", `有效交易个股 ${num(m.total,0)} 只 · ${esc(payload.market?.source || "未取得")}`,dateState(payload.market?.as_of))}
      <dl class="mh-detail-list">${details.map(([k,v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
      <p class="mh-note">PE/PB仅统计正值有效样本，不能直接当作交易所加权平均估值。“近涨跌停”按板块/ST涨跌幅近似，未核验实际涨跌停价，不含新股特殊规则。</p></article></div>
      <article class="mh-box mh-space">${boxHead("广度观测记录", "完整更新回算最近5个交易日；同一窗口固定成分和分母，同日重复更新不增加连续天数")}
      <div class="mh-table-wrap"><table><thead><tr><th>日期</th><th>样本</th><th>MA20上方</th><th>MA250上方</th><th>60日新低 / 新高</th><th>高点回撤中位</th><th>数据口径 / 组号</th></tr></thead><tbody>${[...(payload.observations || [])].reverse().map(r => `<tr><td>${esc(r.as_of)}</td><td>${num(r.sample,0)}</td><td>${pct(r.above_ma20)}</td><td>${pct(r.above_ma250)}</td><td>${pct(r.newlow60)} / ${pct(r.newhigh60)}</td><td>${signed(r.dd52_median)}</td><td>${esc(r.method)}<small>${esc(r.cohort || "")}${r.backfilled ? " · 历史回算" : ""}</small></td></tr>`).join("") || '<tr><td colspan="7">尚无同日观测</td></tr>'}</tbody></table></div></article>
      <div class="mh-notice">观察“指数与个股背离”：比较大盘与小盘20日收益差、指数回撤和个股回撤中位。样本口径不同只能交叉观察，不能直接把差值解释成确定的补跌空间。</div>`;
  }
  function renderConfirmation() {
    $("mhView-confirmation").innerHTML = `<div class="mh-notice">将位置特征与趋势验证分开。以下为经验确认条件，至少3项触发后标记“条件满足”；缺失项保留待补，信号仍可能失效。跨日确认仅用已收盘交易日。</div>
      <div class="mh-grid">${["bottom","top"].map(type => { const c=payload.confirmation[type]; return `<article class="mh-box">${boxHead(type === "bottom" ? "底部反转 · 观察修复" : "顶部转弱 · 观察破位", `${c.hit}/5 项触发 · ${c.known}项可判定`, badge(c.hit >= 3 ? "条件满足" : "等待验证", c.hit >= 3 ? "" : "muted"))}
        <ul class="mh-check-list">${c.items.map(r => `<li><span class="mh-check-icon ${r.hit === true ? "yes" : r.hit == null ? "unknown" : ""}">${r.hit === true ? "✓" : r.hit == null ? "?" : "○"}</span><div><strong>${esc(r.label)} · ${r.hit === true ? "已触发" : r.hit == null ? "待补数据" : "未触发"}</strong><small>${esc(r.note)}</small></div></li>`).join("")}</ul></article>`; }).join("")}</div>
      <article class="mh-box">${boxHead("如何跟踪信号是否失效")}
        <p class="mh-note">底部修复后若指数重新跌破反转日收盘、继续创20日新低，或广度持续恶化，应重新评估。高位转弱后若价格收复MA20并伴随广度恢复，顶部判断也应重新评估。单个信号和单个支撑位都不能独立确认顶底。</p>
        <p class="mh-note">连续广度使用最近5个实际交易日、同一采样方法与固定样本组；首次完整更新即可从历史日线回算。逐日数值、实际分母及组号见广度观测记录。</p></article>`;
  }
  function renderHistory() {
    $("mhView-history").innerHTML = `<div class="mh-box mh-space">${boxHead("原报告历史条件对照", `固定研究参考 · 截止 ${payload.historical_as_of} · 2015–2026`, `<a class="mh-inline-link" href="${esc(payload.report_url)}" target="_blank" rel="noopener">阅读原报告 ↗</a>`)}
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
    $("mhJob").hidden = !job;
    if (!job) return;
    const status = {queued:"已排队",running:"更新中",finished:"更新完成",failed:"更新失败"}[job.status] || job.status;
    const detail = job.error || job.result?.notes?.join("；") || job.logs?.slice(-1)[0] || "正在准备数据";
    $("mhJob").innerHTML = `${busy ? `<progress value="${Math.max(0,Math.min(100,job.progress || 0))}" max="100"></progress>` : ""}<strong>${esc(status)}${busy ? ` ${num(job.progress || 0,0)}%` : ""}</strong><small>${esc(detail)}</small>`;
    if (busy) pollTimer=setTimeout(() => load(true),2500);
  }
  function render() {
    $("mhMeta").textContent = `基准交易日 ${payload.as_of || "未取得"} · ${payload.source === "baseline" ? "2026-10-22研究基线" : "已更新收盘快照"} · ${payload.updated_at ? `采集 ${payload.updated_at.slice(0,16).replace("T"," ")}` : "尚未执行公开数据更新"}`;
    if ($("refreshMeta")) $("refreshMeta").textContent = `体检 · ${payload.as_of || "待更新"}`;
    if (!payload.indices.some(r => r.symbol === symbol)) symbol="sh000001";
    renderOverview();renderCross();renderSignals();renderIndices();renderBreadth();renderConfirmation();renderHistory();renderMethod();
    $("mhLoading").hidden=true;root.setAttribute("aria-busy","false");$("mhExport").disabled=false;
    selectTab(section);renderJob(payload.job);
  }
  async function request(url, options = {}) {
    const response=await fetch(url, {...options, headers:{"Content-Type":"application/json",...options.headers}});
    if (!response.ok) { let message="数据读取失败，请重试";try { message=(await response.json()).error || message; } catch {} throw new Error(message); }
    return response.json();
  }
  async function load(poll = false) {
    const id=++requestId;
    try {
      const value=await request("/api/market-health");
      if (id !== requestId) return;
      const unchanged=payload && payload.as_of===value.as_of && payload.updated_at===value.updated_at && payload.fusion?.updated_at===value.fusion?.updated_at;
      payload=value;error();
      if (poll && unchanged) renderJob(value.job); else render();
      if (!autoStarted && (value.source === "baseline" || !value.fusion?.updated_at) && !value.job) { autoStarted=true;refresh("full"); }
    } catch (e) {
      error(`${e.message}。${payload ? "已有快照继续保留显示。" : "请点击顶部刷新重试。"}`);
      $("mhLoading").hidden=true;root.setAttribute("aria-busy","false");
      if (poll) pollTimer=setTimeout(() => load(true),7000);
    }
  }
  async function refresh(mode) {
    error();$("mhIndexRefresh").disabled=true;$("mhFullRefresh").disabled=true;$("mhCrossRefresh").disabled=true;
    try { const value=await request("/api/market-health/refresh",{method:"POST",body:JSON.stringify({mode})});renderJob(value.job);if (payload) payload.job=value.job; }
    catch (e) { error(e.message);$("mhIndexRefresh").disabled=false;$("mhFullRefresh").disabled=false;$("mhCrossRefresh").disabled=false; }
  }
  $("mhTabs").addEventListener("click", e => { const b=e.target.closest("[data-mh-tab]");if (b) selectTab(b.dataset.mhTab); });
  $("mhTabs").addEventListener("keydown", e => {
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return;
    e.preventDefault();const i=tabs.indexOf(section);
    selectTab(e.key === "Home" ? tabs[0] : e.key === "End" ? tabs.at(-1) : tabs[(i+(e.key === "ArrowRight" ? 1 : -1)+tabs.length)%tabs.length],true);
  });
  root.addEventListener("click", e => {
    const go=e.target.closest("[data-mh-go]"), index=e.target.closest("[data-mh-index]"), win=e.target.closest("[data-mh-window]"), h=e.target.closest("[data-mh-horizon]");
    if (go) selectTab(go.dataset.mhGo,true);
    if (index) { symbol=index.dataset.mhIndex;renderIndices();selectTab("indices",true); }
    if (win) { windowSize=Number(win.dataset.mhWindow);remember();renderIndices(); }
    if (h) { horizon=Number(h.dataset.mhHorizon);renderHistory(); }
  });
  $("mhIndexRefresh").addEventListener("click", () => refresh("indices"));
  $("mhCrossRefresh").addEventListener("click", () => refresh("cross"));
  $("mhFullRefresh").addEventListener("click", () => refresh("full"));
  // 专属页面接管共享刷新按钮，避免外壳只刷新任务列表。
  $("refreshBtn")?.addEventListener("click", e => {e.stopImmediatePropagation();load();},true);
  $("mhExport").addEventListener("click", () => {
    if (!payload) return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:"application/json;charset=utf-8"}));
    const a=document.createElement("a");a.href=url;a.download=`大盘体检_${payload.as_of || "未更新"}.json`;a.click();setTimeout(() => URL.revokeObjectURL(url),1000);
  });
  window.addEventListener("pagehide", () => clearTimeout(pollTimer));
  selectTab(section);load();
})();
