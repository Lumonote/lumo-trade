/* 本地 Three.js 场景；线束只表示接口提供的板块成分关系。 */
import * as THREE from "./vendor/three-r180/three.module.min.js";
const COLORS=[0x9bbdb6,0xa8a9c7,0xc3b18a,0xc1a3ad,0x93b5c7,0xa7bac1,0xb5ba8d,0xd9b0a6];
class ShortTermScene {
  constructor(canvas,overlay,select){
    this.canvas=canvas;this.overlay=overlay;this.select=select;this.labels=[];this.items=[];this.streams=[];this.motion=!matchMedia("(prefers-reduced-motion: reduce)").matches;
    try{this.renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true,powerPreference:"low-power"});}
    catch(error){const replacement=canvas.cloneNode();canvas.replaceWith(replacement);return new window.LumoShortTermSceneFallback(replacement,overlay,select);}
    this.renderer.setPixelRatio(Math.min(devicePixelRatio||1,1.25));this.renderer.setClearColor(0x040609,1);this.renderer.outputColorSpace=THREE.SRGBColorSpace;
    this.scene=new THREE.Scene();this.camera=new THREE.OrthographicCamera(-16,16,9,-9,.1,100);this.camera.position.set(0,7.8,32);this.camera.lookAt(0,4.4,0);
    this.scene.add(new THREE.AmbientLight(0xcbd4e3,2));
    const key=new THREE.DirectionalLight(0xe3edfa,4);key.position.set(-8,15,10);this.scene.add(key);
    const rim=new THREE.DirectionalLight(0x9bbcc5,1.5);rim.position.set(9,4,-5);this.scene.add(rim);
    this.stage=new THREE.Group();this.scene.add(this.stage);this.content=new THREE.Group();this.scene.add(this.content);
    this.buildStage();this.resizeObserver=new ResizeObserver(()=>this.resize());this.resizeObserver.observe(canvas);this.resize();
    this.frame=0;this.last=0;this.tick=this.tick.bind(this);this.frame=requestAnimationFrame(this.tick);
  }
  metal(color=0xb9c0c5){return new THREE.MeshStandardMaterial({color,metalness:.72,roughness:.38});}
  box(group,x,y,z,w,h,d,color=0xb9c0c5){if(group===this.stage){y=y*.6+2;h*=.6;}const m=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),this.metal(color));m.position.set(x,y,z);group.add(m);return m;}
  cylinder(group,x,y,z,r,h,color=0xb9c0c5,top=r){const m=new THREE.Mesh(new THREE.CylinderGeometry(top,r,h,18),this.metal(color));m.position.set(x,y,z);group.add(m);return m;}
  line(group,points,color=0x65747f,opacity=.5){const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(points.map(p=>new THREE.Vector3(p[0],group===this.stage?p[1]*.6+2:p[1],p[2]))),new THREE.LineBasicMaterial({color,transparent:true,opacity}));group.add(line);return line;}
  batchLines(group){
    const lines=group.children.filter(obj=>obj.isLine&&!obj.isLineLoop);if(lines.length<2)return;
    const positions=[],colors=[],point=new THREE.Vector3();
    for(const line of lines){line.updateMatrix();const attr=line.geometry.attributes.position,color=line.material.color.clone().multiplyScalar((line.material.opacity??1)/.65),step=line.isLineSegments?2:1;
      for(let i=0;i<attr.count-1;i+=step)for(const index of [i,i+1]){point.fromBufferAttribute(attr,index).applyMatrix4(line.matrix);positions.push(point.x,point.y,point.z);colors.push(color.r,color.g,color.b);}
      group.remove(line);line.geometry.dispose();line.material.dispose();
    }
    const geometry=new THREE.BufferGeometry();geometry.setAttribute("position",new THREE.Float32BufferAttribute(positions,3));geometry.setAttribute("color",new THREE.Float32BufferAttribute(colors,3));
    group.add(new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.65})));
  }
  wireBox(group,x,y,z,w,h,d,color=0x6f848b){if(group===this.stage){y=y*.6+2;h*=.6;}const line=new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(w,h,d)),new THREE.LineBasicMaterial({color,transparent:true,opacity:.6}));line.position.set(x,y,z);group.add(line);return line;}
  solar(group,x,y,width=1.8){
    this.box(group,x,y,0,width,.55,.035,0x26354b);
    for(let i=0;i<=8;i++)this.line(group,[[x-width/2+i*width/8,y-.275,.024],[x-width/2+i*width/8,y+.275,.024]],0x9aaac2,.65);
    for(let i=0;i<=3;i++)this.line(group,[[x-width/2,y-.275+i*.55/3,.024],[x+width/2,y-.275+i*.55/3,.024]],0x9aaac2,.65);
  }
  satellite(group){
    this.box(group,0,0,0,.85,1.04,.63,0xc2c6c7);this.box(group,0,0,.34,.7,.8,.035,0xb8ad8a);
    for(let i=0;i<8;i++)this.line(group,[[-.3,-.35+i*.1,.365],[.3,-.35+i*.1,.365]],0x7c8993,.8);
    this.solar(group,-1.48,0,1.9);this.solar(group,1.48,0,1.9);this.box(group,0,.67,0,.35,.1,.3);this.cylinder(group,0,.8,0,.025,.2);
    const dish=this.cylinder(group,0,-.8,.08,.55,.12,0xd7dcde,.22);dish.rotation.x=.6;
    for(const p of [[0,-.9,.7],[-.4,-.9,.38],[.4,-.9,.38]])this.line(group,[[0,-.53,.25],p],0xb8c4c7,.8);
  }
  device(index,color){
    const g=new THREE.Group();
    switch(index%8){
      case 0:this.box(g,0,0,0,.72,.48,.5);this.cylinder(g,0,.32,0,.03,.2);this.box(g,0,.46,0,.95,.035,.12,0x78868d);{const lens=this.cylinder(g,0,-.03,.4,.18,.35,0x7e8d91);lens.rotation.x=Math.PI/2;}break;
      case 1:this.cylinder(g,0,0,0,.34,.62);this.cylinder(g,0,.4,0,.025,.25);this.cylinder(g,0,-.32,0,.35,.05,0x7b8e93);break;
      case 2:this.box(g,0,0,0,.55,.33,.4);this.line(g,[[-.7,.05,-.3],[.7,.05,.3]],0xb6c4c7,.9);this.line(g,[[-.7,.05,.3],[.7,.05,-.3]],0xb6c4c7,.9);for(const x of [-.65,.65])for(const z of [-.3,.3])this.box(g,x,.13,z,.6,.024,.06,0x98a6ac);break;
      case 3:this.box(g,0,0,0,.3,.7,.32,0xc5b48e);this.box(g,0,.43,0,.46,.06,.44);this.box(g,0,-.4,0,.48,.05,.4);this.cylinder(g,0,.56,0,.02,.22);break;
      case 4:this.cylinder(g,0,0,0,.21,.55,0x91a5b0,.3);this.box(g,0,.3,0,.8,.06,.13);this.box(g,0,-.28,0,.5,.06,.18);break;
      case 5:this.box(g,0,0,0,.35,.52,.32,0xa49db9);this.solar(g,-.53,.05,.6);this.solar(g,.53,.05,.6);this.cylinder(g,0,.38,0,.02,.22);break;
      case 6:g.add(new THREE.Mesh(new THREE.SphereGeometry(.36,20,14),this.metal(0xcbd0d2)));this.box(g,.47,0,0,.26,.2,.25);this.cylinder(g,0,.47,0,.02,.25);break;
      default:this.cylinder(g,0,0,0,.35,.23,0xc8ced0,.27);this.cylinder(g,0,.22,0,.1,.2);this.box(g,0,-.24,0,.4,.15,.4);this.line(g,[[0,.3,0],[.17,.65,0]],0xd0d5d5,.8);
    }
    const light=new THREE.Mesh(new THREE.SphereGeometry(.045,8,8),new THREE.MeshBasicMaterial({color}));light.position.set(0,-.4,.2);g.add(light);this.batchLines(g);return g;
  }
  buildStage(){
    this.master=new THREE.Group();this.satellite(this.master);this.master.position.set(0,8.9,-1);this.master.scale.setScalar(.8);this.stage.add(this.master);
    this.box(this.stage,0,3.45,.5,25,.05,1.1,0x2c373d);this.line(this.stage,[[-12.5,3.5,1.1],[12.5,3.5,1.1]],0x8fabb4,.7);
    this.wireBox(this.stage,0,-3.05,2.1,24,.28,5.1,0x718e95);
    for(let x=-12;x<=12;x+=.55)this.line(this.stage,[[x,-2.89,-.4],[x,-2.89,4.6]],0x527078,.19);
    for(let z=-.4;z<=4.6;z+=.4)this.line(this.stage,[[-12,-2.89,z],[12,-2.89,z]],0x527078,.19);
    this.wireBox(this.stage,-3,-1.6,.1,11,2.5,.15);this.wireBox(this.stage,-3,-1.6,.22,10.7,2.2,.04,0x9bb6b9);
    for(let i=0;i<11;i++)this.line(this.stage,[[-8.1,-.65-i*.15,.26],[-3.8+Math.sin(i)*.7,-.65-i*.15,.26]],0x839ba7,.5);
    this.line(this.stage,[[-2.7,-2.1,.26],[-1.5,-1.7,.26],[-.7,-1.8,.26],[.3,-1.1,.26],[1.1,-1.4,.26],[2,-.8,.26]],0xb5c9c6,.8);
    for(const [x,w,h] of [[4.4,2.6,2.1],[7.9,2.1,1.6],[10.5,1.4,2.2]]){this.wireBox(this.stage,x,-2.8+h/2,.6,w,h,.65,0x8c9fa4);for(let i=0;i<5;i++)this.line(this.stage,[[x-w*.4,-2.6+i*.28,.96],[x+w*.4,-2.6+i*.28,.96]],0x758f95,.6);}
    const points=[],colors=[];for(let i=0;i<600;i++){const x=Math.sin(i*13.17)*11.8,z=(Math.sin(i*7.31)+1)*2.5-.4;points.push(x,.29+Math.sin(i*4.71)*.02,z);const c=new THREE.Color(x<0?0x9a99bc:0x8eb8b2);colors.push(c.r,c.g,c.b);}
    const geo=new THREE.BufferGeometry();geo.setAttribute("position",new THREE.Float32BufferAttribute(points,3));geo.setAttribute("color",new THREE.Float32BufferAttribute(colors,3));this.dust=new THREE.Points(geo,new THREE.PointsMaterial({size:.035,vertexColors:true,transparent:true,opacity:.55}));this.stage.add(this.dust);this.batchLines(this.master);this.batchLines(this.stage);
  }
  addStream(curve,color,index,meta={}){const points=curve.getSpacedPoints(127),samples=new Float32Array(points.length*3);points.forEach((p,i)=>p.toArray(samples,i*3));this.streams.push({curve,color,index,samples,pulseAt:-10000,...meta});}
  cable(group,a,b,color,opacity=.38,index=0,thick=false,meta={}){
    const points=[a,[a[0]+Math.sin(index)*.35,a[1]-.8,a[2]+.1],[b[0]+Math.sin(index*1.7)*.3,b[1]+.12,b[2]],b].map(p=>new THREE.Vector3(...p));const curve=new THREE.CatmullRomCurve3(points);
    this.line(group,curve.getPoints(24).map(p=>p.toArray()),color,opacity);
    if(group===this.content)this.addStream(curve,color,index,meta);
  }
  label(row,kind,position,color){
    const button=document.createElement("button");button.type="button";button.className="st-node st-node-"+kind;button.dataset.stNode=kind==="sector"?row.id:row.code;button.style.setProperty("--node-color","#"+new THREE.Color(color).getHexString());
    const title=document.createElement("strong"),small=document.createElement("small");title.textContent=row.name;small.textContent=kind==="sector"?`${row.stock_codes.length}股 · ${row.news_ids.length}消息`:`${row.code} · ${row.change_pct==null?"待报价":Number(row.change_pct).toFixed(2)+"%"}`;button.append(title,small);
    button.addEventListener("click",()=>this.select(kind,kind==="sector"?row.id:row.code));this.overlay.append(button);this.labels.push({button,row,kind,position:new THREE.Vector3(...position)});
  }
  setData(data){
    this.disposeGroup(this.content);this.content.clear();this.highlight=null;this.packets=null;this.streams=[];this.overlay.replaceChildren();this.labels=[];
    const sectors=new Map(data.sectors.map(r=>[r.id,r])),stocks=new Map(data.stocks.map(r=>[r.code,r]));
    this.items=(data.graph?.sector_ids||[]).map(id=>sectors.get(id)).filter(Boolean).slice(0,8).map((row,i,all)=>({...row,index:i,color:COLORS[i],position:[all.length===1?0:-10.8+i*21.6/(all.length-1),6.74+[0,.5,-.15,.33,-.21,.12,.48,-.06][i],-.25]}));
    const lookup=new Map(this.items.map(r=>[r.id,r])),codes=[...(data.graph?.stock_codes||[])].sort();const positions=new Map(codes.map((c,i)=>[c,[codes.length===1?0:-10.8+i*21.6/(codes.length-1),4.1,.7]]));
    for(const item of this.items){const group=this.device(item.index,item.color);group.scale.setScalar(.8);group.position.set(...item.position);this.content.add(group);item.group=group;this.line(this.content,[[0,8.3,-1],item.position],0x6f7f87,.42);this.addStream(new THREE.LineCurve3(new THREE.Vector3(0,8.3,-1),new THREE.Vector3(...item.position)),item.color,item.index,{kind:"uplink",sectorId:item.id});this.label(item,"sector",[item.position[0],3.63,1],item.color);}
    (data.graph?.edges||[]).forEach((e,i)=>{const code=e.target.replace("stock:",""),a=lookup.get(e.source),b=positions.get(code);if(a&&b)this.cable(this.content,[a.position[0],a.position[1]-.4,0],b,a.color,.32,i,false,{kind:"membership",sectorId:a.id,stockCode:code});});
    codes.slice(0,8).forEach((code,i,all)=>{const row=stocks.get(code);if(!row)return;const x=all.length===1?0:-10.5+i*21/(all.length-1);this.cable(this.content,positions.get(code),[x,.32,2.8],COLORS[i%8],.15,i,false,{kind:"terminal",stockCode:code});this.label(row,"stock",[x,.02,3],COLORS[i%8]);});
    if(this.streams.length){
      const geo=new THREE.BufferGeometry(),trail=[];geo.setAttribute("position",new THREE.Float32BufferAttribute(new Float32Array(this.streams.length*24*3),3));
      for(const stream of this.streams)for(let j=0;j<24;j++)trail.push(1-j/24);
      geo.setAttribute("aTrail",new THREE.Float32BufferAttribute(trail,1));geo.setAttribute("aBoost",new THREE.Float32BufferAttribute(new Float32Array(trail.length),1));
      const material=new THREE.ShaderMaterial({uniforms:{uScale:{value:this.renderer.getPixelRatio()}},vertexShader:`attribute float aTrail;attribute float aBoost;uniform float uScale;varying float vTrail;varying float vBoost;void main(){vTrail=aTrail;vBoost=aBoost;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.);gl_PointSize=(1.8+5.5*pow(aTrail,4.)+3.0*aBoost)*uScale;}`,fragmentShader:`varying float vTrail;varying float vBoost;void main(){vec2 p=gl_PointCoord*2.-1.;float r=max(0.,1.-dot(p,p));float soft=pow(r,2.);vec3 tint=mix(vec3(.58,.55,.82),vec3(.64,.79,.98),vTrail);vec3 color=mix(tint,vec3(.94,.98,1.),pow(vTrail,8.)*pow(r,3.));float alpha=soft*pow(vTrail,1.5)*(.38+.55*vBoost);if(alpha<.005)discard;gl_FragColor=vec4(color,alpha);}`,transparent:true,blending:THREE.AdditiveBlending,depthWrite:false,depthTest:false,toneMapped:false});
      this.packets=new THREE.Points(geo,material);this.packets.frustumCulled=false;this.content.add(this.packets);this.canvas.__stMeteorCount=this.streams.length;this.animatePackets(performance.now());
    }
    this.batchLines(this.content);this.setSelected(this.selected||this.items.at(-1)?.id||"");this.layoutLabels();this.render();
  }
  setSelected(id){
    this.selected=id;if(this.highlight){this.disposeGroup(this.highlight);this.content.remove(this.highlight);}this.highlight=new THREE.Group();this.content.add(this.highlight);
    const row=this.items.find(r=>r.id===id||r.stock_codes.includes(id));if(row)for(let i=0;i<5;i++)this.cable(this.highlight,[row.position[0]+(i-2)*.04,row.position[1]-.32,.2],[row.position[0]+(i-2)*.16,4.12,1],0xf4d6d0,.85,i,true);this.batchLines(this.highlight);this.layoutLabels();this.render();
  }
  setMotion(value){this.motion=value;this.render();}
  pulseUpdates({stockCodes=[],sectorIds=[]}={}){
    if(!this.motion||document.hidden)return [];const time=performance.now(),codes=new Set(stockCodes),sectors=new Set(sectorIds);
    const related=this.items.filter(item=>sectors.has(item.id)||item.stock_codes.some(code=>codes.has(code))).slice(0,3);const selectedSectors=new Set(related.map(item=>item.id));
    const selectedCodes=new Set(this.streams.filter(s=>s.kind==="membership"&&(codes.has(s.stockCode)||(sectors.has(s.sectorId)&&selectedSectors.has(s.sectorId)))).slice(0,4).map(s=>s.stockCode));
    for(const stream of this.streams)if((stream.kind==="uplink"&&selectedSectors.has(stream.sectorId))||selectedCodes.has(stream.stockCode))if(time-stream.pulseAt>4000)stream.pulseAt=time;
    const hits=[...selectedSectors].map(id=>({id,delay:850}));for(const code of selectedCodes)if(this.labels.some(label=>label.kind==="stock"&&label.row.code===code))hits.push({id:code,delay:3750});
    this.canvas.__stLastPulse={sectors:[...selectedSectors],stocks:[...selectedCodes],at:time};return hits;
  }
  resize(){const {width,height}=this.canvas.getBoundingClientRect();if(!width||!height)return;this.width=width;this.height=height;this.renderer.setSize(width,height,false);const span=Math.max(12,28*height/width);this.camera.left=-span*width/height/2;this.camera.right=-this.camera.left;this.camera.top=span/2;this.camera.bottom=-span/2;this.camera.updateProjectionMatrix();this.layoutLabels();this.render();}
  layoutLabels(){for(const item of this.labels){const p=item.position.clone().project(this.camera);item.button.style.left=(p.x+1)*this.width/2+"px";item.button.style.top=(1-p.y)*this.height/2+"px";item.button.style.opacity=this.selected&&this.selected!==item.row.id&&this.selected!==item.row.code?".78":"1";}}
  render(){if(!this.renderer||!this.width)return;this.renderer.render(this.scene,this.camera);this.canvas.__stDrawCalls=this.renderer.info.render.calls;}
  animatePackets(time){
    if(!this.packets)return;const attr=this.packets.geometry.attributes.position,boostAttr=this.packets.geometry.attributes.aBoost;
    this.streams.forEach((stream,i)=>{const delay=stream.kind==="uplink"?0:stream.kind==="membership"?850:2650,duration=stream.kind==="uplink"?850:stream.kind==="membership"?1800:1100,age=time-stream.pulseAt-delay,active=age>=0&&age<duration+350;
      const head=active?Math.pow(Math.min(1,age/duration),1.2):Math.pow((time*.000145+i*.137)%1,1.22),boost=active?Math.sin(Math.PI*Math.min(1,age/(duration+350))):0,samples=stream.samples;
      for(let j=0;j<24;j++){const t=head-j*.0065,index=i*24+j;if(t<0){attr.setXYZ(index,0,-1000,0);boostAttr.setX(index,0);continue;}const scaled=t*127,lo=Math.min(126,Math.floor(scaled)),mix=scaled-lo,a=lo*3,b=a+3;attr.setXYZ(index,samples[a]+(samples[b]-samples[a])*mix,samples[a+1]+(samples[b+1]-samples[a+1])*mix,samples[a+2]+(samples[b+2]-samples[a+2])*mix);boostAttr.setX(index,boost);}
    });attr.needsUpdate=true;boostAttr.needsUpdate=true;
  }
  tick(time){this.frame=requestAnimationFrame(this.tick);if(document.hidden||!this.motion||time-this.last<32)return;this.last=time;this.master.rotation.y=Math.sin(time*.00035)*.1;this.master.position.y=8.9+Math.sin(time*.00065)*.04;for(const row of this.items){row.group.rotation.y=Math.sin(time*.0005+row.index)*.15;row.group.position.y=row.position[1]+Math.sin(time*.0008+row.index)*.035;}this.animatePackets(time);if(this.dust)this.dust.material.opacity=.4+Math.sin(time*.001)*.16;this.render();}
  disposeGroup(group){group?.traverse(obj=>{obj.geometry?.dispose();for(const material of Array.isArray(obj.material)?obj.material:[obj.material])material?.dispose();});}
  destroy(){cancelAnimationFrame(this.frame);this.resizeObserver.disconnect();this.disposeGroup(this.scene);this.renderer.dispose();this.overlay.replaceChildren();}
}
window.LumoShortTermScene=ShortTermScene;
