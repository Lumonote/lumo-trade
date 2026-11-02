/* 无外部依赖的透视网格与实体节点；连线只取后端给出的成分/行业关系。 */
(() => {
  "use strict";
  const palette = ["#91b8b2","#a8a4cb","#c0ae78","#b58d93","#85afb9","#969fc0","#b8bd8e","#ce9c92"];
  class ShortTermScene {
    constructor(canvas, overlay, select) {
      this.canvas=canvas;this.ctx=canvas.getContext("2d");this.overlay=overlay;this.select=select;
      this.width=0;this.height=0;this.items=[];this.stocks=[];this.edges=[];this.labels=[];this.selected="";this.pulseLinks=new Map();
      this.motion=!matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.resizeObserver=new ResizeObserver(()=>this.resize());this.resizeObserver.observe(canvas);
      this.resize();this.last=0;this.frame=0;this.run=this.run.bind(this);this.frame=requestAnimationFrame(this.run);
    }
    resize() {
      const rect=this.canvas.getBoundingClientRect();this.width=rect.width;this.height=rect.height;
      const ratio=Math.min(devicePixelRatio||1,2);this.canvas.width=Math.round(rect.width*ratio);this.canvas.height=Math.round(rect.height*ratio);
      this.ctx.setTransform(ratio,0,0,ratio,0,0);this.draw(performance.now()/1000);
    }
    setData(data) {
      this.data=data;this.pulseLinks.clear();
      const map=new Map(data.sectors.map(r=>[r.id,r])), stocks=new Map(data.stocks.map(r=>[r.code,r]));
      this.items=(data.graph?.sector_ids||[]).map(id=>map.get(id)).filter(Boolean).map((r,i,all)=>({
        ...r,color:palette[i%palette.length],position:[all.length===1?0:-6.2+i*12.4/(all.length-1),1.45+(i%3)*.35,-.85-(i%2)*.6],index:i}));
      this.stocks=(data.graph?.stock_codes||[]).map(code=>stocks.get(code)).filter(Boolean).map((r,i)=>({
        ...r,position:[-5.9+(i%8)*11.8/7,-.8,1+Math.floor(i/8)*1.1],index:i}));
      this.edges=data.graph?.edges||[];
      this.overlay.replaceChildren();this.labels=[];
      const add=(row,kind)=>{
        const button=document.createElement("button");button.type="button";button.className="st-node st-node-"+kind;
        button.dataset.stNode=kind==="sector"?row.id:row.code;
        const strong=document.createElement("strong");strong.textContent=row.name;
        const small=document.createElement("small");
        small.textContent=kind==="sector"?`${row.stock_codes.length}股 · ${row.news_ids.length}消息`:`${row.code} · ${row.change_pct==null?"待报价":Number(row.change_pct).toFixed(2)+"%"}`;
        button.append(strong,small);button.addEventListener("click",()=>this.select(kind,kind==="sector"?row.id:row.code));
        this.overlay.append(button);this.labels.push({button,row,kind});
      };
      this.items.forEach(row=>add(row,"sector"));this.stocks.forEach(row=>add(row,"stock"));this.draw(performance.now()/1000);
    }
    project(p) {
      const x=p[0],y=p[1]-5,z=p[2]-12,s=.31,c=.9507;
      const depth=-y*s-z*c,vertical=y*c-z*s;
      const f=Math.min(this.width*.87,this.height*1.27);
      return [this.width*.5+x*f/depth,this.height*.43-vertical*f/depth,depth,f/depth];
    }
    path(points,stroke,width=1,fill="") {
      const ctx=this.ctx;ctx.beginPath();points.forEach((p,i)=>{const q=this.project(p);i?ctx.lineTo(q[0],q[1]):ctx.moveTo(q[0],q[1]);});
      if(fill){ctx.closePath();ctx.fillStyle=fill;ctx.fill();}if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=width;ctx.stroke();}
    }
    box(position,size,color="#a5b2c2") {
      const [x,y,z]=position,[a,b,c]=size;
      const p=[[-a,-b,-c],[a,-b,-c],[a,b,-c],[-a,b,-c],[-a,-b,c],[a,-b,c],[a,b,c],[-a,b,c]].map(v=>[v[0]+x,v[1]+y,v[2]+z]);
      const faces=[{p:[0,1,2,3],color:"#2e3136"},{p:[1,5,6,2],color:"#4c525a"},{p:[4,0,3,7],color:"#7a838f"},{p:[3,2,6,7],color}, {p:[5,4,7,6],color:"#a8b5c5"}];
      faces.sort((a,b)=>b.p.reduce((s,i)=>s+this.project(p[i])[2],0)-a.p.reduce((s,i)=>s+this.project(p[i])[2],0));
      faces.forEach(f=>this.path(f.p.map(i=>p[i]),"#b7c5d770",.65,f.color));
    }
    wing(position,width=.85) {
      const [x,y,z]=position;
      this.path([[x-width,y,z-.26],[x+width,y,z-.26],[x+width,y,z+.26],[x-width,y,z+.26]],"#a3afbf94",.8,"#24272b");
      for(let i=1;i<8;i++){const dx=-width+i*width/4;this.path([[x+dx,y,z-.26],[x+dx,y,z+.26]],"#8e99a76b",.5);}
      this.path([[x-width,y,z],[x+width,y,z]],"#8e99a76b",.5);
    }
    instrument(row,time,master=false) {
      const base=row.position;
      const p=[base[0],base[1]+(this.motion?Math.sin(time*.35+(row.index||0))*.035:0),base[2]];
      const s=master?1.35:.65;
      if(master||row.index%3===0){
        this.wing([p[0]-.97*s,p[1],p[2]],.7*s);this.wing([p[0]+.97*s,p[1],p[2]],.7*s);
        this.box(p,[.26*s,.37*s,.22*s],master?"#b4c1d2":"#b8c6d8");
        this.box([p[0],p[1],p[2]+.23*s],[.21*s,.26*s,.025*s],"#bdcbdd");
      } else if(row.index%3===1){
        const sides=10,ring=[];for(let i=0;i<sides;i++)ring.push([p[0]+Math.cos(i/sides*Math.PI*2)*.27,p[1]+.24,p[2]+Math.sin(i/sides*Math.PI*2)*.27]);
        for(let i=0;i<sides;i++){const a=ring[i],b=ring[(i+1)%sides];this.path([a,b,[b[0],b[1]-.5,b[2]],[a[0],a[1]-.5,a[2]]],"#bbc9db66",.5,i%2?"#5c636c":"#8d98a5");}
        this.path(ring,"#cfdef2",.5,"#b2c0d1");
      } else {
        this.box(p,[.18,.36,.2],"#9ca7b6");this.wing([p[0]-.48,p[1]-.03,p[2]],.29);
        this.path([[p[0]-.4,p[1]+.44,p[2]],[p[0]+.4,p[1]+.44,p[2]]],"#bac8daa0",1);
      }
      this.path([[p[0],p[1]+.35*s,p[2]],[p[0],p[1]+.7*s,p[2]]],"#b7c4d6",.7);
      const light=this.project([p[0],p[1]-.39*s,p[2]]),ctx=this.ctx;
      ctx.save();ctx.fillStyle=row.color||"#c9d9ec";ctx.shadowColor=ctx.fillStyle;ctx.shadowBlur=10;ctx.beginPath();ctx.arc(light[0],light[1],master?2:1.4,0,Math.PI*2);ctx.fill();ctx.restore();
    }
    meteor(a,b,index,time,start=-10000,delay=0,duration=1800,straight=false){
      const ctx=this.ctx,p=this.project(a),q=this.project(b),age=(time*1000-start-delay)/duration,active=age>=0&&age<1.2,head=active?Math.pow(Math.min(1,age),1.2):Math.pow((time*.145+index*.137)%1,1.22),strength=active?.4+.55*Math.sin(Math.PI*age/1.2):.4;
      const points=[];for(let j=20;j>=0;j--){const t=head-j*.0075;if(t<0)continue;const u=1-t;points.push(straight?[p[0]+(q[0]-p[0])*t,p[1]+(q[1]-p[1])*t]:[u*u*u*p[0]+3*u*u*t*p[0]+3*u*t*t*q[0]+t*t*t*q[0],u*u*u*p[1]+3*u*u*t*(p[1]+70)+3*u*t*t*(q[1]-24)+t*t*t*q[1]]);}
      if(points.length<2)return;const first=points[0],last=points.at(-1),gradient=ctx.createLinearGradient(first[0],first[1],last[0],last[1]);gradient.addColorStop(0,"#a2a0d500");gradient.addColorStop(.65,"#9ab9e5a0");gradient.addColorStop(1,"#f2faff");ctx.save();ctx.globalCompositeOperation="lighter";ctx.strokeStyle=gradient;ctx.lineCap="round";
      for(const [width,alpha] of [[6,.08],[2.8,.24],[.85,.95]]){ctx.lineWidth=width;ctx.globalAlpha=strength*alpha;ctx.beginPath();points.forEach((point,i)=>i?ctx.lineTo(...point):ctx.moveTo(...point));ctx.stroke();}ctx.restore();
    }
    curve(a,b,color,index,time,selected,start=-10000) {
      const ctx=this.ctx,p=this.project(a),q=this.project(b);
      for(let n=0;n<(selected?7:3);n++){
        const offset=(n-1)*1.9,bend=Math.sin(index*2.7+n)*18;
        ctx.beginPath();ctx.moveTo(p[0]+offset,p[1]);ctx.bezierCurveTo(p[0]+offset+bend,p[1]+70,q[0]-bend,q[1]-24,q[0]+offset*.3,q[1]);
        ctx.strokeStyle=color+(selected?"a8":"4d");ctx.lineWidth=selected?1.1:.6;ctx.stroke();
      }
      if(this.motion)this.meteor(a,b,index,time,start,850,1800);
    }
    draw(time=0) {
      if(!this.ctx||!this.width||!this.height)return;
      const ctx=this.ctx,w=this.width,h=this.height;ctx.clearRect(0,0,w,h);
      const glow=ctx.createRadialGradient(w*.5,h*.67,0,w*.5,h*.67,w*.5);glow.addColorStop(0,"#717a8437");glow.addColorStop(1,"#06060700");ctx.fillStyle=glow;ctx.fillRect(0,0,w,h);
      // 透视冷灰承台；关系线沿真实图谱承接面收束。
      this.path([[-8,-1.18,-1.5],[8,-1.18,-1.5],[8,-1.18,4.2],[-8,-1.18,4.2]],"#78818d45",.8,"#0d0e0f70");
      this.path([[-8,-1.18,4.2],[8,-1.18,4.2],[8,-1.38,4.2],[-8,-1.38,4.2]],"#949fae40",.6,"#161719a0");
      for(let i=-10;i<=10;i++)this.path([[i,-1.1,-4],[i,-1.1,5]],"#757e8a2e",.6);
      for(let z=-4;z<=5;z+=.65)this.path([[-10,-1.1,z],[10,-1.1,z]],"#757e8a26",.6);
      for(let i=-8;i<=8;i+=2)this.path([[i,-1,-5],[i,4,-5]],"#646c751a",.5);
      const sectorMap=new Map(this.items.map(r=>[r.id,r])),stockMap=new Map(this.stocks.map(r=>[r.code,r]));
      this.edges.forEach((edge,index)=>{
        const a=sectorMap.get(edge.source),b=stockMap.get(edge.target.replace("stock:",""));if(!a||!b)return;
        const selected=this.selected===a.id||this.selected===b.code;
        this.curve([a.position[0],a.position[1]-.3,a.position[2]],b.position,a.color,index,time,selected,this.pulseLinks.get("stock:"+b.code)??-10000);
      });
      const master={position:[0,3.8,-3.7],index:0,color:"#c6d5e8"};
      this.items.forEach(r=>{this.path([master.position,r.position],"#a3afbf50",.6);if(this.motion)this.meteor(master.position,r.position,r.index,time,this.pulseLinks.get("sector:"+r.id)??-10000,0,850,true);});
      [...this.items].sort((a,b)=>a.position[2]-b.position[2]).forEach(r=>this.instrument(r,time));
      this.instrument(master,time,true);
      const point=this.project([0,3,-3.7]);ctx.textAlign="center";ctx.fillStyle="#99a4b3";ctx.font="9px monospace";ctx.fillText("MARKET OBSERVATION",point[0],point[1]);
      this.stocks.forEach(r=>{
        const p=this.project(r.position);ctx.fillStyle="#aebbcc";ctx.fillRect(p[0]-1,p[1]-1,2,2);
        this.path([[r.position[0]-.18,r.position[1]-.025,r.position[2]-.1],[r.position[0]+.18,r.position[1]-.025,r.position[2]-.1],[r.position[0]+.18,r.position[1]-.025,r.position[2]+.1],[r.position[0]-.18,r.position[1]-.025,r.position[2]+.1]],"#9da9b88c",.6,"#2f323770");
      });
      // 微粒固定在承台坐标上，避免用随机数伪造任何行情。
      for(let i=0;i<150;i++){
        const x=Math.sin(i*13.17)*7.4,z=(Math.sin(i*7.31)+1)*2.4-.8,p=this.project([x,-1.08,z]);
        ctx.fillStyle=i%9===0?"#b0bdce75":"#848e9b25";ctx.fillRect(p[0],p[1],i%9===0?1.4:.65,.7);
      }
      this.labels.forEach(({button,row,kind})=>{
        const p=this.project([row.position[0],row.position[1]-(kind==="sector"?.62:.25),row.position[2]]);
        button.style.left=p[0]+"px";button.style.top=p[1]+"px";
        button.style.opacity=this.selected&&this.selected!==row.id&&this.selected!==row.code?".72":"1";
      });
      if(!this.items.length){ctx.font='12px "PingFang SC",sans-serif';ctx.fillStyle="#9ca8b7";ctx.fillText("正在等待可关联的板块与个股数据",w*.5,h*.55);}
    }
    setSelected(id){this.selected=id;this.draw(performance.now()/1000);}
    pulseUpdates({stockCodes=[],sectorIds=[]}={}){
      if(!this.motion||document.hidden)return [];const codes=new Set(stockCodes),sectors=new Set(sectorIds),now=performance.now(),related=this.items.filter(item=>sectors.has(item.id)||item.stock_codes.some(code=>codes.has(code))).slice(0,3),selected=new Set(related.map(item=>item.id));
      const targets=new Set(this.edges.filter(edge=>codes.has(edge.target.replace("stock:",""))||(sectors.has(edge.source)&&selected.has(edge.source))).slice(0,4).map(edge=>edge.target.replace("stock:","")));
      for(const id of selected)if(now-(this.pulseLinks.get("sector:"+id)??-10000)>4000)this.pulseLinks.set("sector:"+id,now);for(const code of targets)if(now-(this.pulseLinks.get("stock:"+code)??-10000)>4000)this.pulseLinks.set("stock:"+code,now);
      this.canvas.__stLastPulse={sectors:[...selected],stocks:[...targets],at:now};return [...[...selected].map(id=>({id,delay:850})),...[...targets].map(id=>({id,delay:2650}))];
    }
    setMotion(enabled){this.motion=enabled;this.draw(performance.now()/1000);}
    run(stamp){if(stamp-this.last>40&&!document.hidden&&this.motion){this.draw(stamp/1000);this.last=stamp;}this.frame=requestAnimationFrame(this.run);}
    destroy(){cancelAnimationFrame(this.frame);this.resizeObserver.disconnect();this.overlay.replaceChildren();}
  }
  window.LumoShortTermSceneFallback=ShortTermScene;
})();
