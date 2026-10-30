/* 两融与资金各自保留真实披露日；查询不启动网络采集，刷新复用后台任务。 */
(() => {
  "use strict";
  if (!document.getElementById("mhView-financing")) return;
  const $=id=>document.getElementById(id);
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const num=(v,d=1)=>v==null||!Number.isFinite(Number(v))?"—":Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
  const money=v=>v==null?"—":`${v>0?"+":""}${num(v/1e8,2)}亿`;
  const pct=v=>v==null?"—":`${num(v)}%`;
  const tone=v=>v>0?"mh-positive":v<0?"mh-negative":"mh-neutral";
  const kpi=(label,value,note)=>`<div class="mh-kpi"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`;
  const head=(title,note="")=>`<div class="mh-box-head"><div><h3>${esc(title)}</h3><p>${esc(note)}</p></div></div>`;
  const empty=message=>`<p class="mh-empty">${esc(message)}</p>`;
  const stockButton=r=>`<button type="button" class="mc-row-btn" data-mc-stock="${esc(r.code)}" data-mc-name="${esc(r.name)}">查看K线</button>`;
  function table(labels,rows,cls="") {return `<div class="mh-table-wrap ${cls}"><table><thead><tr>${labels.map(v=>`<th scope="col">${esc(v)}</th>`).join("")}</tr></thead><tbody>${rows.join("")||`<tr><td colspan="${labels.length}">${empty("当前筛选无记录；可调整范围或更新公开数据。")}</td></tr>`}</tbody></table></div>`;}
  function sourceTable(rows) {const labels={margin:"交易所两融汇总",details:"个股两融明细",prices:"同日日线",factors:"复权因子",basics:"流通市值与换手",flows:"订单规模资金流",institutions:"公开机构席位",etf:"ETF份额与净值",national:"国家队持仓披露",quant:"量化雷达观测"};return table(["数据通道","实际数据日","记录数","读取状态"],(rows||[]).map(r=>`<tr><td>${esc(labels[r.key]||r.key)}</td><td>${esc(r.as_of||"未取得")}</td><td>${num(r.rows,0)}</td><td>${esc(r.status)}<small>${esc(r.note||"")}</small></td></tr>`),"mc-source");}
  async function api(url,opts={}) {const response=await fetch(url,{...opts,headers:{"Content-Type":"application/json"}});const value=await response.json();if(!response.ok)throw new Error(value.error||"公开数据读取失败");return value;}
  function error(prefix,message="") {$(prefix+"Error").hidden=!message;$(prefix+"Error").textContent=message;}
  function download(name,value,type="application/json;charset=utf-8") {const url=URL.createObjectURL(new Blob([typeof value==="string"?value:JSON.stringify(value,null,2)],{type}));const a=document.createElement("a");a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  function csv(name,rows) {if(!rows.length)return;const columns=[...new Set(rows.flatMap(r=>Object.keys(r).filter(k=>!Array.isArray(r[k])&&typeof r[k]!=="object")))];const cell=v=>`"${String(v??"").replace(/^[=+@\-\t\r]/,"'$&").replaceAll('"','""')}"`;download(name,"\ufeff"+[columns,...rows.map(r=>columns.map(k=>r[k]))].map(row=>row.map(cell).join(",")).join("\r\n"),"text/csv;charset=utf-8");}
  function chart(rows,series,label) {
    const valid=rows.filter(r=>series.some(s=>r[s.key]!=null));
    if(valid.length<2)return empty("至少需要两个有效交易日；缺失数据不画成零。");
    const values=valid.flatMap(r=>series.map(s=>r[s.key])).filter(v=>v!=null),lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1;
    const x=i=>48+i*602/(valid.length-1),y=v=>168-(v-lo)/span*136;
    return `<div class="mc-legend">${series.map(s=>`<span>${esc(s.label)}</span>`).join("")}</div><svg class="mh-chart" viewBox="0 0 690 210" role="img" aria-label="${esc(label)}">${[0,.5,1].map(f=>`<line class="mh-chart-grid" x1="48" x2="650" y1="${32+136*f}" y2="${32+136*f}"/><text x="0" y="${36+136*f}">${num((hi-f*span)/1e8,0)}亿</text>`).join("")}${series.map((s,j)=>{let path="",previous=false;valid.forEach((r,i)=>{if(r[s.key]==null){previous=false;return;}path+=`${previous?"L":"M"}${x(i)},${y(r[s.key])} `;previous=true;});return `<path d="${path}" fill="none" stroke="${["#14766d","#b88951","#8a9cab"][j]}" stroke-width="2"><title>${esc(s.label)}</title></path>`;}).join("")}<text x="48" y="197">${esc(valid[0].date)}</text><text x="650" y="197" text-anchor="end">${esc(valid.at(-1).date)}</text></svg>`;
  }
  function bars(rows,key,label,attribute="") {const max=Math.max(1,...rows.map(r=>Math.abs(r[key]||0)));return rows.map(r=>`<div class="mc-bar">${attribute?`<button type="button" ${attribute}="${esc(r.name)}">${esc(r.name)}</button>`:`<span>${esc(r.name||r.label)}</span>`}<div><i style="width:${Math.abs(r[key]||0)/max*100}%;background:${(r[key]||0)<0?"#4e9377":"#bc7464"}"></i></div><b class="${tone(r[key])}">${esc(label(r))}</b></div>`).join("")||empty("尚无有效记录");}
  const state={financing:{data:null,page:1,started:false,poll:null,id:0},participants:{data:null,page:1,started:false,poll:null,id:0}};
  let participant="main";
  const types=[
    ["institution","大机构","公开龙虎榜席位","仅上榜个股，已排除多日累计榜；未上榜不代表机构没有交易。"],
    ["main","大资金","大单 / 超大单代理","订单规模口径，不代表确认了机构账户；与其他维度可能重叠。"],
    ["etf","ETF","份额与申赎估算","相邻交易日份额变化×当日净值；分红、份额调整可能影响估算。"],
    ["quant","量化资金","疑似行为代理","复用量化雷达的公开行为观测；无法据此统计量化账户实际资金金额。"],
    ["national","国家队","已公告的持仓证据","公告日、报告期分别列示；持仓变化不是公告当日买卖，不推断当日净流入。"],
    ["sm","散户","小单资金代理","小单为订单规模代理，包含拆单等情况；不是已识别的散户账户净买入。"]
  ];
  function typeButtons() {$("mpTypes").innerHTML=types.map(([key,title,note])=>`<button type="button" class="mc-type" data-mp-type="${key}" aria-pressed="${["main","elg","lg","md"].includes(participant)?key==="main":key===participant}"><strong>${title}</strong><small>${note}</small></button>`).join("");}
  function pager(prefix,total,page) {const pages=Math.max(1,Math.ceil(total/50));$(prefix+"Page").innerHTML=`<span>共 ${total} 项 · ${page}/${pages} 页</span><button type="button" data-mc-page="${prefix}:${page-1}" ${page<=1?"disabled":""}>上一页</button><button type="button" data-mc-page="${prefix}:${page+1}" ${page>=pages?"disabled":""}>下一页</button>`;}
  function financeRows() {
    const data=state.financing.data;if(!data)return [];
    const search=$("mfSearch").value.trim().toLowerCase(),sector=$("mfSector").value,risk=$("mfRisk").value,key=$("mfSort").value;
    return data.rows.filter(r=>(!search||`${r.code} ${r.name} ${(r.flags||[]).join(" ")}`.toLowerCase().includes(search))&&(!sector||r.sector===sector)&&
      (risk==="all"||risk==="missing"&&r.model_ratio==null||risk==="warning"&&r.model_ratio!=null&&r.model_ratio<data.parameters.warning||risk==="closeout"&&r.model_ratio!=null&&r.model_ratio<data.parameters.closeout||risk==="crowded"&&r.float_ratio>=10||risk==="repay"&&r.net5!=null&&r.net5<0))
      .sort((a,b)=>a[key]==null?1:b[key]==null?-1:key==="model_ratio"?a[key]-b[key]:b[key]-a[key]);
  }
  function renderFinanceStocks() {
    const rows=financeRows(),s=state.financing;s.page=Math.min(s.page,Math.max(1,Math.ceil(rows.length/50)));
    $("mfCount").textContent=`筛选 ${rows.length} / ${s.data.rows.length}只 · 金额单位亿、价格元、融券数量股 · 每页50只，支持完整导出`;
    $("mfStocks").innerHTML=table(["股票 / 行业","融资余额","融资买入","融资偿还","当日净买入","5日 / 20日净额","融券余额 / 余量","融券卖出 / 偿还量","收盘 / 模型成本","模型担保比","距情景线缓冲","融资/流通","融资/成交","风险提示"],rows.slice((s.page-1)*50,s.page*50).map(r=>`<tr><td><button type="button" class="mc-row-btn" data-mf-code="${esc(r.code)}">${esc(r.name)}</button><small>${esc(r.code)} · ${esc(r.sector)}</small></td><td>${money(r.rzye)}</td><td>${money(r.rzmre)}</td><td>${money(r.rzche)}</td><td class="${tone(r.net)}">${money(r.net)}</td><td>${money(r.net5)}<small>20日 ${money(r.net20)}</small></td><td>${money(r.rqye)}<small>${num(r.rqyl,0)}股</small></td><td>${num(r.rqmcl,0)}<small>偿还 ${num(r.rqchl,0)}</small></td><td>${num(r.close,2)}<small>成本 ${num(r.cost,2)}</small></td><td class="${r.model_ratio!=null&&r.model_ratio<s.data.parameters.warning?"mc-alert":""}">${pct(r.model_ratio)}<small>${esc(r.risk)}</small></td><td>${pct(r.buffer)}</td><td>${pct(r.float_ratio)}</td><td>${pct(r.buy_ratio)}</td><td class="mc-stock-flags">${esc((r.flags||[]).join("；")||"未触发所列集中风险")}</td></tr>`));
    pager("mf",rows.length,s.page);$("mfCsv").disabled=!rows.length;
  }
  function renderFinance(data) {
    const s=data.summary||{},c=data.coverage;
    $("mfMeta").textContent=`两融披露 ${data.as_of||"未取得"} · 个股 ${c.stocks}只 · 模型有效 ${c.model}只 · 采集 ${data.updated_at||"尚未更新"}`;
    $("mfDisclosure").textContent=data.disclosure;$("mfModelNote").textContent=data.model_note;
    $("mfSummary").innerHTML=`<div class="mh-kpis">${kpi("沪深融资余额",money(s.rzye),"两所同日完整汇总 / 含非股票标的")}${kpi("融资净买入",money(s.net_buy),`买入 ${money(s.rzmre)} / 偿还 ${money(s.rzche)}`)}${kpi("融券余额",money(s.rqye),"金额口径，不累加不同证券的数量")}${kpi("模型有效覆盖",`${c.model} / ${c.stocks}`,`对应融资余额 ${money(c.model_balance)}`)}</div>`;
    $("mfTrend").innerHTML=head("融资余额与净买入趋势",`${data.history.length}个有效披露日 · 单位亿元`)+chart(data.history,[{key:"rzye",label:"融资余额"}],"两融余额趋势")+`<p class="mc-foot">最近五个有效披露日净额：${data.history.slice(-5).map(r=>`${esc(r.date)} ${money(r.net_buy)}`).join(" / ")||"未取得"}</p>`;
    $("mfDistribution").innerHTML=head("模型担保比例分布",`固定区间分布 · 中位数 ${pct(c.median_ratio)} · 以有效模型余额为分母`)+bars(data.distribution,"balance",r=>`${r.count}只 / ${pct(r.share)}`)+`<p class="mc-foot">个股样本融资余额 ${money(c.stock_balance)}；情景模型覆盖 ${money(c.model_balance)}。分布不是券商客户账户的真实担保率。</p>`;
    $("mfStress").innerHTML=table(["统一跌幅假设","低于平仓情景线","对应融资余额","模型余额占比","低于预警情景线","对应融资余额","模型余额占比"],data.stress.map(r=>`<tr class="${r.drop===0?"mc-scenario-current":""}"><td>${r.drop===0?"当前价格":`再跌 ${r.drop}%`}</td><td>${c.model?`${r.closeout_count}只`:"—"}</td><td>${c.model?money(r.closeout_balance):"—"}</td><td>${pct(r.closeout_share)}</td><td>${c.model?`${r.warning_count}只`:"—"}</td><td>${c.model?money(r.warning_balance):"—"}</td><td>${pct(r.warning_share)}</td></tr>`));
    $("mfSectors").innerHTML=table(["行业","样本股票","融资余额","融券余额","当日净额","5日净额","20日净额","模型预警 / 有效"],data.sectors.map(r=>`<tr><td><button type="button" class="mc-row-btn" data-mf-sector="${esc(r.name)}">${esc(r.name)}</button></td><td>${r.count}</td><td>${money(r.rzye)}</td><td>${money(r.rqye)}</td><td class="${tone(r.net)}">${money(r.net)}</td><td>${money(r.net5)}</td><td>${money(r.net20)}</td><td>${r.warning_count} / ${r.model_covered}</td></tr>`));
    const sector=$("mfSector").value;$("mfSector").innerHTML='<option value="">全部行业</option>'+data.sectors.map(r=>`<option value="${esc(r.name)}">${esc(r.name)}</option>`).join("");$("mfSector").value=sector;
    $("mfSources").innerHTML=sourceTable(data.sources);renderFinanceStocks();
  }
  function financeDetail(code) {
    const data=state.financing.data,r=data.rows.find(r=>r.code===code);if(!r)return;
    $("mfDetail").hidden=false;$("mfDetail").innerHTML=head(`${r.name} · 风险证据`,`${r.code} / ${r.sector} / ${r.as_of}`)+`<dl class="mc-detail-grid">${[["融资余额",money(r.rzye)],["同日收盘价",num(r.close,2)],["窗口代理成本",num(r.cost,2)],["模型担保比例",pct(r.model_ratio)],["距平仓情景线缓冲",pct(r.buffer)],["平仓情景价 / 非实际平仓价",num(r.model_price,2)],["余额变化",money(r.balance_change)],["变化与净买入差额",money(r.adjustment)],["融资余额/当日成交",`${num(r.cover_days)}倍`],["5 / 10 / 20日融资净额",`${money(r.net5)} / ${money(r.net10)} / ${money(r.net20)}`]].map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl><p class="mc-boundary">${esc(data.model_note)}</p><p class="mc-foot">${esc(r.flags.join("；")||"本轮未触发所列集中风险；这不表示不存在其他风险。")}</p>${stockButton(r)}`;$("mfDetail").scrollIntoView({behavior:"smooth",block:"nearest"});
  }
  function participantRows() {
    const rows=(state.participants.data?.rows||[]).filter(r=>$("mpDirection").value==="all"||$("mpDirection").value==="buy"&&r.net>0||$("mpDirection").value==="sell"&&r.net<0);
    return rows.sort((a,b)=>a.net==null?b.net==null?(b.activity||b.shares||0)-(a.activity||a.shares||0):1:b.net==null?-1:$("mpSort").value==="asc"?a.net-b.net:b.net-a.net);
  }
  function renderParticipantStocks() {
    const rows=participantRows(),s=state.participants,identity=["national","quant"].includes(participant);s.page=Math.min(s.page,Math.max(1,Math.ceil(rows.length/50)));
    $("mpDirection").disabled=identity;$("mpSort").disabled=identity;
    $("mpCount").textContent=`当前 ${rows.length} 项 · 每条保留来源日期；缺失窗口仅展示已取得的观测，完整性单独标记`;
    let labels=["股票 / 行业","所选净额","超大单","大单","中单","小单","正流入天数 / 观测","数据日期"];
    if(participant==="institution")labels=["股票 / 行业","机构买入","机构卖出","机构净额","单日上榜天数","数据日期"];
    if(participant==="etf")labels=["ETF / 跟踪指数","申赎估算","份额变化（份）","总份额（份）","总规模","净值","观测交易日","数据日期"];
    if(participant==="national")labels=["股票 / 行业","披露股东","持股数量（股）","持股占总股本","披露持股变化（股）","报告期","公告日期"];
    if(participant==="quant")labels=["股票 / 行业","疑似活跃刻度","方向代理","偏多行为数","偏空行为数","观测日期","资金金额"];
    $("mpStocks").innerHTML=table(labels,rows.slice((s.page-1)*50,s.page*50).map((r,i)=>{
      const first=`<td><button type="button" class="mc-row-btn" data-mp-row="${(s.page-1)*50+i}">${esc(r.name)}</button><small>${esc(r.code)} / ${esc(r.sector)}</small></td>`;
      let cells=`<td class="${tone(r.net)}">${money(r.net)}</td>${[r.elg,r.lg,r.md,r.sm].map(v=>`<td class="${tone(v)}">${money(v)}</td>`).join("")}<td>${r.positive_days} / ${r.days}<small>${r.complete?"窗口完整":`已取得${r.days}/${r.expected_days}日`}</small></td><td>${esc(r.as_of)}</td>`;
      if(participant==="institution")cells=`<td>${money(r.buy)}</td><td>${money(r.sell)}</td><td class="${tone(r.net)}">${money(r.net)}</td><td>${r.days}日</td><td>${esc(r.as_of)}</td>`;
      if(participant==="etf")cells=`<td class="${tone(r.net)}">${money(r.net)}</td><td>${num(r.share_change,0)}</td><td>${num(r.shares,0)}</td><td>${money(r.size)}</td><td>${num(r.nav,4)}</td><td>${r.days}/${r.expected_days}<small>${r.complete?"完整相邻日":"窗口不完整"}</small></td><td>${esc(r.as_of)}</td>`;
      if(participant==="national")cells=`<td>${esc(r.holder)}</td><td>${num(r.shares,0)}</td><td>${pct(r.hold_ratio)}</td><td>${num(r.change_shares,0)}<small>缺值表示新进 / 未披露，非0</small></td><td>${esc(r.period)}</td><td>${esc(r.as_of)}</td>`;
      if(participant==="quant")cells=`<td>${num(r.activity)}</td><td>${esc(r.direction||"未取得")}</td><td>${num(r.bull,0)}</td><td>${num(r.bear,0)}</td><td>${esc(r.as_of)}</td><td>不可识别</td>`;
      return `<tr>${first}${cells}</tr>`;
    }));pager("mp",rows.length,s.page);$("mpCsv").disabled=!rows.length;
  }
  function renderParticipants(data) {
    typeButtons();const kind=types.find(r=>r[0]===participant)||types[1],t=data.totals,identity=["national","quant"].includes(participant);
    $("mpMeta").textContent=`查询 ${data.start||"未取得"} 至 ${data.as_of||"未取得"} · ${kind[1]} · 采集 ${data.updated_at||"尚未采集"}`;
    $("mpBoundary").textContent=kind[3];$("mpDisclosure").textContent=data.disclosure;$("mpRankTitle").textContent=`${kind[1]} · ${participant==="national"?"持仓披露查询":participant==="quant"?"疑似行为观测":"买卖与资金去向"}`;
    $("mpSummary").innerHTML=`<div class="mh-kpis">${kpi(identity?"证据条目":"有效样本净额",identity?`${t.count}项`:money(t.observed_net??t.net),identity?"不推算每日净流入金额":`金额覆盖 ${t.amount_covered??t.count}/${t.count}项；非全市场增量资金`)}${kpi(identity?"覆盖股票":"净买入 / 申购",identity?`${new Set(data.rows.map(r=>r.code)).size}只`:`${t.buying}只`,identity?"以公开证据为边界":"按所选口径净额大于0")}${kpi(identity?"覆盖行业":"净卖出 / 赎回",identity?`${data.sectors.length}项`:`${t.selling}只`,identity?"股票当前分类":"按所选口径净额小于0")}${kpi("实际数据截止",data.as_of||"未取得",participant==="national"?"公告日期；报告期在明细列示":"每个来源独立保留真实数据日")}</div>`;
    const best=data.sectors.find(r=>r.net>0),worst=[...data.sectors].reverse().find(r=>r.net<0);
    $("mpInsights").textContent=identity?`${kind[1]}使用${kind[2]}。当前查询得到${t.count}条证据，行业覆盖${data.sectors.length}项；明细可追溯到观测日或公告日，不能据此判断当日买入卖出金额。`:`已取得观测中，${best?`${best.name}承接最强（${money(best.net)}）`:"没有正净额行业"}，${worst?`${worst.name}净卖出最强（${money(worst.net)}）`:"没有负净额行业"}。当前${t.complete}/${t.count}项具备所选完整交易日窗口；净额仅描述所列公开样本，不作为上涨概率。`;
    $("mpTrend").innerHTML=head("所选资金的逐日轨迹",identity?"身份类证据不伪造资金曲线":"按查询的股票与日期统计 / 金额元已换算亿元")+(data.history.length?chart(data.history,[{key:"net",label:"逐日净额"}],"资金净额逐日轨迹"):empty(identity?"该维度不提供可验证的每日净额；请查看下方日期与证据。":"当前维度未提供可比的日资金序列。"));
    $("mpSectors").innerHTML=head(participant==="etf"?"ETF流向的跟踪指数":"行业影响与覆盖",identity?"按证据条目数量展示":"点击行业名称，查询对应个股")+(identity?bars(data.sectors.slice(0,12),"count",r=>`${r.count}项`):bars([...data.sectors].sort((a,b)=>Math.abs(b.net||0)-Math.abs(a.net||0)).slice(0,12),"net",r=>money(r.net),"data-mp-sector"));
    if(["main","elg","lg","md"].includes(participant))$("mpBoundary").innerHTML+=` <label for="mpAmountKind">订单口径</label> <select id="mpAmountKind" class="mc-mini-select">${[["main","主力（大单+超大单）"],["elg","超大单"],["lg","大单"],["md","中单"]].map(([k,v])=>`<option value="${k}" ${participant===k?"selected":""}>${v}</option>`).join("")}</select>`;
    $("mpSources").innerHTML=sourceTable(data.sources);renderParticipantStocks();
    if(data.range_note)$("mpInsights").textContent+=` ${data.range_note}。`;
    $("mpMatrix").innerHTML=participant==="etf"?empty("ETF以跟踪指数单独统计；没有同日篮子与实际成分权重时，不将全部申赎估算额映射为某个行业的买卖。"):table(["行业","大单+超大单净额","超大单净额","机构公开席位净额","小单净额","订单分歧","量化代理股票","国家队持仓股票"],(data.matrix||[]).map(r=>`<tr><td><button type="button" class="mc-row-btn" data-mp-sector="${esc(r.name)}">${esc(r.name)}</button></td>${[r.main,r.elg,r.institution,r.sm].map(v=>`<td class="${tone(v)}">${money(v)}</td>`).join("")}<td>${esc(r.relation)}</td><td>${r.quant_count}</td><td>${r.national_count}</td></tr>`));
  }
  function participantDetail(index) {
    const r=participantRows()[index];if(!r)return;$("mpDetail").hidden=false;
    $("mpDetail").innerHTML=head(`${r.name} · 公开证据`,`${r.code} / ${r.sector} / ${r.as_of}`)+(r.evidence?table(["交易日","公开席位","买入","卖出","净额","上榜依据"],r.evidence.map(v=>`<tr><td>${esc(v.date)}</td><td>${esc(v.seat)}</td><td>${money(v.buy)}</td><td>${money(v.sell)}</td><td>${money(v.net)}</td><td>${esc(v.reason)}</td></tr>`)):`<dl class="mc-detail-grid">${Object.entries(r).filter(([k,v])=>!Array.isArray(v)&&v!=null).map(([k,v])=>`<div><dt>${esc(({net:"所选净额（元）",shares:"持有份额 / 股数",holder:"披露股东",as_of:"数据日期",period:"报告期",share_change:"份额变化",sector:"行业 / 跟踪指数",nav:"净值",activity:"疑似活跃刻度",direction:"行为方向",main:"主力净额（元）",elg:"超大单净额（元）",lg:"大单净额（元）",md:"中单净额（元）",sm:"小单净额（元）"})[k]||k)}</dt><dd>${esc(typeof v==="number"?num(v,2):String(v))}</dd></div>`).join("")}</dl>`)+`<p class="mc-boundary">${esc($("mpBoundary").textContent)}</p>${stockButton(r)}`;$("mpDetail").scrollIntoView({behavior:"smooth",block:"nearest"});
  }
  function query(kind) {
    if(kind==="financing")return new URLSearchParams({window:$("mfWindow").value,deposit:$("mfDeposit").value,warning:$("mfWarning").value,closeout:$("mfCloseout").value});
    return new URLSearchParams({participant,start:$("mpStart").value,end:$("mpEnd").value,keyword:$("mpSearch").value});
  }
  async function load(kind) {
    const s=state[kind],prefix=kind==="financing"?"mf":"mp",id=++s.id;clearTimeout(s.poll);error(prefix);
    try {
      const data=await api(`/api/market-health/${kind}?${query(kind)}`);if(id!==s.id)return;s.data=data;
      if(kind==="financing")renderFinance(data);else renderParticipants(data);
      const busy=data.job&&["queued","running"].includes(data.job.status);$(prefix+"Refresh").disabled=Boolean(busy);$(prefix+"Export").disabled=!data.as_of;
      $(prefix+"Status").textContent=busy?`更新中 ${data.job.progress||0}% · ${data.job.logs?.at(-1)||"准备读取公开数据"}`:data.job?.status==="failed"?data.job.error:(data.errors||[]).join("；");
      if(busy)s.poll=setTimeout(()=>load(kind),2500);
    }catch(e){if(id!==s.id)return;error(prefix,`${e.message}。已显示的研究保留原日期。`);$(prefix+"Refresh").disabled=false;}
  }
  async function refresh(kind) {const prefix=kind==="financing"?"mf":"mp";$(prefix+"Refresh").disabled=true;error(prefix);$(prefix+"Status").textContent="正在启动完整公开数据采集，已显示的研究保留…";try{await api(`/api/market-health/${kind}/refresh`,{method:"POST",body:"{}"});await load(kind);}catch(e){error(prefix,e.message);$(prefix+"Refresh").disabled=false;}}
  for(const kind of Object.keys(state)) {
    const prefix=kind==="financing"?"mf":"mp",root=$("mhView-"+kind);
    function activate(){if(!root.hidden&&!state[kind].started){state[kind].started=true;load(kind);}}
    new MutationObserver(activate).observe(root,{attributes:true,attributeFilter:["hidden"]});activate();
    $(prefix+"Refresh").addEventListener("click",()=>refresh(kind));$(prefix+"Export").addEventListener("click",()=>state[kind].data&&download(`${kind}_${state[kind].data.as_of}.json`,state[kind].data));
    $(prefix+"Csv").addEventListener("click",()=>csv(`${kind}_${state[kind].data?.as_of}.csv`,kind==="financing"?financeRows():participantRows()));
    root.addEventListener("click",e=>{const page=e.target.closest("[data-mc-page]");if(page){const[p,n]=page.dataset.mcPage.split(":");state[kind].page=Number(n);p==="mf"?renderFinanceStocks():renderParticipantStocks();}const stock=e.target.closest("[data-mc-stock]");if(stock&&typeof window.openStockKlineModal==="function")window.openStockKlineModal(stock.dataset.mcStock,240,{stockName:stock.dataset.mcName}).catch(e=>error(prefix,e.message));});
  }
  $("mfModel").addEventListener("submit",e=>{e.preventDefault();state.financing.page=1;$("mfDetail").hidden=true;load("financing");});
  for(const id of ["mfSearch","mfSector","mfRisk","mfSort"])$(id).addEventListener(id==="mfSearch"?"input":"change",()=>{state.financing.page=1;if(state.financing.data)renderFinanceStocks();});
  $("mhView-financing").addEventListener("click",e=>{const stock=e.target.closest("[data-mf-code]"),sector=e.target.closest("[data-mf-sector]");if(stock)financeDetail(stock.dataset.mfCode);if(sector){$("mfSector").value=sector.dataset.mfSector;state.financing.page=1;renderFinanceStocks();$("mfStocks").scrollIntoView({behavior:"smooth",block:"nearest"});}});
  $("mpTypes").addEventListener("click",e=>{const b=e.target.closest("[data-mp-type]");if(!b)return;participant=b.dataset.mpType;$("mpDirection").value="all";$("mpStart").value="";$("mpEnd").value="";$("mpDetail").hidden=true;state.participants.page=1;typeButtons();load("participants");});
  $("mpQuery").addEventListener("submit",e=>{e.preventDefault();state.participants.page=1;$("mpDetail").hidden=true;load("participants");});
  $("mpLatest").addEventListener("click",()=>{$("mpStart").value="";$("mpEnd").value="";state.participants.page=1;load("participants");});
  for(const id of ["mpDirection","mpSort"])$(id).addEventListener("change",()=>{state.participants.page=1;renderParticipantStocks();});
  $("mhView-participants").addEventListener("change",e=>{if(e.target.id==="mpAmountKind"){participant=e.target.value;load("participants");}});
  $("mhView-participants").addEventListener("click",e=>{const row=e.target.closest("[data-mp-row]"),sector=e.target.closest("[data-mp-sector]"),days=e.target.closest("[data-mp-days]");if(row)participantDetail(Number(row.dataset.mpRow));if(sector){$("mpSearch").value=sector.dataset.mpSector;state.participants.page=1;load("participants");}if(days){const dates=(state.participants.data?.calendar||[]).filter(d=>d<=(state.participants.data.as_of||""));if(dates.length){$("mpEnd").value=dates.at(-1);$("mpStart").value=dates[Math.max(0,dates.length-Number(days.dataset.mpDays))];state.participants.page=1;load("participants");}}});
  document.addEventListener("market-health-full-refresh",()=>{for(const kind of Object.keys(state))if(state[kind].started&&!$(kind==="financing"?"mfRefresh":"mpRefresh").disabled)refresh(kind);});
  window.addEventListener("pagehide",()=>{for(const s of Object.values(state))clearTimeout(s.poll);});typeButtons();
})();
