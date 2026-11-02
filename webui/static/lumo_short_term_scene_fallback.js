/* 无外部依赖的透视网格与实体节点；连线只取后端给出的成分/行业关系。 */
(() => {
  "use strict";
  const palette = ["#91b8b2","#a8a4cb","#c0ae78","#b58d93","#85afb9","#969fc0","#b8bd8e","#ce9c92"];
  class ShortTermScene {
    constructor(canvas, overlay, select) {
      this.canvas=canvas;this.ctx=canvas.getContext("2d");this.overlay=overlay;this.select=select;
      this.width=0;this.height=0;this.items=[];this.stocks=[];this.edges=[];this.labels=[];this.selected="";this.pulseLinks=new Map();this.meteorCache=new Map();this.visible=true;this.backdropDirty=true;this.frameMark=0;this.frameCount=0;this.canvas.dataset.flowEngine="canvas-timeline";this.canvas.dataset.flowBuild="20261102-smooth";
      this.motion=!matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.resizeObserver=new ResizeObserver(()=>this.resize());this.resizeObserver.observe(canvas);
      this.resize();this.last=0;this.frame=0;this.run=this.run.bind(this);this.frame=requestAnimationFrame(this.run);
      if(window.IntersectionObserver){this.visibilityObserver=new IntersectionObserver(entries=>{this.visible=entries[0].isIntersecting;this.flow?.setRunning(this.motion&&this.visible);});this.visibilityObserver.observe(canvas);}
    }
    resize() {
      const rect=this.canvas.getBoundingClientRect();this.width=rect.width;this.height=rect.height;
      const ratio=Math.min(devicePixelRatio||1,2);this.canvas.width=Math.round(rect.width*ratio);this.canvas.height=Math.round(rect.height*ratio);
      this.ctx.setTransform(ratio,0,0,ratio,0,0);this.backdropDirty=true;this.arrange();this.layoutLabels();this.layoutFlows();this.draw(performance.now()/1000);
    }
    setData(data) {
      this.flow?.destroy();this.flow=null;
      this.data=data;this.pulseLinks.clear();this.backdropDirty=true;
      const map=new Map(data.sectors.map(r=>[r.id,r])), stocks=new Map(data.stocks.map(r=>[r.code,r]));
      this.items=(data.graph?.sector_ids||[]).map(id=>map.get(id)).filter(Boolean).map((r,i)=>({...r,color:palette[i%palette.length],position:[0,0,0],index:i}));
      this.stocks=(data.graph?.stock_codes||[]).map(code=>stocks.get(code)).filter(Boolean).map((r,i)=>({...r,position:[0,0,0],index:i}));
      this.stockColumns=Math.max(1,Math.min(data.graph?.columns||8,this.stocks.length));this.arrange();
      this.edges=data.graph?.edges||[];
      this.overlay.replaceChildren();this.labels=[];
      const add=(row,kind)=>{
        const button=document.createElement("button");button.type="button";button.className="st-node st-node-"+kind;
        button.dataset.stNode=kind==="sector"?row.id:row.code;
        const strong=document.createElement("strong");strong.textContent=row.name;
        const small=document.createElement("small");
        small.textContent=kind==="sector"?`${row.stock_codes.length}股 · ${row.news_ids.length}消息`:`${row.code} · ${row.change_pct==null?"待报价":Number(row.change_pct).toFixed(2)+"%"}`;
        button.title=row.name+(kind==="stock"?" "+row.code:"");button.append(strong,small);button.addEventListener("click",()=>this.select(kind,kind==="sector"?row.id:row.code));
        this.overlay.append(button);this.labels.push({button,row,kind});
      };
      this.items.forEach(row=>add(row,"sector"));this.stocks.forEach(row=>add(row,"stock"));this.layoutLabels();
      if(window.LumoShortTermFlow&&Element.prototype.animate){this.flow=new window.LumoShortTermFlow(this.overlay);this.layoutFlows();this.canvas.dataset.flowEngine="compositor-timeline";this.canvas.dataset.flowBuild="20261102-compositor";}
      this.draw(performance.now()/1000);
    }
    layoutFlows(){
      if(!this.flow)return;const sectors=new Map(this.items.map(row=>[row.id,row])),stocks=new Map(this.stocks.map(row=>[row.code,row])),paths=[];
      this.items.forEach(row=>{const a=this.project([0,3.8,-3.7]),b=this.project(row.position);paths.push({kind:"uplink",sectorId:row.id,points:[a,b]});});
      this.edges.forEach(edge=>{const sector=sectors.get(edge.source),stock=stocks.get(edge.target.replace("stock:",""));if(!sector||!stock)return;const a=this.project([sector.position[0],sector.position[1]-.3,sector.position[2]]),b=this.project(stock.position),points=Array.from({length:128},(_,i)=>{const t=i/127,u=1-t;return [u*u*u*a[0]+3*u*u*t*a[0]+3*u*t*t*b[0]+t*t*t*b[0],u*u*u*a[1]+3*u*u*t*(a[1]+70)+3*u*t*t*(b[1]-24)+t*t*t*b[1]];});paths.push({kind:"membership",sectorId:sector.id,stockCode:stock.code,points});});
      this.flow.setPaths(paths);this.flow.setRunning(this.motion&&this.visible);
    }
    unproject(x,y,z){const s=.31,c=.9507,f=Math.min(this.width*.87,this.height*1.27)||1,q=(this.height*.43-y)/f,pz=z-12,py=(s-q*c)*pz/(q*s+c),depth=-py*s-pz*c;return [(x-this.width*.5)*depth/f,py+5,z];}
    arrange(){
      this.meteorCache.clear();
      const sectorMargin=Math.max(this.width*.07,Math.min(100,this.width/(this.items.length||1)-12)/2+8),stockMargin=Math.max(this.width*.08,58);
      this.items.forEach((row,i,all)=>{row.position=this.unproject(all.length===1?this.width*.5:sectorMargin+i*(this.width-2*sectorMargin)/(all.length-1),this.height*(.32+[0,-.02,.01][i%3]),-1.2);});
      const columns=this.stockColumns||8;this.stocks.forEach((row,i)=>{const line=Math.floor(i/columns),col=i%columns,count=Math.min(columns,this.stocks.length-line*columns);row.position=this.unproject(count===1?this.width*.5:stockMargin+col*(this.width-2*stockMargin)/(count-1),this.height*(.59+line*.145),1);});
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
      const ctx=this.ctx,key=(straight?"u":"s")+index;let samples=this.meteorCache.get(key);
      if(!samples){const p=this.project(a),q=this.project(b);samples=new Float32Array(256);for(let i=0;i<128;i++){const t=i/127,u=1-t;samples[i*2]=straight?p[0]+(q[0]-p[0])*t:u*u*u*p[0]+3*u*u*t*p[0]+3*u*t*t*q[0]+t*t*t*q[0];samples[i*2+1]=straight?p[1]+(q[1]-p[1])*t:u*u*u*p[1]+3*u*u*t*(p[1]+70)+3*u*t*t*(q[1]-24)+t*t*t*q[1];}this.meteorCache.set(key,samples);}
      const age=(time*1000-start-delay)/duration,heads=[[(time*.145+index*.137)%1*1.18,.4]];if(age>=0&&age<1.18)heads.push([age,.4+.55*Math.sin(Math.PI*age/1.18)]);
      for(const [head,intensity] of heads){const from=Math.max(0,head-.15),to=Math.min(1,head);if(to<=from)continue;const fade=Math.min(1,head/.035)*Math.min(1,(1.18-head)/.18),lo=Math.floor(from*127),hi=Math.min(127,Math.ceil(to*127)),gradient=ctx.createLinearGradient(samples[lo*2],samples[lo*2+1],samples[hi*2],samples[hi*2+1]);gradient.addColorStop(0,"#a2a0d500");gradient.addColorStop(.65,"#9ab9e5a0");gradient.addColorStop(1,"#f2faff");ctx.save();ctx.globalCompositeOperation="lighter";ctx.strokeStyle=gradient;ctx.lineCap="round";ctx.beginPath();
        for(let j=0;j<=20;j++){const at=(from+(to-from)*j/20)*127,index=Math.min(126,Math.floor(at)),mix=at-index,x=samples[index*2]+(samples[index*2+2]-samples[index*2])*mix,y=samples[index*2+1]+(samples[index*2+3]-samples[index*2+1])*mix;j?ctx.lineTo(x,y):ctx.moveTo(x,y);}
        for(const [width,alpha] of [[6,.08],[2.8,.24],[.85,.95]]){ctx.lineWidth=width;ctx.globalAlpha=intensity*fade*alpha;ctx.stroke();}ctx.restore();
      }
    }
    curve(a,b,color,index,time,selected,start=-10000,meteors=true) {
      const ctx=this.ctx,p=this.project(a),q=this.project(b);
      for(let n=0;n<(selected?7:3);n++){
        const offset=(n-1)*1.9,bend=Math.sin(index*2.7+n)*18;
        ctx.beginPath();ctx.moveTo(p[0]+offset,p[1]);ctx.bezierCurveTo(p[0]+offset+bend,p[1]+70,q[0]-bend,q[1]-24,q[0]+offset*.3,q[1]);
        ctx.strokeStyle=color+(selected?"a8":"4d");ctx.lineWidth=selected?1.1:.6;ctx.stroke();
      }
      if(this.motion&&meteors)this.meteor(a,b,index,time,start,850,1800);
    }
    draw(time=0,baseOnly=false) {
      if(!this.ctx||!this.width||!this.height)return;
      const ctx=this.ctx,w=this.width,h=this.height;ctx.clearRect(0,0,w,h);
      if(!baseOnly){
        if(this.backdropDirty){
          this.backdrop??=document.createElement("canvas");this.backdrop.width=this.canvas.width;this.backdrop.height=this.canvas.height;const cache=this.backdrop.getContext("2d"),ratio=this.canvas.width/w;cache.setTransform(ratio,0,0,ratio,0,0);this.ctx=cache;
          try{this.draw(0,true);}finally{this.ctx=ctx;}this.backdropDirty=false;
        }
        ctx.drawImage(this.backdrop,0,0,w,h);
        if(this.motion&&!this.flow){const sectors=new Map(this.items.map(row=>[row.id,row])),stocks=new Map(this.stocks.map(row=>[row.code,row]));this.edges.forEach((edge,index)=>{const a=sectors.get(edge.source),b=stocks.get(edge.target.replace("stock:",""));if(a&&b)this.meteor([a.position[0],a.position[1]-.3,a.position[2]],b.position,index,time,this.pulseLinks.get("stock:"+b.code)??-10000,850,1800);});this.items.forEach(row=>this.meteor([0,3.8,-3.7],row.position,row.index,time,this.pulseLinks.get("sector:"+row.id)??-10000,0,850,true));}
        const stamp=performance.now();if(!this.motion||!this.visible||document.hidden){this.canvas.dataset.renderFps="0";this.frameMark=0;this.frameCount=0;}else if(!this.frameMark){this.frameMark=stamp;}else if(++this.frameCount&&stamp-this.frameMark>=1000){this.canvas.dataset.renderFps=(this.frameCount*1000/(stamp-this.frameMark)).toFixed(1);this.frameMark=stamp;this.frameCount=0;}return;
      }
      const glow=ctx.createRadialGradient(w*.5,h*.67,0,w*.5,h*.67,w*.5);glow.addColorStop(0,"#717a8437");glow.addColorStop(1,"#06060700");ctx.fillStyle=glow;ctx.fillRect(0,0,w,h);
      const sectorMap=new Map(this.items.map(r=>[r.id,r])),stockMap=new Map(this.stocks.map(r=>[r.code,r]));
      this.edges.forEach((edge,index)=>{
        const a=sectorMap.get(edge.source),b=stockMap.get(edge.target.replace("stock:",""));if(!a||!b)return;
        const selected=this.selected===a.id||this.selected===b.code;
        this.curve([a.position[0],a.position[1]-.3,a.position[2]],b.position,a.color,index,time,selected,-10000,false);
      });
      const master={position:[0,3.8,-3.7],index:0,color:"#c6d5e8"};
      this.items.forEach(r=>this.path([master.position,r.position],"#a3afbf50",.6));
      [...this.items].sort((a,b)=>a.position[2]-b.position[2]).forEach(r=>this.instrument(r,time));
      this.instrument(master,time,true);
      const point=this.project([0,3,-3.7]);ctx.textAlign="center";ctx.fillStyle="#99a4b3";ctx.font="9px monospace";ctx.fillText("MARKET OBSERVATION",point[0],point[1]);
      this.stocks.forEach(r=>{
        const p=this.project(r.position);ctx.fillStyle="#aebbcc";ctx.fillRect(p[0]-1,p[1]-1,2,2);
        this.path([[r.position[0]-.18,r.position[1]-.025,r.position[2]-.1],[r.position[0]+.18,r.position[1]-.025,r.position[2]-.1],[r.position[0]+.18,r.position[1]-.025,r.position[2]+.1],[r.position[0]-.18,r.position[1]-.025,r.position[2]+.1]],"#9da9b88c",.6,"#2f323770");
      });
      if(!this.items.length){ctx.font='12px "PingFang SC",sans-serif';ctx.fillStyle="#9ca8b7";ctx.fillText("正在等待可关联的板块与个股数据",w*.5,h*.55);}
    }
    layoutLabels(){this.labels.forEach(({button,row,kind})=>{
        const p=this.project([row.position[0],row.position[1]-(kind==="sector"?.62:.25),row.position[2]]);
        button.style.left=p[0]+"px";button.style.top=p[1]+"px";
        button.style.maxWidth=Math.max(48,Math.min(100,this.width/(kind==="sector"?this.items.length:this.stockColumns)-12))+"px";
        button.style.opacity=this.selected&&this.selected!==row.id&&this.selected!==row.code?".72":"1";
      });}
    setSelected(id){this.selected=id;this.backdropDirty=true;this.layoutLabels();this.draw(performance.now()/1000);}
    pulseUpdates({stockCodes=[],sectorIds=[]}={}){
      if(!this.motion||document.hidden||!this.visible)return [];const codes=new Set(stockCodes),sectors=new Set(sectorIds),now=performance.now(),related=this.items.filter(item=>sectors.has(item.id)||item.stock_codes.some(code=>codes.has(code))).slice(0,3),selected=new Set(related.map(item=>item.id));
      const targets=new Set(this.edges.filter(edge=>codes.has(edge.target.replace("stock:",""))||(sectors.has(edge.source)&&selected.has(edge.source))).slice(0,4).map(edge=>edge.target.replace("stock:","")));
      for(const id of selected)if(now-(this.pulseLinks.get("sector:"+id)??-10000)>4000)this.pulseLinks.set("sector:"+id,now);for(const code of targets)if(now-(this.pulseLinks.get("stock:"+code)??-10000)>4000)this.pulseLinks.set("stock:"+code,now);
      this.flow?.pulse(track=>(track.kind==="uplink"&&selected.has(track.sectorId))||targets.has(track.stockCode),now);
      this.canvas.__stLastPulse={sectors:[...selected],stocks:[...targets],at:now};return [...[...selected].map(id=>({id,delay:850})),...[...targets].map(id=>({id,delay:2650}))];
    }
    setMotion(enabled){this.motion=enabled;this.flow?.setRunning(enabled&&this.visible);this.draw(performance.now()/1000);}
    run(stamp){if(!document.hidden&&this.motion&&this.visible)this.draw(stamp/1000);this.frame=requestAnimationFrame(this.run);}
    destroy(){cancelAnimationFrame(this.frame);this.flow?.destroy();this.resizeObserver.disconnect();this.visibilityObserver?.disconnect();this.overlay.replaceChildren();}
  }
  window.LumoShortTermSceneFallback=ShortTermScene;
})();
