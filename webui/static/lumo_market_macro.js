/* 宏观描述研究；原始观测日期与采集时间分别显示，未知值不作零。 */
(() => {
  "use strict";
  const root=document.getElementById("mhView-macro");if(!root)return;
  const chartDate=window.LumoMarketChartDate;
  const $=id=>document.getElementById(id),esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num=(v,d=2)=>v==null||!Number.isFinite(Number(v))?"—":Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
  const pct=v=>v==null?"—":`${v>0?"+":""}${num(v)}%`,tone=v=>v>0?"mh-positive":v<0?"mh-negative":"mh-neutral";
  const empty=message=>`<p class="mm-unavailable">${esc(message)}</p>`;
  const head=(title,note="")=>`<div class="mh-box-head"><div><h3>${esc(title)}</h3><p>${esc(note)}</p></div></div>`;
  const stat=(title,value,note="")=>`<div class="mm-stat"><span>${esc(title)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const link=(url,label)=>/^https:\/\//.test(url||"")?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`:esc(label);
  const state={data:null,id:0,poll:null,started:false,sort:new Map(),tables:new Map(),factors:[],candle:null,candles:[],candleWidth:0};
  const selected=()=>({equity:$("mmEquity").value,factor:$("mmFactor").value,frequency:$("mmFrequency").value,window:$("mmWindow").value,lag:$("mmLag").value,category:$("mmCategory").value,keyword:$("mmKeyword").value.trim()});
  function error(message=""){$("mmError").hidden=!message;$("mmError").textContent=message;}
  async function api(url,opts={}){const response=await fetch(url,{...opts,headers:{"Content-Type":"application/json"}});const body=await response.json();if(!response.ok)throw new Error(body.error||"宏观研究读取失败");return body;}
  function download(name,content,type="application/json;charset=utf-8"){const url=URL.createObjectURL(new Blob([content],{type}));const a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function orderedRows(id,columns,rows){const sort=state.sort.get(id),ordered=[...rows];if(sort)ordered.sort((a,b)=>{const key=columns[sort.index][1],x=typeof key==="function"?key(a):a[key],y=typeof key==="function"?key(b):b[key];if(x==null||y==null)return x==null?y==null?0:1:-1;return (typeof x==="number"&&typeof y==="number"?x-y:String(x).localeCompare(String(y),"zh-CN",{numeric:true}))*(sort.asc?1:-1);});return ordered;}
  function table(id,columns,rows){state.tables.set(id,{columns,rows});const sort=state.sort.get(id),ordered=orderedRows(id,columns,rows);
    return `<div class="mm-table" data-mm-table="${id}"><table><thead><tr>${columns.map(([label],i)=>`<th scope="col" aria-sort="${sort?.index===i?sort.asc?"ascending":"descending":"none"}"><button type="button" class="mm-sort" data-mm-sort="${id}" data-mm-column="${i}">${esc(label)}<span aria-hidden="true">${sort?.index===i?sort.asc?"↑":"↓":"↕"}</span></button></th>`).join("")}</tr></thead><tbody>${ordered.map(r=>`<tr>${columns.map(([,key,render])=>`<td>${render?render(r):esc(typeof key==="function"?key(r):r[key])}</td>`).join("")}</tr>`).join("")||`<tr><td colspan="${columns.length}">${empty("当前筛选没有有效记录；原日期保留，可调整条件或更新公开数据。")}</td></tr>`}</tbody></table></div>`;
  }
  function spark(values){if(values.length<2)return "";const lo=Math.min(...values),span=Math.max(...values)-lo||1;return `<svg viewBox="0 0 180 24" aria-hidden="true"><path d="${values.map((v,i)=>`${i?"L":"M"}${i*180/(values.length-1)},${22-(v-lo)/span*20}`).join(" ")}" fill="none" stroke="#8aa385" stroke-width="1.5"/></svg>`;}
  function lineChart(groups,{dual=false,normalized=false}={}){
    const valid=groups.filter(s=>s.rows.length>1);if(!valid.length)return empty("历史观测不足，尚不能绘图。");
    const dates=valid.flatMap(s=>s.rows.map(r=>Date.parse(r.date))),first=Math.min(...dates),last=Math.max(...dates);
    const layout={width:680,height:258,left:58,right:608,top:30,bottom:213};
    const colors=["#397b64","#bb9153","#8a98a7"],x=d=>layout.left+(Date.parse(d)-first)/(last-first||1)*(layout.right-layout.left);
    const transformed=valid.map(s=>({...s,rows:s.rows.map(r=>({...r,value:normalized?r.value/s.rows[0].value*100:r.value}))}));
    const all=transformed.flatMap(s=>s.rows.map(r=>r.value)),byDate=new Map();
    let svg="";
    for(let i=0;i<4;i++)svg+=`<line class="grid" x1="58" x2="608" y1="${30+i*61}" y2="${30+i*61}"/>`;
    transformed.forEach((s,j)=>{const values=dual?s.rows.map(r=>r.value):all;const lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1,y=v=>213-(v-lo)/span*183;
      if(j===0||dual)for(let i=0;i<4;i++)svg+=`<text x="${j===0?50:616}" y="${34+i*61}" text-anchor="${j===0?"end":"start"}">${num(hi-i*span/3,Math.abs(hi)>100?0:2)}</text>`;
      svg+=`<path d="${s.rows.map((r,i)=>`${i?"L":"M"}${x(r.date)},${y(r.value)}`).join(" ")}" fill="none" stroke="${colors[j]}" stroke-width="2"/>`;
      const stride=Math.max(1,Math.floor(s.rows.length/100));svg+=s.rows.filter((r,i)=>i%stride===0||i===s.rows.length-1).map(r=>`<circle cx="${x(r.date)}" cy="${y(r.value)}" r="3" fill="${colors[j]}" opacity="0.2"/>`).join("");
      for(const {date,value} of s.rows){
        if(!byDate.has(date))byDate.set(date,[]);
        byDate.get(date).push(`${s.name}：${num(value)}`);
      }
    });
    svg+=`<text class="mh-date-tick" x="58" y="245">${new Date(first).toISOString().slice(0,10)}</text><text class="mh-date-tick" x="608" y="245" text-anchor="end">${new Date(last).toISOString().slice(0,10)}</text>`;
    const points=[...byDate].map(([date,values])=>({x:x(date),date,title:[date,...values].join(" · ")})).sort((a,b)=>a.x-b.x);
    return `<div class="mm-legend">${valid.map((s,i)=>`<span><i style="background:${colors[i]}"></i>${esc(s.name)}${dual?i===0?" · 左轴":" · 右轴":""}${normalized?" · 起点100":""}</span>`).join("")}</div>`+
      chartDate.render("mm-chart",`${valid.map(s=>s.name).join("与")}历史走势`,layout,svg+chartDate.hits(points,layout));
  }
  function scatter(r){if(!r.pairs.length)return empty(r.status);const xvals=r.pairs.map(p=>p.x),yvals=r.pairs.map(p=>p.y),xmin=Math.min(...xvals),xspan=Math.max(...xvals)-xmin||1,ymin=Math.min(...yvals),yspan=Math.max(...yvals)-ymin||1;
    return `<svg class="mm-chart" viewBox="0 0 680 258" role="img" aria-label="宏观因子变化与指数收益散点图">${[0,1,2,3].map(i=>`<line class="grid" x1="58" x2="608" y1="${30+i*58}" y2="${30+i*58}"/><text x="50" y="${34+i*58}" text-anchor="end">${num(ymin+yspan-i*yspan/3)}</text>`).join("")}${r.pairs.map(p=>`<circle cx="${58+(p.x-xmin)/xspan*550}" cy="${204-(p.y-ymin)/yspan*174}" r="3" fill="#397b64" opacity=".45"><title>${p.date}；因子日${p.factor_date}；X ${num(p.x)}；Y ${num(p.y)}</title></circle>`).join("")}<text x="58" y="225">${num(xmin)}</text><text x="608" y="225" text-anchor="end">${num(xmin+xspan)}</text><text x="333" y="250" text-anchor="middle">X：${esc(r.x_unit)} / Y：${esc(r.y_unit)}</text></svg>`;
  }
  function factors(){const frequency=$("mmFrequency").value,previous=$("mmFactor").value;const rows=state.factors.filter(r=>r.frequency!=="quarterly"&&(frequency==="monthly"||r.frequency==="daily"));$("mmFactor").innerHTML=rows.map(r=>`<option value="${r.key}">${esc(r.name)}</option>`).join("");$("mmFactor").value=rows.some(r=>r.key===previous)?previous:frequency==="monthly"?"m2":"real10y";}
  function frequencyChanged(){const monthly=$("mmFrequency").value==="monthly";$("mmWindow").innerHTML=(monthly?[24,36,60,120]:[60,120,252,756]).map(n=>`<option value="${n}" ${n===(monthly?60:252)?"selected":""}>${n}${monthly?"月":"交易日"}</option>`).join("");$("mmLag").innerHTML=(monthly?[0,1,3,6]:[0,1,5,20]).map(n=>`<option value="${n}">${n?`领先${n}${monthly?"个月":"交易日"}`:"同期变化"}</option>`).join("");factors();}
  function renderValuationComparison(d){
    const v=d.valuation_comparison, rank=n=>n==null?"—":`${num(n,1)}%`;
    $("mmValuationComparison").innerHTML=head("五指数股债利差与低估对比", `股债利差 = 100 ÷ PE TTM − 同日中国10年期国债收益率（%）。以各指数配对日为基准，回看${v.years}年（不含当日）。PE分位 ≤${v.pe_max}% 且利差分位 ≥${v.spread_min}% 标为历史低估；少于${v.min_samples}个历史样本则不可判定。`)+table("valuation-comparison",[
      ["指数","name",r=>`${esc(r.name)}<small>${esc(r.code)}${r.key===d.equity?" · 研究指数":""}</small>`],
      ["判定","status",r=>`<span${r.low===true?' class="mm-low-mark"':""}>${esc(r.status)}</span>`],
      ["配对日","date",r=>esc(r.date||"—")],["PE TTM（倍）","pe",r=>num(r.pe)],
      ["股债利差（百分点）","spread",r=>num(r.spread)],["10年国债（%）","cn10y",r=>num(r.cn10y)],
      ["PE历史分位","pe_percentile",r=>rank(r.pe_percentile)],["利差历史分位","spread_percentile",r=>rank(r.spread_percentile)],
      ["历史样本","samples",r=>num(r.samples,0)],
    ],v.rows);
  }
  function candlePeriod(date,bucket){
    if(bucket==="quarter")return `${date.slice(0,4)}-Q${Math.ceil(Number(date.slice(5,7))/3)}`;
    if(bucket==="month")return date.slice(0,7);
    const day=new Date(date);day.setUTCDate(day.getUTCDate()-(day.getUTCDay()+6)%7);
    return day.toISOString().slice(0,10);
  }
  function makeCandles(rows,{start,end,bucket}){
    const bars=[];
    for(const {date,value} of rows){
      if(date<start||date>end)continue;
      const period=candlePeriod(date,bucket),bar=bars.at(-1);
      if(bar?.period===period){bar.end=date;bar.high=Math.max(bar.high,value);bar.low=Math.min(bar.low,value);bar.close=value;bar.n++;}
      else bars.push({period,start:date,end:date,open:value,high:value,low:value,close:value,n:1});
    }
    return bars;
  }
  const candleDate=({start,end})=>start===end?start:`${start} 至 ${end}`;
  function candleDescription(bar){
    const {open,high,low,close,n}=bar;
    const d=state.data.factor==="usdcny"?4:2;
    return `${candleDate(bar)} · 开 ${num(open,d)}　高 ${num(high,d)}　低 ${num(low,d)}　收 ${num(close,d)} · ${n} 条观测`;
  }
  function renderCandlePlot(){
    const box=$("mmCandlePlot"),bars=state.candles;
    if(!bars.length)return;
    const width=box.clientWidth||680,left=60,right=width-12,top=16,bottom=186;
    state.candleWidth=box.clientWidth;
    const lo=Math.min(...bars.map(b=>b.low)),hi=Math.max(...bars.map(b=>b.high)),pad=(hi-lo)*.08||Math.max(.01,Math.abs(hi)*.005);
    const max=hi+pad,span=hi-lo+2*pad,y=v=>top+(max-v)/span*(bottom-top),slot=(right-left)/bars.length,w=Math.min(16,slot*.62);
    const digits=state.data.factor==="usdcny"?4:span<.08?3:Math.abs(max)>=100?0:2;
    const grid=Array.from({length:4},(_,i)=>{const at=top+i*(bottom-top)/3;return `<line class="grid" x1="${left}" x2="${right}" y1="${at}" y2="${at}"/><text x="${left-7}" y="${at+4}" text-anchor="end">${num(max-i*span/3,digits)}</text>`;}).join("");
    const marks=bars.map((b,i)=>{
      const x=left+(i+.5)*slot,up=b.close>b.open,flat=b.close===b.open,color=flat?"#64748b":up?"#ad4f50":"#397b64";
      return `<g data-mm-candle="${i}" ${chartDate.marker(x,candleDate(b))}><title>${esc(candleDescription(b))}</title><rect x="${left+i*slot}" y="${top}" width="${slot}" height="${bottom-top}" fill="transparent"/><line x1="${x}" x2="${x}" y1="${y(b.high)}" y2="${y(b.low)}" stroke="${color}"/><rect x="${x-w/2}" y="${Math.min(y(b.open),y(b.close))}" width="${w}" height="${Math.max(2,Math.abs(y(b.open)-y(b.close)))}" fill="${color}"/></g>`;
    }).join("");
    const first=bars[0].start,last=bars.at(-1).end,dates=first===last?`<text class="mh-date-tick" x="${(left+right)/2}" y="209" text-anchor="middle">${first}</text>`:`<text class="mh-date-tick" x="${left}" y="209">${first}</text><text class="mh-date-tick" x="${right}" y="209" text-anchor="end">${last}</text>`;
    box.innerHTML=chartDate.render("mm-candle-chart",`${$("mmFactorHistoryTitle").textContent}：${bars.length} 根`,{width,height:220,top,bottom},grid+marks+dates);
  }
  function filterFactorHistory(){
    const c=state.candle,rows=state.data.chart.factor_history;
    state.candles=c.start<=c.end?makeCandles(rows,c):[];
    const latest=state.candles.at(-1);
    $("mmCandleDetail").textContent=latest?candleDescription(latest):"";
    if(latest)renderCandlePlot();
    else $("mmCandlePlot").innerHTML=empty(c.start>c.end?"开始日期不能晚于结束日期。":rows.length?"所选区间没有实际观测。":"当前快照没有该指标的实际观测。");
  }
  function renderFactorHistory(d,factor,reset=false){
    const rows=d.chart.factor_history,first=rows[0]?.date,last=rows.at(-1)?.date,end=last||d.as_of;
    $("mmFactorHistory").hidden=false;
    $("mmFactorHistoryTitle").textContent=`${d.factor==="equity_bond_spread"?d.equity_name+" ":""}${d.factor_name} · 指标 K 线`;
    $("mmFactorCoverage").textContent=`${first?`历史观测 ${first} 至 ${last}`:"暂无历史观测"} · 单位：${factor.unit} · 开收取周期内首末观测，高低取极值`;
    const previous=state.candle;
    if(reset||previous?.factor!==d.factor||(!previous.custom&&previous.end!==end)){
      const start=new Date(Date.parse(end)-364*864e5).toISOString().slice(0,10);
      const bucket=previous?.factor===d.factor?previous.bucket:factor.frequency==="monthly"||["lpr1y","lpr5y"].includes(d.factor)?"month":"week";
      state.candle={factor:d.factor,start,end,bucket,custom:false};
      $("mmCandleStart").value=start;$("mmCandleEnd").value=end;$("mmCandleBucket").value=bucket;
    }
    filterFactorHistory();
  }
  function render(){const d=state.data,r=d.relationship;state.factors=d.factors;if($("mmFactor").options.length===1)factors();
    $("mmMeta").textContent=`研究快照 ${d.as_of} · 读取时间 ${d.updated_at||"尚未采集"} · 指标各自保留实际数据日`;
    $("mmMetrics").innerHTML=d.metrics.map(m=>`<button type="button" class="mm-metric" data-mm-factor="${m.key}" ${m.frequency==="quarterly"?'disabled title="季频环境指标，暂不参与日/月频相关研究"':""} aria-pressed="${m.key===d.factor}"><span>${esc(m.name)}</span><strong>${num(m.value,m.key==="usdcny"?4:2)} <span style="display:inline">${esc(m.unit)}</span></strong><small>观测 ${esc(m.period||m.date||"未取得")}</small><small>${m.published_on?`刊载 ${esc(m.published_on)}`:m.frequency==="monthly"||m.frequency==="quarterly"?"原始发布日期未提供":"观测日不等于精确发布时间"}${m.age_days>60?` · 距今${m.age_days}天`:""}</small></button>`).join("");
    $("mmInsights").innerHTML=head("当前研究线索","先看数据是否同频，再看政策是否传导到信用、需求和盈利。")+`<ul class="mm-brief">${d.insights.map(n=>`<li>${esc(n)}</li>`).join("")}</ul>`;
    const factor=d.metrics.find(m=>m.key===d.factor),asset=d.chart.asset,last=asset.at(-1)?.date,first=asset[0]?.date;
    $("mmTrend").innerHTML=head(`${d.equity_name} × ${d.factor_name}`,"双轴仅便于对照原始走势；统计使用变化量，不以曲线接近认定因果。")+lineChart([{name:d.equity_name,rows:asset},{name:`${d.factor_name} (${factor.unit})`,rows:d.chart.factor.filter(r=>(!first||r.date>=first)&&(!last||r.date<=last))}],{dual:true});
    $("mmScatter").innerHTML=head("变化量关系与稳定性",`${r.start||"—"} 至 ${r.end||"—"} · ${r.status}`)+`<div class="mm-stats">${stat("Pearson相关 r",num(r.rho),"范围 -1 至 +1")}${stat("Spearman秩相关",num(r.spearman),"观察单调关系")}${stat("有效样本",num(r.n,0),`${d.frequency==="monthly"?"月":"日"}频`)}${stat("样本 R²",r.r2==null?"—":`${num(r.r2*100,1)}%`,"单因子线性拟合解释度")}</div>`+scatter(r)+`<p class="mm-subtle">β ${num(r.beta,4)}（指数对数收益% / ${esc(r.x_unit)}）；窗口对照 ${r.stability.map(s=>`${s.window}${d.frequency==="monthly"?"月":"日"} r=${num(s.rho)} / n=${s.n}`).join("；")}。${esc(d.alignment_note)}</p>`;
    renderValuationComparison(d);renderFactorHistory(d,factor);
    const corrCell=row=>`<span class="mm-corr"><i style="width:${Math.abs(row.rho||0)*80}px;background:${(row.rho||0)<0?"#b88f5d":"#3d806c"}"></i>${num(row.rho)}</span>`;
    $("mmCorrelations").innerHTML=head("宏观因子联动矩阵","点击因子切换研究；点击任意表头升降序，缺失值始终排在最后。")+table("correlations",[["宏观因子","name",row=>`<button class="mc-row-btn" data-mm-factor="${row.key}" type="button">${esc(row.name)}</button>`],["相关 r","rho",corrCell],["有效样本","n",row=>num(row.n,0)],["β","beta",row=>num(row.beta,4)],["覆盖起点","start"],["覆盖终点","end"]],d.correlations);
    $("mmSectors").innerHTML=head("行业敏感度","申万一级行业历史变化关系，当前分类与成分会变化；相关程度不是板块涨跌预测。")+table("sectors",[["行业","name"],["相关 r","rho",corrCell],["秩相关","spearman",row=>num(row.spearman)],["β","beta",row=>num(row.beta,4)],["样本","n",row=>num(row.n,0)],["截至","end"]],d.sector_sensitivity);
    $("mmGold").innerHTML=head("黄金、实际利率与汇率：交叉验证","同一研究窗口内，上金所Au99.99人民币金价收益与海外因子变化的关系；黄金并非在所有阶段都与实际利率反向。")+table("gold",[["黄金对照因子","name"],["相关 r","rho",corrCell],["秩相关","spearman",r=>num(r.spearman)],["有效样本","n",r=>num(r.n,0)],["起点","start"],["终点","end"]],d.gold_links);
    $("mmTransmission").innerHTML=head("政策怎样影响市场","条件传导框架：同时观察受益路径、反向风险与可验证数据。")+`<div class="mm-transmissions">${d.transmissions.map(t=>`<article class="mm-transmission"><h4>${esc(t.name)}</h4><p class="mm-path">${esc(t.path)}</p><p>${esc(t.benefit)}</p><details><summary>反向风险与观察行业</summary><p><b>风险：</b>${esc(t.risk)}</p><p><b>验证：</b>${esc(t.watch)}</p><p><b>行业：</b>${esc(t.sectors)}</p></details></article>`).join("")}</div>`;
    $("mmEventSummary").innerHTML=d.events.summary.map(s=>stat(`${s.horizon}日历史中位收益`,pct(s.median),`均值 ${pct(s.mean)} · 完整样本 ${s.n} · 上涨频率 ${num(s.positive_share,1)}%`)).join("");
    $("mmEvents").innerHTML=table("events",[["事件日期","date"],["政策 / 利率事件","title",row=>`<div class="mm-wide">${link(row.url,row.title)}<small>${esc(row.summary)} · ${esc(row.source)}${row.overlap?" · 20日内有其他事件，窗口重叠":""}</small></div>`],["观察起点","anchor"],...[1,5,20,60].map(n=>[`${n}日收益`,row=>row.returns[String(n)],row=>`<span class="${tone(row.returns[String(n)])}">${pct(row.returns[String(n)])}</span>`]),["20日最低/基准", "drawdown20",row=>pct(row.drawdown20)]],d.events.rows);
    $("mmEventNote").textContent=`共 ${d.events.total} 条，展示 ${d.events.rows.length} 条；汇总覆盖全部筛选记录，CSV沿用表头排序。${d.events.note} 未完成窗口显示“—”。20日最低/基准为期内最低收盘相对事件前基准，并非峰谷最大回撤。`;
    $("mmEventsCsv").disabled=!d.events.rows.length;
    $("mmFiscal").innerHTML=head("财政收支与需求传导","原文亿元，均为年内累计；一般预算与政府性基金分列。")+lineChart([{name:"一般预算累计支出同比%",rows:d.fiscal.filter(r=>r.spending_yoy!=null).map(r=>({date:r.date,value:r.spending_yoy}))}])+table("fiscal",[["统计期","period"],["累计支出","spending",r=>num(r.spending,0)],["支出同比%","spending_yoy",r=>pct(r.spending_yoy)],["收入同比%","income_yoy",r=>pct(r.income_yoy)],["基金支出同比%","fund_spending_yoy",r=>pct(r.fund_spending_yoy)],["土地收入同比%","land_income_yoy",r=>pct(r.land_income_yoy)],["原文刊载","published_on",r=>link(r.url,r.published_on||"原文")]], [...d.fiscal].reverse());
    $("mmDebt").innerHTML=head("地方债发行：新增与再融资","新增可支持项目与需求；再融资用于偿还存量债务，不能加总视作新增刺激。单位：当月亿元。")+lineChart([{name:"新增发行亿元",rows:d.debt.map(r=>({date:r.date,value:r.new}))},{name:"再融资亿元",rows:d.debt.map(r=>({date:r.date,value:r.refinance}))}])+table("debt",[["统计期","period"],["新增债券","new",r=>num(r.new,0)],["再融资债券","refinance",r=>num(r.refinance,0)],["合计","total",r=>num(r.total,0)],["原文刊载","published_on",r=>link(r.url,r.published_on||"原文")]], [...d.debt].reverse());
    const v=d.valuation;$("mmValuation").innerHTML=head("盈利与估值：解释过去的涨跌",`${d.equity_name} · ${v.start||"—"} 至 ${v.as_of||"—"}`)+(v.available?`<div class="mm-stats">${stat("指数价格变化",pct(v.price_pct))}${stat("盈利代理贡献",num(v.earnings_log)+" log%")}${stat("估值贡献",num(v.valuation_log)+" log%")}${stat("同日 PE_TTM",num(v.pe),`历史分位 ${num(v.pe_percentile,1)}%`)}</div>`+lineChart([{"name":"指数价格",rows:v.rows.map(r=>({date:r.date,value:r.price}))},{name:"隐含盈利代理",rows:v.rows.map(r=>({date:r.date,value:r.earnings}))},{name:"PE估值",rows:v.rows.map(r=>({date:r.date,value:r.valuation}))}])+`<p class="mm-subtle">起点均为100。对数贡献可加总，普通收益不可直接相加。</p>`:empty(v.note))+`<p class="mm-subtle">${esc(v.note)}</p>`;
    renderScenario();
    const calendarRows=d.calendar.filter(r=>Number(r.date.slice(0,4))>=Number(d.today.slice(0,4))-1);$("mmCalendar").innerHTML=head("FOMC会议日历","官方日程，标记带经济预测的会议；计划日期可能调整。当前利率不代表期货隐含概率。")+table("calendar",[["会议结束日","date",r=>link(r.url,r.date)],["日程","days"],["经济预测",r=>r.sep?1:0,r=>r.sep?"含经济预测 SEP":"—"],["状态","status"]],calendarRows);
    $("mmMethod").innerHTML=`<p>${esc(d.alignment_note)}</p><ul>${d.limits.map(t=>`<li>${esc(t)}</li>`).join("")}</ul><p>日频利率使用基点变化、价格因子使用对数收益；月频同比因子使用百分点变化，PMI与金额使用原单位变化。月频按统计期配对，修订与公布滞后未还原。观察窗口按最近N个指数交易日或完整月份限定，缺失配对不向更早历史补满；日频最少60个、月频最少24个有效配对，无有效变化时不强行给数值。社融与债券金额有季节性，变化量相关未作季节调整。</p>${d.errors.length?`<p>来源读取情况：${d.errors.map(esc).join("；")}</p>`:""}`;
    $("mmSources").innerHTML=table("sources",[["通道","name"],["实际观测日","date"],["记录数","rows",r=>num(r.rows,0)],["状态","status"],["来源","source",r=>link(r.url,r.source)]],d.sources);
    $("mmExport").disabled=false;renderJob(d.job);
  }
  function renderScenario(){const form=$("mmScenario");if(!form.checkValidity()){$("mmScenarioResult").innerHTML=empty("请输入范围内的有效情景参数。");return;}const e=Number($("mmEarnings").value),p=Number($("mmPe").value),bp=Number($("mmDiscount").value),duration=Number($("mmDuration").value);const a=((1+e/100)*(1+p/100)-1)*100,b=((1+e/100)*Math.exp(-duration*bp/10000)-1)*100;
    $("mmScenarioResult").innerHTML=`<div class="mm-stats">${stat("盈利 × PE价格情景",pct(a),"(1+盈利变化)×(1+PE变化)−1")}${stat("盈利 × 折现近似情景",pct(b),"(1+盈利变化)×e^(−久期×折现变化)−1")}</div><p class="mm-subtle">${esc(state.data?.scenario.note||"")} 两项是独立假设结果，不相加、不作为目标价。</p>`;
    if(state.data)state.data.scenario={...state.data.scenario,earnings:e,pe:p,discount_bp:bp,duration,identity_pct:a,duration_pct:b};
  }
  function renderJob(job){clearTimeout(state.poll);const busy=job&&["queued","running"].includes(job.status);$("mmRefresh").disabled=!!busy;$("mmJob").hidden=!job;$("mmJob").textContent=job?busy?`宏观公开数据更新中 ${num(job.progress||0,0)}% · 已有研究保留原日期，完成后替换成功通道。`:job.status==="failed"?job.error||"更新失败，原研究保留":`更新完成 · ${job.finished_at||""}`:"";if(busy)state.poll=setTimeout(()=>load(true),3000);}
  async function load(poll=false){const id=++state.id;if(!poll){error();$("mmQueryButton").disabled=true;}root.setAttribute("aria-busy","true");try{const d=await api("/api/market-health/macro?"+new URLSearchParams(selected()));if(id!==state.id)return;state.data=d;render();if(!d.available)error("宏观研究尚未采集，请更新宏观研究；数据缺失不会显示为零。");}catch(e){if(id===state.id){error(`${e.message}；已显示的研究与原日期保留。`);if(poll)state.poll=setTimeout(()=>load(true),7000);}}finally{if(id===state.id){root.setAttribute("aria-busy","false");$("mmQueryButton").disabled=false;}}}
  $("mmQuery").addEventListener("submit",e=>{e.preventDefault();load();});$("mmEventQuery").addEventListener("submit",e=>{e.preventDefault();load();});
  $("mmCandleQuery").addEventListener("submit",e=>{e.preventDefault();if(!state.candle)return;
    Object.assign(state.candle,{start:$("mmCandleStart").value,end:$("mmCandleEnd").value,bucket:$("mmCandleBucket").value,custom:true});filterFactorHistory();});
  $("mmCandleReset").addEventListener("click",()=>{const d=state.data;if(d)renderFactorHistory(d,d.metrics.find(m=>m.key===d.factor),true);});
  for(const type of ["pointerover","click"])$("mmCandlePlot").addEventListener(type,e=>{const mark=e.target.closest("[data-mm-candle]");if(mark)$("mmCandleDetail").textContent=candleDescription(state.candles[Number(mark.dataset.mmCandle)]);});
  new ResizeObserver(()=>{if($("mmCandlePlot").clientWidth>0&&$("mmCandlePlot").clientWidth!==state.candleWidth)renderCandlePlot();}).observe($("mmCandlePlot"));
  $("mmFrequency").addEventListener("change",frequencyChanged);$("mmScenario").addEventListener("input",renderScenario);$("mmScenario").addEventListener("submit",e=>e.preventDefault());
  $("mmRefresh").addEventListener("click",async()=>{error();$("mmRefresh").disabled=true;try{const d=await api("/api/market-health/macro/refresh",{method:"POST",body:"{}"});renderJob(d.job);}catch(e){error(e.message);$("mmRefresh").disabled=false;}});
  document.addEventListener("market-health-full-refresh",()=>{if(state.started&&!$("mmRefresh").disabled)$("mmRefresh").click();});
  root.addEventListener("click",e=>{const sort=e.target.closest("[data-mm-sort]"),factor=e.target.closest("[data-mm-factor]");if(sort){const id=sort.dataset.mmSort,index=Number(sort.dataset.mmColumn),previous=state.sort.get(id),wrap=sort.closest(".mm-table"),left=wrap.scrollLeft,top=wrap.scrollTop;state.sort.set(id,{index,asc:previous?.index===index?!previous.asc:true});const m=state.tables.get(id);wrap.outerHTML=table(id,m.columns,m.rows);const next=root.querySelector(`[data-mm-sort="${id}"][data-mm-column="${index}"]`);next.closest(".mm-table").scrollLeft=left;next.closest(".mm-table").scrollTop=top;next.focus({preventScroll:true});}
    if(factor){const key=factor.dataset.mmFactor,spec=state.factors.find(r=>r.key===key);if(spec?.frequency==="quarterly")return;if(spec?.frequency==="monthly"&&$("mmFrequency").value!=="monthly"){$("mmFrequency").value="monthly";frequencyChanged();}$("mmFactor").value=key;load();}});
  $("mmExport").addEventListener("click",()=>{if(state.data)download(`宏观经济_${state.data.as_of}.json`,JSON.stringify(state.data,null,2));});
  $("mmEventsCsv").addEventListener("click",()=>{const model=state.tables.get("events"),rows=model?orderedRows("events",model.columns,model.rows):[],cell=v=>`"${(typeof v==="string"?v.replace(/^[=+@\-\t\r]/,"'$&"):String(v??"")).replaceAll('"','""')}"`;download(`宏观事件_${state.data.as_of}.csv`,"\ufeff"+[["事件日","事件","观察起点","1日%","5日%","20日%","60日%","窗口重叠","来源"],...rows.map(r=>[r.date,r.title,r.anchor,...[1,5,20,60].map(n=>r.returns[String(n)]),r.overlap,r.url])].map(r=>r.map(cell).join(",")).join("\r\n"),"text/csv;charset=utf-8");});
  function start(){if(!state.started&&!root.hidden){state.started=true;load();}}
  new MutationObserver(start).observe(root,{attributes:true,attributeFilter:["hidden"]});window.addEventListener("pagehide",()=>clearTimeout(state.poll));start();
})();
