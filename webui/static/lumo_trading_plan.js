/* 统一计划的独立展示层，复用桌面全局的个股打开入口。 */
(() => {
  "use strict";
  const esc = value => String(value ?? "—").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[ch]);
  const fmt = value => value == null ? "—" : Number(value).toFixed(2);
  const board = document.getElementById("tradingPlanBoard");
  const stock = document.getElementById("tradingPlanStock");
  let scope = document.body.dataset.page === "watchlist" ? "watchlist" : "opportunities";
  let offset = 0;
  let boardVersion = 0;
  let stockVersion = 0;
  let boardData = null;
  let stockData = null;
  let stockCode = "";

  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function marketHtml(data) {
    const m = data.market || {};
    const indices = (m.indices || []).map(i => `<span>${esc(i.name)} · ${esc(i.pulse_label)} · 5日 ${fmt(i.chg_5d)}%</span>`).join("");
    const sectors = data.sectors || {};
    const sectorText = (sectors.fired || []).slice(0, 8).map(s => `${s.sector}${s.provisional ? "（盘中待确认）" : "（收盘触发）"}`).join("、");
    return `<div class="tp-market">
      <div><small>大盘执行档位</small><strong>${esc(m.label)}</strong><small>指数日期 ${esc(m.as_of)}${m.partial ? " · 部分指数缺失" : ""}</small></div>
      <div><small>总仓位上限 / 当前</small><strong>${fmt(m.total_cap_pct)}% / ${fmt(m.used_position_pct)}%</strong></div>
      <div><small>单票 / 板块上限</small><strong>${fmt(m.single_cap_pct)}% / ${fmt(m.sector_cap_pct)}%</strong></div>
      <div><small>可新增 / 需减仓</small><strong>${fmt(m.new_budget_pct)}% / ${fmt(m.reduce_pct)}%</strong></div>
    </div><div class="tp-indices">${indices}</div>
    <p class="tp-note">${esc(m.advice)} · 单笔账户风险预算 ${fmt(m.risk_budget_pct)}%。${esc(m.rules_note)} ${esc(m.data_note || "")}</p>
    <p class="tp-sectors">板块联动：${esc(sectorText || "暂无已验证的板块触发，等待收盘确认")}</p>`;
  }

  function planHtml(p) {
    const l = p.levels || {};
    const candidate = p.candidate || {};
    const facts = [candidate.trend_label, p.sector_plan?.label,
      `首目标盈亏比 ${fmt(l.risk_reward)}:1`, `日K ${p.data_as_of || "—"}`].filter(Boolean).join(" · ");
    return `<article class="tp-card" data-status="${esc(p.status)}">
      <div class="tp-card-head"><button type="button" class="button secondary compact" data-tp-open="${esc(p.code)}">${esc(p.name)} ${esc(p.code)}</button>
        <span class="tp-status">${esc(p.status_label)}${p.locked ? " · 已锁定" : " · 草案"}</span></div>
      <small>${esc(facts)}</small>
      <div class="tp-levels">
        <div><span>买入观察区</span><strong>${fmt(l.entry_low)} – ${fmt(l.entry_high)}</strong></div>
        <div><span>${p.stop_locked ? "锁定止损" : "拟定止损"}</span><strong>${fmt(l.stop_loss)}</strong></div>
        <div><span>第一 / 第二目标</span><strong>${fmt(l.target1)} / ${fmt(l.target2)}</strong></div>
        <div><span>新增总额度 / 当前动作额度</span><strong>${fmt(p.new_position_pct)}% / ${fmt(p.add_position_pct || p.trial_position_pct || 0)}%</strong></div>
      </div>
      <p class="tp-blockers">${(p.blockers || []).length ? `未通过：${esc(p.blockers.join("；"))}` : "入场条件已通过，仍需核算成交与费用"}</p>
      <details class="tp-detail"><summary>触发条件与退出纪律</summary>
        <p>价格 ${fmt(p.current_price)}（${p.price_source === "quote" ? `报价日期 ${esc(p.quote_date)}` : "日K参考"}）；单票最大目标 ${fmt(p.max_position_pct)}% 的模拟账户净值。${esc(l.target_basis)}</p>
        <p>${esc(p.sector_plan?.condition)}；${esc(p.sector_plan?.exit_condition)}</p>
        <ul>${[...(p.stages || []), ...(p.exits || [])].map(text => `<li>${esc(text)}</li>`).join("")}</ul>
        <p>复核期限 ${esc(p.valid_until)}。${esc(p.review_note)}</p>
      </details>
      ${p.locked && candidate.available ? `<details class="tp-detail"><summary>查看当前数据生成的重制定草案</summary><p>买入区 ${fmt(candidate.levels.entry_low)} – ${fmt(candidate.levels.entry_high)}，止损 ${fmt(candidate.levels.stop_loss)}，目标 ${fmt(candidate.levels.target1)} / ${fmt(candidate.levels.target2)}。有效至 ${esc(candidate.valid_until)}。持仓时新止损不会低于原锁定线。</p></details>` : ""}
      <div class="tp-actions"><small>${p.locked ? `锁定于 ${esc(p.locked_at)} · 版本 ${esc(p.revision)}` : "锁定后保留价位，行情刷新不下移止损"}</small>
        <button type="button" class="button secondary compact" data-tp-lock="${esc(p.code)}" ${candidate.available ? "" : "disabled"}>${p.locked ? "按当前数据重新制定" : "锁定计划"}</button></div>
    </article>`;
  }

  function render(root, data, single = false) {
    const content = root.querySelector(".tp-content");
    content.innerHTML = marketHtml(data) + `<div class="tp-list">${(data.items || []).map(planHtml).join("") || '<p class="tp-note">此范围暂无股票，可添加自选或运行机会挖掘。</p>'}</div>`
      + `<p class="tp-note">${esc(data.disclaimer)} ${single ? "" : esc(data.allocation_note)}</p>`
      + (single ? "" : `<div class="tp-page-nav"><button type="button" class="button secondary compact" data-tp-page="prev" ${data.offset ? "" : "disabled"}>上一页</button><small>${data.total ? data.offset + 1 : 0} – ${Math.min(data.offset + data.limit, data.total)} / ${data.total}</small><button type="button" class="button secondary compact" data-tp-page="next" ${data.offset + data.limit >= data.total ? "disabled" : ""}>下一页</button></div>`);
  }

  async function loadBoard() {
    if (!board) return;
    const version = ++boardVersion;
    board.querySelector(".tp-content").textContent = "正在读取操作计划…";
    board.querySelectorAll("[data-tp-scope]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.tpScope === scope)));
    try {
      const data = await request(`/api/trading-plans?scope=${scope}&offset=${offset}&limit=10`);
      if (version !== boardVersion) return;
      boardData = data;
      render(board, data);
    } catch (error) {
      if (version === boardVersion) board.querySelector(".tp-content").textContent = `计划读取失败：${error.message}`;
    }
  }

  async function loadStock(code) {
    if (!stock || !code) return;
    stockCode = code;
    const version = ++stockVersion;
    stock.querySelector(".tp-stock-status").textContent = "读取中…";
    stock.querySelector(".tp-content").textContent = "正在读取操作计划…";
    try {
      const data = await request(`/api/trading-plans?code=${encodeURIComponent(code)}`);
      if (version !== stockVersion) return;
      stockData = data;
      stock.querySelector(".tp-stock-status").textContent = data.items[0]?.status_label || "暂无计划";
      render(stock, data, true);
    } catch (error) {
      if (version !== stockVersion) return;
      stock.querySelector(".tp-stock-status").textContent = "读取失败";
      stock.querySelector(".tp-content").textContent = error.message;
    }
  }

  async function act(event, root, single) {
    const button = event.target.closest("button");
    if (!button || !root.contains(button)) return;
    if (button.dataset.tpOpen) {
      if (single) return;
      openStockContext({ stock_code: button.dataset.tpOpen }).catch(() => {});
    } else if (button.dataset.tpScope) {
      scope = button.dataset.tpScope; offset = 0; loadBoard();
    } else if (button.hasAttribute("data-tp-refresh")) {
      loadBoard();
    } else if (button.dataset.tpPage) {
      offset = Math.max(0, offset + (button.dataset.tpPage === "next" ? 10 : -10)); loadBoard();
    } else if (button.dataset.tpLock) {
      const p = (single ? stockData : boardData)?.items.find(p => p.code === button.dataset.tpLock);
      if (!p) return;
      button.disabled = true;
      try {
        await request("/api/trading-plans/lock", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: p.code, expected_revision: p.revision, basis_key: p.candidate.basis_key }) });
        if (single) await loadStock(stockCode); else await loadBoard();
      } catch (error) {
        button.disabled = false;
        const note = root.querySelector(".tp-note");
        if (note) note.textContent = `锁定失败：${error.message}`;
      }
    }
  }

  if (board) {
    board.addEventListener("click", event => act(event, board, false));
    loadBoard();
  }
  if (stock) {
    stock.addEventListener("click", event => act(event, stock, true));
    const previousOpen = window.openStockContext;
    window.openStockContext = function(context) {
      const pending = previousOpen(context);
      loadStock(state.currentStockCode);
      return pending;
    };
    // 原风控列的无条件摊平方案由统一计划取代；保留深度信号与隐性风险列。
    const previousRisk = window.renderSuiteRiskControl;
    window.renderSuiteRiskControl = function(payload) {
      previousRisk(payload);
      const pane = document.getElementById("suitePaneRiskControl");
      const oldExecution = pane?.querySelector(".suite-risk-col");
      if (oldExecution) oldExecution.remove();
      const note = document.createElement("p");
      note.className = "tp-note";
      note.textContent = "买卖触发条件、仓位与锁定止损见上方「统一买卖计划」。";
      pane?.prepend(note);
      stock.open = true;
    };
    stock.addEventListener("toggle", () => {
      if (stock.open && state.currentStockCode && stockCode !== state.currentStockCode) loadStock(state.currentStockCode);
    });
  }
  setInterval(() => {
    if (document.hidden) return;
    if (board) loadBoard();
    if (stock && !document.getElementById("stockContextModal")?.hidden && stockCode) loadStock(stockCode);
  }, 60000);
})();
