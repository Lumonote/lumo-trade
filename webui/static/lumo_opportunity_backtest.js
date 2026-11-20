/* 机会挖掘工作台；使用桌面共用的排序、任务轮询和股票详情。 */
async function renderOpportunityPatternBacktest() {
  const body = $("#opportunityDataBody");
  if (!body) return;
  const view = state.opportunityData;
  const runId = state.opportunityCanvas.activeRunId || null;
  const mode = view.patternMode || "run_date";
  const modeLabel = mode === "latest" ? "最新行情重评" : "按批次日回测";
  const request = view.patternRequest = (view.patternRequest || 0) + 1;
  const current = () => view.patternRequest === request && view.tab === "pattern" &&
    (state.opportunityCanvas.activeRunId || null) === runId;
  const cached = view.patternResult;
  let result = runId && cached?.mode === mode && String(cached.run.id) === String(runId) ? cached : null;

  function draw(message = "", busy = false) {
    const controls = `<div class="opp-no-filter"><label>行情口径 <select class="opp-pattern-mode">
      <option value="run_date" ${mode === "run_date" ? "selected" : ""}>按批次日回测</option>
      <option value="latest" ${mode === "latest" ? "selected" : ""}>最新行情重评</option></select></label>
      <button type="button" class="button compact opp-pattern-run" ${busy ? "disabled" : ""}>${busy ? "回测中…" : result ? "重新回测" : "运行形态回测"}</button></div>`;
    opportunityDataSummary(result ? `${modeLabel} · 批次 ${result.run.date} · 截止 ${result.cutoff} · 实际行情最晚 ${result.data_as_of || "无有效日线"} · 扫描 ${result.scanned}/${result.total} 只` : `${modeLabel} · ${runId ? `批次 #${runId}` : "最近一次挖掘批次"}`);
    body.innerHTML = controls + (message ? `<p role="status" class="item-meta opp-no-filter">${html(message)}</p>` : "") +
      '<p class="item-meta opp-no-filter">形态评分取该股历史相似片段的 10 日胜率，收益取这些片段的 10 日平均收益。</p>';
    body.querySelector(".opp-pattern-mode").onchange = e => {
      view.patternMode = e.target.value;
      renderOpportunityPatternBacktest();
    };
    body.querySelector(".opp-pattern-run").onclick = run;
    if (!result) return;
    body.insertAdjacentHTML("beforeend", sortableTableHtml(result.rows, {
      sortState: view.sortPattern, tableClass: "sortable-data-table opportunity-pattern-table", emptyText: "无回测结果。",
      rowAttrs: r => `data-stock="${html(r.code)}" data-stock-code="${html(r.code)}" data-stock-name="${html(r.name)}" data-search="${html(r.name)} ${html(r.code)}" role="button" tabindex="0"`,
      columns: [
        {key:"name", label:"名称·代码", className:"col-left", cell:r=>`<span class="cell-strong">${html(r.name || r.code)}</span> <span class="cell-sub">${html(r.code)}</span>`, value:r=>r.name || r.code},
        {key:"total_score", label:"综合分", cell:r=>r.total_score == null ? "—" : num(r.total_score), value:r=>r.total_score},
        {key:"rating", label:"评级", cell:r=>html(r.rating || "—"), value:r=>r.rating},
        {key:"data_as_of", label:"实际行情截至", cell:r=>html(r.data_as_of || "无有效日线"), value:r=>r.data_as_of},
        {key:"bar_count", label:"有效日线", cell:r=>num(r.bar_count,0), value:r=>r.bar_count},
        {key:"pattern_score", label:"形态评分", cell:r=>r.pattern_score == null ? html(r.note || "—") : num(r.pattern_score,1), value:r=>r.pattern_score},
        {key:"win_rate", label:"10日胜率", cell:r=>r.win_rate == null ? "—" : `${num(100*r.win_rate,1)}%`, value:r=>r.win_rate},
        {key:"avg_return", label:"10日平均收益", cell:r=>r.avg_return == null ? "—" : `<span class="${changeClass(r.avg_return)}">${num(100*r.avg_return,2)}%</span>`, value:r=>r.avg_return},
        {key:"sample_count", label:"样本数", cell:r=>num(r.sample_count,0), value:r=>r.sample_count}
      ]
    }));
    bindSortableHeaders(body.querySelector("table"), view.sortPattern, () => draw());
    bindOpportunityDataStockClicks(body);
    applyOpportunityDataSearch();
  }

  async function run() {
    result = null;
    draw("正在读取 Top 20 股票的有效日线…", true);
    try {
      const task = await fetchJson("/api/opportunity/pattern-backtest", {method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({run_id:runId ? Number(runId) : undefined, top_n:20, mode})});
      if (!current()) return;
      const id = task.job_id || task.job?.id;
      if (!id) throw Error("回测任务创建失败");
      const job = await pollJob(id, {intervalMs:2000, maxAttempts:90, onUpdate:j=>{
        if (current() && latestJobLog(j)) opportunityDataSummary(latestJobLog(j));
      }});
      if (!current()) return;
      if (job?.status !== "finished" || !job.result?.ok) throw Error(job?.error || "未取得回测结果，请重试");
      if (job.result.mode !== mode || (runId && String(job.result.run.id) !== String(runId))) throw Error("批次或行情口径不匹配");
      result = view.patternResult = job.result;
      draw();
    } catch (error) {
      if (current()) draw(`形态回测失败：${error.message}`);
    }
  }
  draw();
}
