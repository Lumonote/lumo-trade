/* The 3D renderer is optional; verified monitoring codes also have a native picker. */
(() => {
  const picker=document.getElementById('particlePicker'), select=picker.querySelector('select');
  const dialog=document.getElementById('particleDialog'), host=document.getElementById('particleChart');
  let monitored=new Map(), signature, request;

  function update(stocks) {
    const next=new Map();
    for (const stock of stocks || []) {
      const id=stock.real_api_code;
      if (!/^(sh|sz|bj)\d{6}$/.test(id) || id.slice(2)!==stock.code) continue;
      next.set(id,stock.name && stock.name!=='Loading...'?`${stock.name} ${stock.code}`:stock.code);
    }
    const key=JSON.stringify([...next]);
    if (key===signature) return;
    signature=key; monitored=next;
    const last=select.value;
    select.replaceChildren(...[...next].map(([id,label])=>new Option(label,id)));
    if (!next.size) select.add(new Option('暂无监测股票',''));
    if (next.has(last)) select.value=last;
    picker.querySelector('button').disabled=!next.size;
  }

  async function open(id) {
    if (!monitored.has(id)) return;
    request?.abort();
    const controller=new AbortController(); request=controller;
    document.getElementById('particleTitle').textContent=`${monitored.get(id)} · K 线`;
    if (!dialog.open) dialog.showModal();
    host.textContent='正在读取行情…';
    try {
      const response=await fetch(`/api/stock-kline/${id.slice(2)}?limit=all`,{signal:controller.signal});
      if (!response.ok) throw new Error(`行情读取失败 (${response.status})`);
      const {data}=await response.json();
      if (dialog.open && !controller.signal.aborted) window.LumoPriceChart.mount(host,data);
    } catch (error) {if (!controller.signal.aborted && dialog.open) host.textContent=error.message;}
  }
  picker.onsubmit=e=>{e.preventDefault(); void open(select.value);};
  dialog.onclose=()=>{if(!dialog.open) request?.abort();};
  window.LumoParticleKline={update,open};
  // A blocked 3D CDN must not block this path. The scene supplies later snapshots.
  fetch('/api/snapshot').then(r=>r.ok?r.json():null).then(s=>{if(signature==null)update(s?.stocks);})
    .catch(()=>{if(signature==null)update([]);});
})();
