/* Desktop shell entry points. A preview, an open dialog and a stock analysis
   panel can each belong to a different stock. Only share the in-flight read. */
const desktopKline = {
  preview: {revision:0, data:null}, modal: {revision:0, data:null, context:null}, pending:new Map(),
};

function fetchKlineData(code) {
  const pending=desktopKline.pending;
  if (!pending.has(code)) pending.set(code,fetchJson(`/api/stock-kline/${encodeURIComponent(code)}?limit=all`)
    .then(result=>result.data || {}).finally(()=>pending.delete(code)));
  return pending.get(code);
}

function renderDesktopPrice(slot) {
  const data=slot.data, modal=slot===desktopKline.modal;
  const title=[data.name!==data.code && data.name,data.code].filter(Boolean).join(' ');
  const loadRange=range=>LumoPriceChart.stockRange(data.code,data.period || 'daily',range);
  if (!modal) {
    $('#klineTitle').textContent=`${title} K线`;
    $('#openKlineModalBtn').disabled=!data.records?.length;
    LumoPriceChart.mount($('#klineChart'),data,{loadRange,onRange(_rows,_state,_stats,active) {
      $('#klineMeta').textContent=klineMetaText(active);
      window.LumoPanic?.mount($('#klineMeta'),active.panic);
    }});
    return;
  }
  $('#klineModalTitle').textContent=`${title} 股票K线与分析`;
  LumoPriceChart.mount($('#klineModalChart'),data,{loadRange,onRange(rows, _state, stats, active) {
    $('#klineModalMeta').textContent=klineMetaText(active);
    window.LumoPanic?.mount($('#klineModalMeta'),active.panic);
    $('#klineModalSource').textContent=active.source || '—';
    const fmt=v=>v==null?'—':num(v);
    $('#klineModalRange').textContent=rows.length?`区间 ${fmt(stats.change)}${stats.change==null?'':'%'} · 高 ${fmt(stats.high)} / 低 ${fmt(stats.low)}`:'区间无行情';
    $('#klineModalVolume').textContent=`均量 ${fmt(stats.avgVolume)}`;
  }});
}

async function loadKline(code,name='') {
  const slot=desktopKline.preview, revision=++slot.revision;
  slot.data=null;
  const host=$('#klineChart');
  if (!host || !code) return;
  $('#klineTitle').textContent=`${name || ''} ${code} K线`;
  $('#klineMeta').textContent='读取中…'; $('#openKlineModalBtn').disabled=true;
  host.textContent='K线读取中…';
  try {
    const data=await fetchKlineData(code);
    if (revision!==slot.revision) return;
    slot.data={...data,code,name:data.name || name};
    renderDesktopPrice(slot);
  } catch (error) { if(revision===slot.revision){host.textContent=error.message; $('#klineMeta').textContent='读取失败';} }
}

function setKlineActions(enabled) {
  for (const id of ['klineOpportunityBtn','klineAnalysisBtn','klineTradingBtn']) $('#'+id).disabled=!enabled;
}

async function loadKlineModalContext(revision,data) {
  const slot=desktopKline.modal;
  try {
    const context=await fetchJson(`/api/stock-context/${encodeURIComponent(data.code)}?name=${encodeURIComponent(data.name || '')}`);
    if (revision!==slot.revision) return;
    slot.context=context;
    renderKlineInsight(context);
  } catch (error) {
    if (revision!==slot.revision) return;
    $('#klineStockInsight').textContent=error.message;
    $('#klineInsightStatus').textContent='股票分析失败'; $('#klineInsightStatus').className='pill bad';
  }
}

async function openStockKlineModal(code, legacyLimit, options={}) {
  if (typeof legacyLimit==='object') options=legacyLimit;
  code=normalizeStockCode(code);
  const host=$('#klineModal');
  if (!code || !host) return;
  const slot=desktopKline.modal, revision=++slot.revision, name=options.stockName || options.name || '';
  slot.data=null; slot.context=null;
  closeOtherStockModals('#klineModal'); host.hidden=false; host.setAttribute('aria-hidden','false'); syncModalOpenState();
  $('#klineModalTitle').textContent=`${name} ${code} 股票K线与分析`;
  for(const id of ['klineModalSource','klineModalVolume','klineModalRange']) $('#'+id).textContent='—';
  $('#klineModalMeta').textContent='读取中…'; $('#klineModalChart').textContent='K线读取中…';
  $('#klineInsightStatus').textContent='股票分析读取中'; $('#klineInsightStatus').className='pill warn';
  $('#klineStockInsight').textContent='正在读取股票分析…'; setKlineActions(false);
  // Context can still be useful if no candles were disclosed for this stock.
  void loadKlineModalContext(revision,{code,name});
  try {
    const data=options.preview || await fetchKlineData(code);
    if (revision!==slot.revision) return;
    slot.data={...data,code,name:data.name || name}; setKlineActions(true); renderDesktopPrice(slot);
  } catch(error) {
    if (revision!==slot.revision) return;
    $('#klineModalChart').textContent=error.message; $('#klineModalMeta').textContent='行情读取失败';
  }
}

function closeKlineModal() {
  const slot=desktopKline.modal, host=$('#klineModal');
  ++slot.revision; slot.data=null; slot.context=null;
  if (host) { host.hidden=true; host.setAttribute('aria-hidden','true'); syncModalOpenState(); }
}

async function refreshKlineLive() {
  if (!isAStockTradingHours()) return;
  await Promise.all([desktopKline.preview,desktopKline.modal].map(async slot=>{
    const {data,revision}=slot;
    if (!data?.code || (slot===desktopKline.modal && $('#klineModal').hidden)) return;
    try {
      const fresh=await fetchKlineData(data.code);
      if (slot.revision!==revision || !fresh.records?.length || slot.data!==data) return;
      slot.data={...fresh,code:data.code,name:fresh.name || data.name}; renderDesktopPrice(slot);
    } catch { /* Keep the last successful chart when a live quote times out. */ }
  }));
}

function bindKlineInteractions() {
  const modal=$('#klineModal');
  if (!modal || modal.dataset.bound) return;
  modal.dataset.bound='1';
  $('#openKlineModalBtn')?.addEventListener('click',()=>{
    const preview=desktopKline.preview.data;
    if (preview) void openStockKlineModal(preview.code,{stockName:preview.name,preview});
  });
  $('#closeKlineModalBtn').addEventListener('click',closeKlineModal);
  modal.addEventListener('click',e=>{if(e.target===modal)closeKlineModal();});
  const context=()=>{
    const {data,context}=desktopKline.modal;
    return context || (data && {stock:{code:data.code,name:data.name},quote:data.quote || {},trading_clients:{clients:[]}});
  };
  for (const [id,action] of [['klineOpportunityBtn',startStockOpportunity],['klineAnalysisBtn',startStockAnalysis]]) {
    const button=$('#'+id); button.addEventListener('click',()=>{const ctx=context(); if(ctx?.stock?.code)action(ctx,button).catch(e=>alert(e.message));});
  }
  const trading=$('#klineTradingBtn'); trading.addEventListener('click',()=>{
    const ctx=context(), client=ctx?.trading_clients?.clients?.[0];
    if (client) openTradingClientFromContext(ctx,client.id,trading);
    else prependStockContextNotice('未发现可跳转的本地交易软件，请先在个股分析里刷新交易软件。','info',trading);
  });
}
