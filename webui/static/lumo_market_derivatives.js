/* 衍生品只展示后端给出的观测与方向；缺失、对冲和日期边界单独说明。 */
(() => {
  "use strict";
  const varieties = [["IF", "沪深300"], ["IH", "上证50"], ["IC", "中证500"], ["IM", "中证1000"]];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const numeric = value => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const num = (value, digits = 0) => numeric(value) ? Number(value).toLocaleString("zh-CN", {minimumFractionDigits:digits, maximumFractionDigits:digits}) : "—";
  const signed = (value, digits = 0) => numeric(value) ? `${Number(value) > 0 ? "+" : ""}${num(value,digits)}` : "—";
  const unit = (value, suffix, digits = 0, sign = false) => numeric(value) ? `${sign ? signed(value,digits) : num(value,digits)}${suffix}` : "—";
  const date = value => value ? esc(window.LumoDate?.date(value) || value) : "未取得";
  const stamp = value => value ? esc(window.LumoDate?.format(value) || value) : "未取得";
  const direction = value => ["bullish","bearish","neutral","unavailable"].includes(value) ? value : "unavailable";
  const labels = {bullish:"偏多",bearish:"偏空",neutral:"中性",unavailable:"证据不足"};
  const status = value => ({current:"观察日同日",lagged:"上一交易日",stale:"历史观测 · 不计方向",unavailable:"尚无有效数据"}[value] || "尚无有效数据");
  const changeTone = (value, short = false) => !numeric(value) || Number(value) === 0 ? "neutral" : (Number(value) > 0) !== short ? "bullish" : "bearish";
  const metric = (label, value, note = "") => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd>${note ? `<small>${esc(note)}</small>` : ""}</div>`;

  function positionBar(row) {
    const share = Number(row.long_share_pct);
    if (!numeric(row.long_share_pct) || share < 0 || share > 100 || !numeric(row.long) || !numeric(row.short) || Number(row.long) + Number(row.short) <= 0) {
      return '<div class="md-position-empty">持仓覆盖不足，暂不绘制多空占比</div>';
    }
    return `<div class="md-position-bar" role="img" aria-label="所列席位多单占比 ${num(share,1)}%，空单占比 ${num(100-share,1)}%"><i style="width:${share}%"></i><i style="width:${100-share}%"></i></div>`;
  }

  function productCard(row) {
    const tone = direction(row.direction);
    const observed = row.position_status === "current" || row.position_status === "lagged";
    const basisChange = numeric(row.basis_change) ? unit(Number(row.basis_change)*100," bp",1,true) : "暂无可比前日";
    return `<article class="md-product md-${tone}" data-md-product="${esc(row.variety)}">
      <header class="md-product-head"><div><span class="md-contract-code">${esc(row.variety)}</span><h4>${esc(row.name)}</h4></div><span class="md-direction md-${tone}">${esc(row.direction_label || labels[tone])}</span></header>
      <p class="md-observation">席位 ${date(row.position_date)}<span class="md-freshness ${observed ? "" : "md-old"}">${esc(status(row.position_status))}</span></p>
      <div class="md-change-heading"><span>席位净变化 <small>手</small></span><strong class="md-${tone}">${signed(row.net_change)}</strong><span class="md-action">${esc(row.action_label || "等待完整增减仓数据")}</span></div>
      <p class="md-reason">${esc(row.reason || "更新公开数据后，按多单与空单增减观察方向。")}</p>
      <div class="md-position-labels"><span>多单 <b>${num(row.long)}</b></span><span>空单 <b>${num(row.short)}</b></span></div>
      ${positionBar(row)}
      <div class="md-changes"><div><span>多单增减</span><strong class="md-${changeTone(row.long_change)}">${signed(row.long_change)}<small> 手</small></strong></div><div><span>空单增减</span><strong class="md-${changeTone(row.short_change,true)}">${signed(row.short_change)}<small> 手</small></strong></div></div>
      <dl class="md-basis">${metric("同日升贴水率",unit(row.basis_pct,"%",2,true),"期货 / 现货 − 1")}${metric("同合约基差变化",basisChange,row.previous_date ? `对比 ${row.previous_date}` : "需相邻交易日")}</dl>
      <div class="md-price-position"><span>价仓配合</span><strong>${esc(row.price_position_label || "暂无可比价仓数据")}</strong></div>
      <details class="md-detail"><summary>行情与持仓明细</summary><dl>
        ${metric("主力合约",row.contract || "未取得")}${metric("期货价格",num(row.price,2))}
        ${metric("同合约价格变化",unit(row.price_change_pct,"%",2,true),"较前一交易日观测")}${metric("总持仓",unit(row.open_interest," 手"))}
        ${metric("总持仓变化",unit(row.oi_change," 手",0,true))}${metric("席位净持仓",unit(row.net," 手",0,true))}
        ${metric("席位多空比",num(row.ls_ratio,3))}${metric("多单占所列席位",unit(row.long_share_pct,"%",1))}
      </dl><p>净持仓为所列多单减空单；持仓量与持仓变化均不等于交易资金金额。</p></details>
      <footer class="md-product-source"><span>行情 ${date(row.quote_date)} · ${esc(row.contract || "合约待取得")}</span><span>${esc(status(row.quote_status))}${row.quote_at ? ` · ${stamp(row.quote_at)}` : ""}</span><span>${esc(row.source || "现有股指期货服务 / 中金所席位数据")}</span></footer>
    </article>`;
  }

  function render(report) {
    const data = report || {}, summary = data.summary || {}, tone = direction(summary.tone);
    const products = Array.isArray(data.products) ? data.products : [];
    const rows = varieties.map(([variety,name]) => ({variety,name,...products.find(row => row.variety === variety)}));
    const notes = [...new Set((Array.isArray(data.notes) ? data.notes : []).filter(Boolean))];
    const errors = [...new Set((Array.isArray(data.errors) ? data.errors : []).filter(Boolean))];
    return `<div class="md-root">
      <header class="mh-box-head md-heading"><div><p class="md-eyebrow">金融衍生品分析 · 股指期货</p><h3>多空正在怎样变化</h3><p>观察日 ${date(data.as_of)} · IF / IH / IC / IM · 各合约前20席位汇总</p></div><div class="mh-actions"><button type="button" class="button" data-mh-derivatives-refresh>更新衍生品分析</button><a class="button secondary" href="/desktop/futures">完整期货行情 ↗</a></div></header>
      <section class="md-summary md-${tone}" aria-label="衍生品多空概览"><div class="md-summary-copy"><span class="md-eyebrow">席位变化方向</span><h4>${esc(summary.label || "等待衍生品数据")}</h4><p>${esc(summary.reason || "尚无足够的同日席位证据，请更新衍生品分析。")}</p><small>${summary.date ? `比较席位日 ${date(summary.date)}` : "同日有效证据不足"} · ${num(summary.eligible)} / ${num(summary.total ?? 4)} 个品种纳入比较</small></div><div class="md-counts" aria-label="同日有效品种分布">${[["bullish","偏多",summary.bullish],["bearish","偏空",summary.bearish],["neutral","中性",summary.neutral],["unavailable","未纳入",summary.missing]].map(([key,label,count])=>`<div class="md-${key}"><strong>${num(count)}</strong><span>${label}</span></div>`).join("")}</div></section>
      <p class="md-reading-guide"><span>先看增减，再看存量。</span> 偏多 / 偏空描述席位净变化；各合约前20席位可能包含套保，不代表全市场裸多或裸空。异日数据分别展示。</p>
      ${errors.length ? `<div class="mh-notice" role="status">${errors.map(value=>`<p>${esc(value)}</p>`).join("")}</div>` : ""}
      <div class="md-products">${rows.map(productCard).join("")}</div>
      ${notes.length ? `<div class="md-notes">${notes.map(value=>`<p>${esc(value)}</p>`).join("")}</div>` : ""}
      <details class="mh-box mh-method md-method"><summary>怎么看这些数据 · 后续可补充</summary><div class="md-method-grid"><div><h4>当前判断口径</h4><p>席位净变化 = 多单增减 − 空单增减。只比较同一数据日的有效品种；旧日期与缺失不补零。升贴水使用已核验的同日期货与现货，换月时不跨合约计算基差变化。</p></div><div><h4>期权情绪</h4><p>后续可接入认沽 / 认购比（PCR）、隐含波动率与偏斜，区分保护需求和方向交易。当前没有接入期权数据。</p></div><div><h4>期限与交割</h4><p>后续可补充近远月价差、年化基差及到期倒计时，帮助辨别移仓和临近交割的影响。当前未据此计算多空方向。</p></div></div><button type="button" class="mh-inline-link mh-select-index" data-mh-go="cross">结合跨市场证据查看 →</button></details>
    </div>`;
  }
  window.LumoMarketDerivatives = {render};
})();
