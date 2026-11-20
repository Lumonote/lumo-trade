/* Price charts only. Fund flows and economic releases have their own units and time axes. */
(() => {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const number = v => v == null || v === '' || !Number.isFinite(+v) ? null : +v;
  const fmt = (v, digits=2) => v == null ? '—' : v.toLocaleString('zh-CN', {maximumFractionDigits:digits});
  const timeKey = v => String(v || '').replace('T',' ');
  const colors = {up:'#c94740', down:'#128467', ma5:'#857250', ma20:'#b36d08', ma60:'#267ba7', ma250:'#795ab1',
    channel:'#36857d', swing:'#8761a9', wave:'#a94486', volume:'#7c8da1', vma5:'#a76b23', ice:'#236fa2'};
  const periods = [5,10,20,60,250];
  colors.ma10='#696f9c';
  const layers = {ma:'价格均线', retest:'MA20 回踩 / 反抽', channel:'前20根支撑 / 阻力', volume:'成交量 / 均量', swing:'确认波段', trend:'高低点趋势线', wave:'候选五浪', fib:'波段回撤位', structure:'结构区间 / 事件'};
  const instances = new WeakMap();
  const freezeLine = 20; // research reference (% above MA20), not a score or a buy signal

  function prepare(records) {
    const byTime = new Map();
    for (const r of records || []) {
      const date = timeKey(r.date || r.day || r.timestamp);
      if (!date) continue;
      const bar = {date, provisional:!!r.provisional};
      for (const k of ['open','high','low','close','volume','amount']) {
        const v = number(r[k]);
        bar[k] = v == null || (k === 'volume' || k === 'amount' ? v < 0 : v <= 0) ? null : v;
      }
      bar.full = [bar.open,bar.high,bar.low,bar.close].every(v => v != null) &&
        bar.low <= Math.min(bar.open,bar.close) && Math.max(bar.open,bar.close) <= bar.high;
      byTime.set(date,bar);
    }
    const rows = [...byTime.values()].sort((a,b) => a.date < b.date ? -1 : 1);
    for (const [field, n, key] of [...periods.map(n=>['close',n,`ma${n}`]), ['volume',5,'vma5']]) {
      let sum=0, known=0;
      rows.forEach((r,i) => {
        if (r[field] != null) { sum += r[field]; known++; }
        if (i >= n && rows[i-n][field] != null) { sum -= rows[i-n][field]; known--; }
        r[key] = known === n ? sum/n : null;
      });
    }
    rows.forEach((r,i) => {
      const prev=rows[i-1];
      r.previousVolumeMean = prev?.vma5 ?? null;
      if (prev?.full && !prev.provisional && prev.ma20!=null && r.full) {
        const level=prev.ma20;
        if (prev.close>level && r.low<=level && r.close>level) r.retest={support:true,level};
        if (prev.close<level && r.high>=level && r.close<level) r.retest={support:false,level};
      }
      const prior = i >= 20 ? rows.slice(i-20,i) : [];
      if (prior.length && prior.every(b=>b.full)) {
        r.floor = Math.min(...prior.map(b=>b.low)); r.ceiling = Math.max(...prior.map(b=>b.high));
      }
    });
    return rows;
  }

  function breadthByDate(observations) {
    const days = new Map();
    for (const r of observations || []) {
      const value=number(r.above_ma20), count=number(r.denominators?.above_ma20);
      if (!r.as_of || value == null || value < 0 || value > 100 || !(count >= 200)) continue;
      const group = r.cohort && r.method ? JSON.stringify([r.method,r.cohort,!!r.provisional]) : null;
      if (!days.has(r.as_of)) days.set(r.as_of,[]);
      const raw=value*count/100, exact=Math.abs(raw-Math.round(raw))<1e-8 ? Math.round(raw) : null;
      days.get(r.as_of).push({value,count,above:number(r.above_count) ?? exact,group,cohort:r.cohort,method:r.method,
        source:r.source || (r.method?.startsWith('sina_') ? '新浪未复权日线' : r.method),sampleDate:r.sample_as_of,provisional:!!r.provisional});
    }
    return days;
  }

  function rangeStats(rows) {
    let high=null, low=null, sum=0, count=0;
    for (const r of rows) {
      if (r.full) {high=high==null?r.high:Math.max(high,r.high); low=low==null?r.low:Math.min(low,r.low);}
      if (r.volume!=null) {sum+=r.volume;count++;}
    }
    const first=rows[0]?.close, last=rows.at(-1)?.close;
    return {high,low,close:last,change:first>0 && last!=null ? (last/first-1)*100 : null,avgVolume:count?sum/count:null};
  }

  function mount(host, payload={}, options={}) {
    if (!host) return;
    const key = `${payload.symbol || payload.code || ''}:${payload.period || 'daily'}`;
    const previous = instances.get(host);
    const state = options.state || (previous?.key === key && previous.state) || {};
    previous?.observer.disconnect();
    const all = prepare(payload.records || payload.series);
    const op = payload.operation || {};
    const breadth = options.observations ? breadthByDate(options.observations) : null;
    const comparison = options.comparison;
    const minute = all.some(r=>r.date.length>10);
    const inputType = minute ? 'datetime-local' : 'date';
    const presets = [['all','全部'],['60','60根'],['120','120根'],['250','250根']];
    if (breadth?.size) presets.push(['observed','观测区间']);
    state.window ??= options.initialWindow || 'all';
    state.layers ??= {ma:true,channel:false,volume:true,swing:true,trend:false,wave:true,fib:false,structure:true};
    state.layers.retest ??= true;
    const pending=r=>r.provisional || (op.provisional && r.date===op.as_of);
    const simulated=r=>comparison?.simulated && r.date>=comparison.from;
    host.classList.add('lp-chart');
    host.innerHTML = `<div class="lp-controls"><div class="lp-presets" role="group" aria-label="行情窗口">${presets.map(([v,l])=>`<button type="button" data-window="${v}">${l}</button>`).join('')}</div>
      <form class="lp-range"><input type="${inputType}" step="1" aria-label="K线起点"><span>至</span><input type="${inputType}" step="1" aria-label="K线终点"><button type="submit">查询</button></form></div>
      <div class="lp-layers">${Object.entries(layers).map(([k,l])=>`<label><input type="checkbox" data-layer="${k}" ${state.layers[k]?'checked':''}>${l}</label>`).join('')}</div>
      <div class="lp-meta"></div><div class="lp-summary"></div><div class="lp-legend"></div><div class="lp-frame"></div><output class="lp-readout" aria-live="off"></output><div class="lp-note"></div>`;
    const find = s => host.querySelector(s);
    const frame=find('.lp-frame'), readout=find('.lp-readout'), form=find('.lp-range');
    const [from,to] = form.querySelectorAll('input');
    const observer = new ResizeObserver(() => { if (!frame.isConnected) observer.disconnect(); else if (frame.clientWidth && frame.clientWidth !== width) paint(); });
    instances.set(host,{key,state,observer});
    let rows=[], width=0, left=0, dx=0, cursor=-1, pivots=[], waves=[], events=[], fib=[], trends=[], zones=[], visibleLevels=[];
    const indices = new Map();
    const normalizeInput = v => minute ? v.slice(0,19).replace(' ','T') : v.slice(0,10);
    function select() {
      if (state.window === 'custom') {
        const lo=timeKey(state.from), hi=timeKey(state.to);
        rows = all.filter(r=>(!lo || r.date>=lo) && (!hi || r.date.slice(0,hi.length)<=hi));
      } else if (state.window === 'observed' && breadth?.size) {
        const dates=[...breadth.keys()].sort(); rows=all.filter(r=>r.date>=dates[0] && r.date<=dates.at(-1));
      } else rows = Number(state.window)>0 ? all.slice(-Number(state.window)) : all;
      from.value=state.window==='custom' ? state.from || '' : normalizeInput(rows[0]?.date || '');
      to.value=state.window==='custom' ? state.to || '' : normalizeInput(rows.at(-1)?.date || '');
      host.querySelectorAll('[data-window]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.window===String(state.window))));
      indices.clear(); rows.forEach((r,i)=>indices.set(r.date,i));
      const end=rows.at(-1)?.date || '';
      const confirmed=p=>p.confirmed_at && timeKey(p.confirmed_at)<=end;
      const known=(op.fractals || []).filter(confirmed);
      pivots=known.filter(p=>indices.has(p.date));
      waves=(op.waves || []).filter(w=>confirmed(w) && w.points.length===6 && w.points.every(p=>indices.has(p.date)));
      events=(op.events || []).filter(e=>confirmed(e) && indices.has(e.date));
      const b=known.at(-1), a=b && known.findLast(p=>p.date<b.date && p.kind!==b.kind && p.segment===b.segment);
      fib=a ? [.382,.5,.618].map(rate=>({rate,value:b.price+(a.price-b.price)*rate,asOf:b.confirmed_at})) : [];
      trends=['low','high'].map(kind=>pivots.filter(p=>p.kind===kind && p.segment===b?.segment).slice(-2))
        .filter(pair=>pair.length===2 && (pair[0].kind==='low'?pair[1].price>pair[0].price:pair[1].price<pair[0].price));
      zones=(op.zones || []).filter(z=>z.from<=end && z.to>=rows[0]?.date && (z.confirmed_at || z.to)<=end && number(z.high)>0 && number(z.low)>0);
      const since=timeKey(options.locked ? options.levelsAt : options.levelsAt || op.as_of);
      const levelDate=minute ? since : since.slice(0,10);
      const l=options.levels ?? op.levels ?? {};
      visibleLevels = levelDate && levelDate<=end ? [['触发区下沿',l.entry_low,'#3775b0'],['触发区上沿',l.entry_high,'#3775b0'],[options.locked?'锁定失效线':'结构失效线',l.stop_loss,colors.down],['第一目标区',l.target1,colors.ma20]]
        .filter(([,v])=>number(v)>0).map(([label,value,color])=>({label,value:Number(value),color,asOf:levelDate})) : [];
      cursor=rows.length-1;
      find('.lp-meta').textContent=`${rows.length ? `${rows[0].date} 至 ${end} · ` : ''}${rows.length} / ${all.length} 根${payload.source ? ' · '+payload.source : ''}`;
      const stats=rangeStats(rows);
      find('.lp-summary').textContent=rows.length ? `区间涨跌 ${stats.change==null?'—':fmt(stats.change)+'%'}　最高 ${fmt(stats.high)}　最低 ${fmt(stats.low)}　均量 ${fmt(stats.avgVolume)}` : '';
      const enabled=state.layers;
      find('.lp-legend').innerHTML = [...(enabled.ma?periods.map(n=>[`MA${n}`,colors[`ma${n}`]]):[]),
        ...(enabled.retest?[['▲ 回踩支撑',colors.ma60],['▼ 反抽遇阻',colors.ma20]]:[]),
        ...(enabled.volume?[['均量5',colors.vma5]]:[]), ...(enabled.channel?[['前20根高 / 低',colors.channel]]:[]),
        ...(enabled.swing?[['确认高低点',colors.swing]]:[]), ...(enabled.wave?[[`五浪候选 ${waves.length} 组`,colors.wave]]:[]),
        ...(enabled.trend?trends.map(([a])=>a.kind==='low'?['上升支撑',colors.down]:['下降阻力',colors.up]):[]),
        ...(enabled.fib?fib.map(f=>[`${fmt(f.rate*100,1)}% · ${fmt(f.value)}`,colors.channel]):[]),
        ...(enabled.structure?visibleLevels.map(l=>[`${l.label} ${fmt(l.value)}`,l.color]):[])].map(([l,c])=>`<span><i style="background:${c}"></i>${esc(l)}</span>`).join('');
      const n = rows.filter(r=>!r.full).length, obs = breadth && rows.filter(r=>breadth.has(r.date)).length;
      find('.lp-note').textContent=[n?`${n} 根开高低收不全或不一致，仅标可用收盘。`:'',
        rows.length && enabled.volume && !rows.some(r=>r.volume!=null)?'区间无成交量数据。':'',
        breadth?`广度有观测 ${obs} / ${rows.length} 个交易日；其中 ${rows.filter(r=>breadth.get(r.date)?.some(b=>b.value<=freezeLine && !b.provisional)).length} 日收盘触及冰点参考（≤${freezeLine}%），不代表见底。`:'',options.note || '',payload.message || '',
        enabled.wave && waves.length?`最近${waves.at(-1).direction==='up'?'上行':'下行'}五浪候选，确认于 ${waves.at(-1).confirmed_at}。`:'',
        enabled.wave && !waves.length?'区间未见完整、已确认的五浪候选。':''].filter(Boolean).join(' ');
      paint(); options.onRange?.(rows,state,stats);
    }

    function paint() {
      width=frame.clientWidth || 740;
      if (!rows.length) { frame.innerHTML='<div class="lp-empty">所选区间无行情</div>'; readout.textContent=''; return; }
      const enabled=state.layers, right=width-12, top=13, priceBottom=width<520?192:236;
      left=width<520?49:66; dx=(right-left)/rows.length;
      const x=i=>left+(i+.5)*dx, xy=(u,v)=>`${u.toFixed(2)},${v.toFixed(2)}`;
      const volumeTop=priceBottom+23, volumeBottom=enabled.volume?volumeTop+53:priceBottom;
      const iceTop=volumeBottom+35, iceBottom=iceTop+77, bottom=breadth?iceBottom:volumeBottom, height=bottom+25;
      let low=Infinity, high=-Infinity, maxVol=0;
      function include(v) { if (v != null) { low=Math.min(low,v); high=Math.max(high,v); } }
      for (const r of rows) {
        include(r.full?r.low:r.close); include(r.full?r.high:r.close);
        if (enabled.ma) periods.forEach(n=>include(r[`ma${n}`]));
        if (enabled.channel) { include(r.ceiling); include(r.floor); }
        maxVol=Math.max(maxVol,r.volume || 0,r.vma5 || 0);
      }
      if (enabled.structure) {visibleLevels.forEach(l=>include(l.value)); zones.forEach(z=>{include(z.low);include(z.high);});}
      if (enabled.fib) fib.forEach(f=>include(f.value));
      if (!Number.isFinite(low)) { low=0; high=1; }
      const pad=(high-low)*.05 || Math.abs(high)*.01 || 1; low-=pad; high+=pad;
      const y=v=>top+(high-v)/(high-low)*(priceBottom-top);
      const vy=v=>volumeBottom-v/(maxVol || 1)*(volumeBottom-volumeTop);
      const iy=v=>iceBottom-v/100*(iceBottom-iceTop);
      const path=(d,color,stroke=1,extra='')=>d?`<path d="${d}" fill="none" stroke="${color}" stroke-width="${stroke}" ${extra}/>`:'';
      const text=(xx,yy,s,anchor='start',color='currentColor')=>`<text x="${xx}" y="${yy}" text-anchor="${anchor}" fill="${color}">${esc(s)}</text>`;
      const line=(key,axis,color,dash='')=>{
        let started=false, d='';
        rows.forEach((r,i)=>{const v=r[key]; if (v==null) {started=false;return;} d+=(started?'L':'M')+xy(x(i),axis(v)); if (!started) d+='h0.1'; started=true;});
        return path(d,color,1.35,dash);
      };
      const asX=d=>d<=rows[0].date?left:d>=rows.at(-1).date?x(rows.length-1):x(rows.findIndex(r=>r.date>=d));
      let svg='';
      if (comparison) {
        const first=rows.findIndex(r=>r.date>=comparison.from);
        if (first>=0) {
          const xx=x(first)-dx/2;
          svg+=`<rect data-replay-region x="${xx}" y="${top}" width="${right-xx}" height="${bottom-top}" fill="${colors.ma60}" opacity=".08"/>`;
          svg+=path(`M${xx},${top}V${bottom}`,colors.ma60,1,'stroke-dasharray="3 3"');
        }
      }
      for (let i=0;i<4;i++) { const v=low+(high-low)*i/3; svg+=path(`M${left},${y(v)}H${right}`,'var(--lp-grid)')+text(left-5,y(v)+3,fmt(v,high>1000?0:2),'end'); }
      const stages={advance:'#c94740',decline:'#128467',base:'#267ba7',top:'#b36d08'};
      if (enabled.structure) {
        let block=null;
        const flush=()=>{if(block) svg+=`<rect x="${x(block.start)-dx/2}" y="${top}" width="${(block.end-block.start+1)*dx}" height="${priceBottom-top}" fill="${stages[block.key]}" opacity=".05"/>`;};
        for (const s of op.stage_history || []) { const i=indices.get(s.date); if (i==null || !stages[s.key]) continue;
          if (block && block.key===s.key && block.end===i-1) block.end=i; else { flush(); block={key:s.key,start:i,end:i}; } }
        flush();
        const band=(x0,x1,lo,hi,color,label)=>`<rect x="${x0}" y="${y(hi)}" width="${Math.max(1,x1-x0)}" height="${Math.max(1,y(lo)-y(hi))}" fill="${color}" fill-opacity=".09" stroke="${color}" stroke-opacity=".5" stroke-dasharray="3 3"><title>${esc(label)}</title></rect>`;
        for (const z of zones) svg+=band(asX(z.from),asX(z.to),z.low,z.high,colors.volume,z.label);
        const entry=visibleLevels.filter(l=>l.label.startsWith('触发区'));
        if (entry.length===2) svg+=band(asX(entry[0].asOf),right,entry[0].value,entry[1].value,colors.ma60,'触发区');
      }
      let wicks=['',''], bodies=['',''], volumes=['',''], closes='';
      rows.forEach((r,i)=>{
        const xx=x(i), k=r.open!=null && r.close<r.open?1:0;
        if (r.full) {
          wicks[k]+=`M${xy(xx,y(r.high))}V${y(r.low).toFixed(2)}`;
          const yy=y(Math.max(r.open,r.close)); bodies[k]+=`M${xy(xx,yy)}v${Math.max(.9,Math.abs(y(r.close)-y(r.open))).toFixed(2)}`;
        } else if (r.close!=null) closes+=`M${xy(xx-1.5,y(r.close))}h3`;
        if (enabled.volume && r.volume!=null) volumes[k]+=`M${xy(xx,vy(r.volume))}V${volumeBottom}`;
      });
      const bw=Math.max(.36,Math.min(dx*.65,13));
      [colors.up,colors.down].forEach((c,i)=>{svg+=path(wicks[i],c,Math.min(bw,1))+path(bodies[i],c,bw)+path(volumes[i],c,bw,'opacity=".62"');});
      svg+=path(closes,'currentColor',2);
      if (enabled.ma) periods.forEach(n=>{svg+=line(`ma${n}`,y,colors[`ma${n}`]);});
      if (enabled.retest) rows.forEach((r,i)=>{
        if (!r.retest || simulated(r)) return;
        const {support,level}=r.retest, color=support?colors.ma60:colors.ma20;
        const size=Math.max(1.4,Math.min(4,dx*.33)), offset=support?size:-size;
        svg+=`<path data-retest="${support?'support':'resistance'}" data-date="${esc(r.date)}" d="M${xy(x(i),y(level))}l${-size},${offset*1.6}h${2*size}Z" stroke="${color}" fill="${pending(r)?'var(--lp-bg)':color}"/>`;
      });
      if (enabled.channel) for(const k of ['floor','ceiling']) svg+=line(k,y,colors.channel,'stroke-dasharray="4 3"');
      if (enabled.volume) svg+=text(left-5,volumeTop+9,'量','end')+line('vma5',vy,colors.vma5);
      if (enabled.swing) {
        let d='', segment=null;
        for (const p of pivots) { d+=(segment===p.segment && d?'L':'M')+xy(x(indices.get(p.date)),y(p.price))+'h0.1'; segment=p.segment; }
        svg+=path(d,colors.swing,1.25,'stroke-dasharray="3 2"');
      }
      if (enabled.wave) for (const w of waves) {
        svg+=path(w.points.map((p,j)=>(j?'L':'M')+xy(x(indices.get(p.date)),y(p.price))).join(''),colors.wave,1.8);
        if (w===waves.at(-1)) w.points.forEach((p,j)=>{svg+=text(x(indices.get(p.date)),y(p.price)+(j%2===(w.direction==='up'?1:0)?-5:13),String(j),'middle',colors.wave);});
      }
      if (enabled.trend) for (const [a,b] of trends) svg+=path(`M${xy(x(indices.get(a.date)),y(a.price))}L${xy(x(indices.get(b.date)),y(b.price))}`,a.kind==='low'?colors.down:colors.up,2);
      if (enabled.fib) for (const f of fib) svg+=path(`M${asX(f.asOf)},${y(f.value)}H${right}`,colors.channel,1,'stroke-dasharray="2 3"');
      if (enabled.structure) {
        for (const l of visibleLevels) {const xx=asX(l.asOf); svg+=path(`M${xx},${y(l.value)}H${right}`,l.color,1.5,'stroke-dasharray="4 2"')+`<circle cx="${xx}" cy="${y(l.value)}" r="2.6" fill="${l.color}"/>`;}
        for (const e of events) {const xx=x(indices.get(e.date)), yy=y(e.price), d=e.kind==='risk'?-1:1;
          svg+=`<path d="M${xy(xx,yy)}l-3,${d*6}h6Z" fill="${d<0?colors.down:colors.ma60}"/>`;}
      }
      if (breadth) {
        svg+=text(left,iceTop-8,'站上 MA20 的样本占比')+text(right,iceTop-8,`冰点参考 ${freezeLine}%`,'end',colors.ice);
        for (const v of [0,freezeLine,100]) svg+=path(`M${left},${iy(v)}H${right}`,v===freezeLine?colors.ice:'var(--lp-grid)',1,'stroke-dasharray="4 3"')+text(left-5,iy(v)+3,v+'%','end');
        let prev=new Map();
        rows.forEach((r,i)=>{ const next=new Map();
          for (const b of breadth.get(r.date)||[]) {
            const xx=x(i), yy=iy(b.value), p=b.group && prev.get(b.group);
            if (p) svg+=path(`M${xy(...p)}L${xy(xx,yy)}`,colors.ice,1.7,'data-ice-segment');
            svg+=`<circle data-ice-point cx="${xx}" cy="${yy}" r="3" stroke="${colors.ice}" fill="${b.provisional?'var(--lp-bg)':colors.ice}"/>`;
            if (b.group) next.set(b.group,[xx,yy]);
          } prev=next; });
      }
      svg+=text(left,height-5,rows[0].date);
      if (rows.length>1) svg+=text(right,height-5,rows.at(-1).date,'end');
      frame.innerHTML=`<svg viewBox="0 0 ${width} ${height}" tabindex="0" role="group" aria-label="${esc(payload.name || payload.code || '指数')} K线与量价，左右键逐根查看，Home、End 查看首末根"><g>${svg}</g><path class="lp-cross" d="M0,${top} V${bottom}" stroke="currentColor" stroke-dasharray="3 3" pointer-events="none"/></svg><span class="lp-hover-date" hidden></span>`;
      inspect(cursor);
    }

    function inspect(i, active=false) {
      if (!rows.length) return;
      cursor=Math.max(0,Math.min(rows.length-1,i));
      const r=rows[cursor], enabled=state.layers;
      const cx=left+(cursor+.5)*dx, tag=frame.querySelector('.lp-hover-date');
      frame.querySelector('.lp-cross')?.setAttribute('transform',`translate(${cx} 0)`);
      if (tag) {tag.hidden=!active; if(active){tag.textContent=r.date;const half=tag.offsetWidth/2;tag.style.left=`${Math.max(half,Math.min(width-half,cx))}px`;}}
      const label=comparison ? r.date>=comparison.from ? comparison.label : '输入历史' : '';
      const parts=[label,pending(r)?'盘中待确认':'',`开 ${fmt(r.open)}`,`高 ${fmt(r.high)}`,`低 ${fmt(r.low)}`,`收 ${fmt(r.close)}`,`${label}量 ${fmt(r.volume)}`];
      const other=comparison?.other.get(r.date);
      if (other) parts.push(`${comparison.otherLabel}收 ${fmt(other.close)}`,`${comparison.otherLabel}量 ${fmt(other.volume)}`);
      if (r.amount!=null) parts.push(`额 ${fmt(r.amount)}`);
      if (enabled.ma) periods.forEach(n=>parts.push(`MA${n} ${fmt(r[`ma${n}`])}`));
      if (enabled.ma && r.ma20!=null && r.close!=null) parts.push(`${pending(r)?'现价':simulated(r)?'预测收盘':'收盘'}在 MA20 ${r.close>=r.ma20?'上方或持平':'下方'}`);
      if (enabled.retest && r.retest && !simulated(r)) parts.push(`${r.retest.support?'回踩支撑':'反抽遇阻'} · 前一根 MA20 ${fmt(r.retest.level)} · ${pending(r)?'盘中待确认':'收盘确认'}`);
      if (enabled.channel) parts.push(`前20根低 / 高 ${fmt(r.floor)} / ${fmt(r.ceiling)}`);
      if (enabled.volume) { const prev=r.previousVolumeMean; parts.push(`均量5 ${fmt(r.vma5)}`,`较前5根均量 ${prev>0 && r.volume!=null?fmt(r.volume/prev)+'倍':'—'}`); }
      if (enabled.swing) for(const p of pivots.filter(p=>p.date===r.date)) parts.push(`${p.kind==='high'?'波段高':'波段低'} ${fmt(p.price)} · 确认 ${p.confirmed_at}`);
      if (enabled.structure) for(const e of events.filter(e=>e.date===r.date)) parts.push(`${e.label} · 确认 ${e.confirmed_at}`);
      if (breadth) {const day=breadth.get(r.date); if (!day) parts.push('当日无有效广度观测');
        else day.forEach(b=>parts.push(`MA20广度 ${fmt(b.value)}% · 站上 ${fmt(b.above,0)} / 有效 ${fmt(b.count,0)} 只 · ${b.provisional?'盘中':'收盘'}`,
          `${b.source || '来源未存'} · 样本时点 ${b.sampleDate || '未记录'} · 组号 ${b.cohort || '未记录'}`));}
      readout.innerHTML=`<strong>${esc(r.date)}</strong> `+parts.filter(Boolean).map(v=>`<span>${esc(v)}</span>`).join(' ');
    }
    const point=e=>{const svg=frame.querySelector('svg'); if (!svg) return; const box=svg.getBoundingClientRect(); inspect(Math.floor(((e.clientX-box.left)*width/box.width-left)/dx),true);};
    frame.onpointermove=point; frame.onpointerdown=point;
    frame.onpointerleave=()=>{const tag=frame.querySelector('.lp-hover-date');if(tag)tag.hidden=true;};
    frame.onkeydown=e=>{const i={ArrowLeft:cursor-1,ArrowRight:cursor+1,Home:0,End:rows.length-1}[e.key]; if (i!==undefined){e.preventDefault();inspect(i,true);} };
    host.querySelectorAll('[data-window]').forEach(b=>b.onclick=()=>{state.window=b.dataset.window;to.setCustomValidity('');select();});
    host.querySelectorAll('[data-layer]').forEach(c=>c.onchange=()=>{state.layers[c.dataset.layer]=c.checked;select();});
    form.oninput=()=>to.setCustomValidity('');
    form.onsubmit=e=>{e.preventDefault(); if(from.value && to.value && from.value>to.value){to.setCustomValidity('终点不能早于起点');to.reportValidity();return;}
      state.window='custom'; state.from=from.value; state.to=to.value; select();};
    select(); observer.observe(frame);
  }
  window.LumoPriceChart={mount,rangeStats};
})();
