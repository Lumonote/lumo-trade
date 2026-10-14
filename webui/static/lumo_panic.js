/* 大盘、板块、个股共用恐慌指标展示。计算结果来自后端，前端只负责呈现。
   卖压分高位显示「恐慌 xx」，低位/平静侧镜像显示「贪婪 xx」（xx = 100 − 卖压分），中性居中。 */
(() => {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function reading(panic) {
    const p = panic || {}, score = Number(p.score);
    const available = !!p.available && p.score != null && Number.isFinite(score);
    if (!available) return {available: false, greedy: false, label: "数据不足"};
    if (p.level === "low" || p.level === "calm") {
      return {available: true, greedy: true, value: 100 - score,
              label: p.level === "calm" ? "极度贪婪" : "贪婪偏高"};
    }
    return {available: true, greedy: false, value: score, label: p.label || ""};
  }
  const titleOf = (title, greedy) => greedy ? String(title).replace("恐慌", "贪婪") : title;
  function badge(panic, title="恐慌", compact=false) {
    const p = panic || {}, r = reading(p);
    const fullLabel = r.available ? `${r.value.toFixed(1)} · ${r.label}` : "数据不足";
    const label = compact ? (r.available ? r.value.toFixed(1) : "暂无") : fullLabel;
    const status = compact ? "" : [p.quality === "snapshot" ? "单项快照" : "", p.stale ? "数据滞后" : "", p.provisional ? "实时更新" : ""].filter(Boolean).join(" · ");
    const basis = (p.factors || []).map(f => `${f.label} ${f.value}（因子分 ${f.score}）`).join("；");
    const tip = [fullLabel, p.description, basis, p.as_of, status].filter(Boolean).join("\n");
    const level = ["calm","low","neutral","high","extreme"].includes(p.level) ? p.level : "unknown";
    const shownTitle = titleOf(title, r.greedy);
    return `<span class="panic-badge panic-${level}${compact ? " panic-compact" : ""}" title="${esc(tip)}" aria-label="${esc(shownTitle+" "+fullLabel+" "+status)}">${esc(shownTitle)} <b>${esc(label)}</b>${status ? `<small>${esc(status)}</small>` : ""}</span>`;
  }
  function details(panic, title="恐慌指标") {
    const p = panic || {};
    return `<details class="panic-details"><summary>${badge(p,title)}<span>计算依据</span></summary><p>${esc(p.description || "行情数据不足，暂无法计算。")}</p><div class="panic-factors">${(p.factors || []).map(f => `<span>${esc(f.label)} <b>${esc(f.value)}</b> · 因子分 ${esc(f.score)}</span>`).join("")}</div><p>有效权重 ${Math.round((p.coverage || 0)*100)}%${p.as_of ? ` · ${esc(p.as_of)}` : ""} · 自定义卖压指标（高位显示恐慌，低位显示贪婪）</p></details>`;
  }
  function mount(host, panic, {detail=false, title="恐慌", compact=false}={}) {
    if (!host) return;
    let node = Array.from(host.children).find(e => e.dataset.panicMount === "1");
    if (!node) { node = document.createElement(["P","SPAN"].includes(host.tagName) ? "span" : "div"); node.dataset.panicMount = "1"; node.className = "panic-mount"; host.append(node); }
    const markup = detail ? details(panic,title) : badge(panic,title,compact);
    if (node.panicMarkup !== markup) { node.panicMarkup = markup; node.innerHTML = markup; }
  }
  window.LumoPanic = {badge, details, mount};

  // 原有压缩脚本保留；在既有渲染入口补上指标。
  function wrap(name, after) {
    const previous = window[name];
    if (typeof previous !== "function") return;
    window[name] = function(...args) { const result = previous.apply(this,args); after(...args); return result; };
  }
  wrap("renderStockContext", payload => mount(document.getElementById("stockContextMeta"),payload.panic));
  function decorateDashboard(payload) {
    const market = payload.market || {};
    mount(document.querySelector("#marketCornerTicker .market-corner-head"),market.panic,{title:"大盘恐慌"});
    mount(document.getElementById("marketIndexMeta"),market.panic);
    document.querySelectorAll("#indexList > .item").forEach((node,i) => mount(node,market.indices?.[i]?.panic));
    document.querySelectorAll("#topMovers [data-stock-code]").forEach(node => mount(node,market.top_movers?.find(s => s.code === node.dataset.stockCode)?.panic));
  }
  wrap("renderDashboardPage",decorateDashboard);
  wrap("renderOverview",decorateDashboard);
  wrap("drawPagedOverviewList", (host,key) => {
    if (key !== "sectorList") return;
    const rows = window.state?.overviewListPaging?.[key]?.rows || (typeof state !== "undefined" ? state.overviewListPaging?.[key]?.rows : []) || [];
    host.querySelectorAll("[data-board-code]").forEach(node => mount(node,rows.find(r => String(r.board_code || r.code || r.key) === node.dataset.boardCode)?.panic));
  });
  wrap("renderBoardDrill", payload => {
    document.querySelectorAll("#boardDrillBody [data-drill-stock]").forEach(node => mount(node,payload.stocks?.find(s => s.code === node.dataset.drillStock)?.panic));
  });
  if (typeof state !== "undefined" && state.dashboard) decorateDashboard(state.dashboard);

  // 云图使用私有状态的只读快照；页面过滤不改变全样本恐慌口径。
  const cloud = document.getElementById("marketCloudRoot");
  if (cloud) {
    let scheduled = false;
    let cachedData, cachedStocks;
    const decorate = () => {
      scheduled = false;
      const data = window.lumoMarketCloudSnapshot?.();
      if (!data) return;
      mount(cloud.querySelector(".mc-terminal-brand"),data.panic,{title:"大盘恐慌"});
      cloud.querySelectorAll(".mc-tape-index").forEach((node,i) => {
        const name = node.querySelector("span")?.textContent;
        mount(node,data.indices?.find(r => r.name === name)?.panic,{compact:true});
      });
      if (cachedData !== data) { cachedData = data; cachedStocks = new Map((data.stocks || []).map(s => [String(s.code),s])); }
      const stocks = cachedStocks;
      // 行内用紧凑徽章（仅数值，悬浮看详情），保持单行 42px 行高，避免列表被撑高、
      // 面板底边裁剪股票内容。
      cloud.querySelectorAll(".mc-panel-row").forEach(node => mount(node,stocks.get(node.dataset.stockCode)?.panic,{compact:true}));
      const header = cloud.querySelector(".mc-panel-head"), sector = header?.querySelector("span")?.textContent;
      if (header) mount(header,data.sector_panics?.[sector],{title:"板块恐慌"});
    };
    new MutationObserver(changes => {
      // 忽略本模块插入的徽章，避免重复观察自己的渲染。
      if (!changes.some(c => !c.target.closest?.("[data-panic-mount]"))) return;
      if (!scheduled) { scheduled = true; queueMicrotask(decorate); }
    }).observe(cloud,{childList:true,subtree:true});
    decorate();
  }
})();
