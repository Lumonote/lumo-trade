/* 本地 Three.js 场景；线束只表示接口提供的板块成分关系。 */
import * as THREE from "./vendor/three-r180/three.module.min.js";
const COLORS=[0x9bbdb6,0xa8a9c7,0xc3b18a,0xc1a3ad,0x93b5c7,0xa7bac1,0xb5ba8d,0xd9b0a6];
class ShortTermScene {
  constructor(canvas,overlay,select){
    this.canvas=canvas;this.overlay=overlay;this.select=select;this.labels=[];this.items=[];this.streams=[];this.clockStart=performance.now();this.frameMark=0;this.frameCount=0;this.motion=!matchMedia("(prefers-reduced-motion: reduce)").matches;
    try{this.renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true,powerPreference:"low-power"});}
    catch(error){const replacement=canvas.cloneNode();canvas.replaceWith(replacement);return new window.LumoShortTermSceneFallback(replacement,overlay,select);}
    this.renderer.setPixelRatio(Math.min(devicePixelRatio||1,1.25));this.renderer.setClearColor(0x040609,1);this.renderer.outputColorSpace=THREE.SRGBColorSpace;
    this.scene=new THREE.Scene();this.camera=new THREE.OrthographicCamera(-16,16,9,-9,.1,100);this.camera.position.set(0,7.8,32);this.camera.lookAt(0,4.4,0);
    this.scene.add(new THREE.AmbientLight(0xcbd4e3,2));
    const key=new THREE.DirectionalLight(0xe3edfa,4);key.position.set(-8,15,10);this.scene.add(key);
    const rim=new THREE.DirectionalLight(0x9bbcc5,1.5);rim.position.set(9,4,-5);this.scene.add(rim);
    this.stage=new THREE.Group();this.scene.add(this.stage);this.content=new THREE.Group();this.scene.add(this.content);
    this.buildStage();this.resizeObserver=new ResizeObserver(()=>this.resize());this.resizeObserver.observe(canvas);this.resize();this.visible=true;
    if(window.IntersectionObserver){this.visibilityObserver=new IntersectionObserver(entries=>{this.visible=entries[0].isIntersecting;this.flow?.setRunning(this.motion&&this.visible);});this.visibilityObserver.observe(canvas);}
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
    this.batchLines(this.master);this.batchLines(this.stage);
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
    button.title=row.name+(kind==="stock"?" "+row.code:"");button.addEventListener("click",()=>this.select(kind,kind==="sector"?row.id:row.code));this.overlay.append(button);this.labels.push({button,row,kind,position:new THREE.Vector3(...position)});
  }
  setData(data){
    this.flow?.destroy();this.flow=null;
    this.curveTexture?.dispose();this.curveTexture=null;this.disposeGroup(this.content);this.content.clear();this.highlight=null;this.packets=null;this.streams=[];this.overlay.replaceChildren();this.labels=[];
    const sectors=new Map(data.sectors.map(r=>[r.id,r])),stocks=new Map(data.stocks.map(r=>[r.code,r]));
    this.items=(data.graph?.sector_ids||[]).map(id=>sectors.get(id)).filter(Boolean).slice(0,12).map((row,i,all)=>({...row,index:i,color:COLORS[i%COLORS.length],position:[all.length===1?0:-10.8+i*21.6/(all.length-1),6.74+[0,.5,-.15,.33,-.21,.12,.48,-.06][i%8],-.25]}));
    const lookup=new Map(this.items.map(r=>[r.id,r])),codes=[...new Set(data.graph?.stock_codes||[])],columns=Math.max(1,Math.min(data.graph?.columns||8,codes.length));
    const viewportWidth=this.width||this.canvas.clientWidth||960,viewportHeight=this.height||this.canvas.clientHeight||480,worldPerPixel=(this.camera.top-this.camera.bottom)/viewportHeight,rowGap=Math.max(1.2,34*worldPerPixel),firstRow=3.63-Math.max(.93,36*worldPerPixel),cardWidth=Math.min(100,viewportWidth/columns-8),halfSpan=Math.min(10.8,(this.camera.right-this.camera.left)/2*(1-(cardWidth+16)/viewportWidth));
    this.stockColumns=columns;const positions=new Map(codes.map((code,i)=>{const col=i%columns,row=Math.floor(i/columns),count=Math.min(columns,codes.length-row*columns);return [code,[count===1?0:-halfSpan+col*2*halfSpan/(count-1),firstRow-row*rowGap,.65]];}));
    for(const item of this.items){const group=this.device(item.index,item.color);group.scale.setScalar(.8);group.position.set(...item.position);this.content.add(group);item.group=group;this.line(this.content,[[0,8.3,-1],item.position],0x6f7f87,.42);this.addStream(new THREE.LineCurve3(new THREE.Vector3(0,8.3,-1),new THREE.Vector3(...item.position)),item.color,item.index,{kind:"uplink",sectorId:item.id});this.label(item,"sector",[item.position[0],3.63,1],item.color);}
    (data.graph?.edges||[]).forEach((e,i)=>{const code=e.target.replace("stock:",""),a=lookup.get(e.source),b=positions.get(code);if(a&&b)this.cable(this.content,[a.position[0],a.position[1]-.4,0],b,a.color,.32,i,false,{kind:"membership",sectorId:a.id,stockCode:code});});
    codes.forEach((code,i)=>{const row=stocks.get(code);if(row)this.label(row,"stock",positions.get(code),COLORS[i%COLORS.length]);});
    if(this.streams.length){
      const geo=new THREE.BufferGeometry(),trail=[],streamRows=[],phases=[],events=[],timing=[],curveData=new Float32Array(this.streams.length*128*4);
      this.streams.forEach((stream,i)=>{
        for(let point=0;point<128;point++){const source=point*3,target=(i*128+point)*4;curveData[target]=stream.samples[source];curveData[target+1]=stream.samples[source+1];curveData[target+2]=stream.samples[source+2];curveData[target+3]=1;}
        for(let lane=0;lane<2;lane++)for(let j=0;j<24;j++){trail.push(1-j/24);streamRows.push((i+.5)/this.streams.length);phases.push(i*.137);events.push(lane);timing.push(stream.kind==="uplink"?0:.85,stream.kind==="uplink"?.85:1.8);}
      });
      // 曲线只上传一次；常态流星与新数据流星各走独立轨道，后者不会重置前者。
      this.curveTexture=new THREE.DataTexture(curveData,128,this.streams.length,THREE.RGBAFormat,THREE.FloatType);this.curveTexture.minFilter=THREE.NearestFilter;this.curveTexture.magFilter=THREE.NearestFilter;this.curveTexture.generateMipmaps=false;this.curveTexture.needsUpdate=true;
      geo.setAttribute("position",new THREE.Float32BufferAttribute(new Float32Array(trail.length*3),3));geo.setAttribute("aTrail",new THREE.Float32BufferAttribute(trail,1));geo.setAttribute("aStream",new THREE.Float32BufferAttribute(streamRows,1));geo.setAttribute("aPhase",new THREE.Float32BufferAttribute(phases,1));geo.setAttribute("aEvent",new THREE.Float32BufferAttribute(events,1));geo.setAttribute("aTiming",new THREE.Float32BufferAttribute(timing,2));geo.setAttribute("aPulseAt",new THREE.Float32BufferAttribute(new Float32Array(trail.length).fill(-10000),1));
      const material=new THREE.ShaderMaterial({uniforms:{uScale:{value:this.renderer.getPixelRatio()},uTime:{value:0},uCurves:{value:this.curveTexture}},vertexShader:`
        attribute float aTrail;attribute float aStream;attribute float aPhase;attribute float aEvent;attribute float aPulseAt;attribute vec2 aTiming;
        uniform float uScale;uniform float uTime;uniform sampler2D uCurves;varying float vTrail;varying float vBoost;varying float vVisible;
        void main(){
          float age=(uTime-aPulseAt-aTiming.x)/aTiming.y;
          float head=mix(fract(uTime*.145+aPhase)*1.18,age,aEvent);
          float progress=head-(1.-aTrail)*.156;
          float live=mix(1.,step(0.,age)*step(age,1.18),aEvent);
          vTrail=aTrail;vBoost=aEvent*max(0.,sin(clamp(age/1.18,0.,1.)*3.14159265));
          vVisible=live*step(0.,progress)*step(progress,1.)*smoothstep(0.,.035,head)*(1.-smoothstep(1.,1.18,head));
          if(vVisible<.001){gl_Position=vec4(2.,2.,2.,1.);gl_PointSize=0.;return;}
          float sampleAt=clamp(progress,0.,1.)*127.,lo=floor(sampleAt),hi=min(127.,lo+1.);
          vec3 a=texture2D(uCurves,vec2((lo+.5)/128.,aStream)).xyz;
          vec3 b=texture2D(uCurves,vec2((hi+.5)/128.,aStream)).xyz;
          gl_Position=projectionMatrix*modelViewMatrix*vec4(mix(a,b,fract(sampleAt)),1.);
          gl_PointSize=(1.8+5.5*pow(aTrail,4.)+3.*vBoost)*uScale;
        }`,fragmentShader:`varying float vTrail;varying float vBoost;varying float vVisible;void main(){vec2 p=gl_PointCoord*2.-1.;float r=max(0.,1.-dot(p,p));float soft=pow(r,2.);vec3 tint=mix(vec3(.58,.55,.82),vec3(.64,.79,.98),vTrail);vec3 color=mix(tint,vec3(.94,.98,1.),pow(vTrail,8.)*pow(r,3.));float alpha=soft*pow(vTrail,1.5)*(.38+.55*vBoost)*vVisible;if(alpha<.005)discard;gl_FragColor=vec4(color,alpha);}`,transparent:true,blending:THREE.AdditiveBlending,depthWrite:false,depthTest:false,toneMapped:false});
      this.packets=new THREE.Points(geo,material);this.packets.frustumCulled=false;this.content.add(this.packets);this.canvas.__stMeteorCount=this.streams.length;this.canvas.dataset.flowEngine="gpu-timeline";this.canvas.dataset.flowBuild="20261102-smooth";this.animatePackets(performance.now());
    }
    if(window.LumoShortTermFlow&&Element.prototype.animate){this.flow=new window.LumoShortTermFlow(this.overlay);this.layoutFlows();if(this.packets)this.packets.visible=false;this.canvas.dataset.flowEngine="compositor-timeline";this.canvas.dataset.flowBuild="20261102-compositor";}
    this.batchLines(this.content);this.setSelected(this.selected||this.items.at(-1)?.id||"");this.layoutLabels();this.render();
  }
  layoutFlows(){if(!this.flow)return;this.flow.setPaths(this.streams.map(stream=>({...stream,points:Array.from({length:128},(_,i)=>{const p=new THREE.Vector3().fromArray(stream.samples,i*3).project(this.camera);return [(p.x+1)*this.width/2,(1-p.y)*this.height/2];})})),performance.now()-this.clockStart);this.flow.setRunning(this.motion&&this.visible);}
  setSelected(id){
    this.selected=id;if(this.highlight){this.disposeGroup(this.highlight);this.content.remove(this.highlight);}this.highlight=new THREE.Group();this.content.add(this.highlight);
    const row=this.items.find(r=>r.id===id||r.stock_codes.includes(id));if(row)for(let i=0;i<5;i++)this.cable(this.highlight,[row.position[0]+(i-2)*.04,row.position[1]-.32,.2],[row.position[0]+(i-2)*.16,4.12,1],0xf4d6d0,.85,i,true);this.batchLines(this.highlight);this.layoutLabels();this.render();
  }
  setMotion(value){this.motion=value;this.flow?.setRunning(value&&this.visible);this.render();}
  pulseUpdates({stockCodes=[],sectorIds=[]}={}){
    if(!this.motion||document.hidden||!this.visible)return [];const time=performance.now(),codes=new Set(stockCodes),sectors=new Set(sectorIds);
    const related=this.items.filter(item=>sectors.has(item.id)||item.stock_codes.some(code=>codes.has(code))).slice(0,3);const selectedSectors=new Set(related.map(item=>item.id));
    const selectedCodes=new Set(this.streams.filter(s=>s.kind==="membership"&&(codes.has(s.stockCode)||(sectors.has(s.sectorId)&&selectedSectors.has(s.sectorId)))).slice(0,4).map(s=>s.stockCode));
    const pulseAttr=this.packets?.geometry.attributes.aPulseAt;let updated=false;
    this.streams.forEach((stream,i)=>{if(((stream.kind==="uplink"&&selectedSectors.has(stream.sectorId))||selectedCodes.has(stream.stockCode))&&time-stream.pulseAt>4000){stream.pulseAt=time;if(pulseAttr)for(let j=24;j<48;j++)pulseAttr.setX(i*48+j,(time-this.clockStart)/1000);updated=true;}});
    if(updated&&pulseAttr)pulseAttr.needsUpdate=true;
    this.flow?.pulse(stream=>(stream.kind==="uplink"&&selectedSectors.has(stream.sectorId))||selectedCodes.has(stream.stockCode),time);
    const hits=[...selectedSectors].map(id=>({id,delay:850}));for(const code of selectedCodes)if(this.labels.some(label=>label.kind==="stock"&&label.row.code===code))hits.push({id:code,delay:2650});
    this.canvas.__stLastPulse={sectors:[...selectedSectors],stocks:[...selectedCodes],at:time};return hits;
  }
  resize(){const {width,height}=this.canvas.getBoundingClientRect();if(!width||!height)return;this.width=width;this.height=height;this.renderer.setSize(width,height,false);const span=Math.max(12,28*height/width);this.camera.left=-span*width/height/2;this.camera.right=-this.camera.left;this.camera.top=span/2;this.camera.bottom=-span/2;this.camera.updateProjectionMatrix();this.layoutLabels();this.layoutFlows();this.render();}
  layoutLabels(){for(const item of this.labels){const p=item.position.clone().project(this.camera),spacing=item.kind==="sector"&&this.items.length>1?21.6/(this.items.length-1)*this.width/(this.camera.right-this.camera.left):this.width/(this.stockColumns||1),edgeSpace=item.kind==="sector"?this.width*(1-21.6/(this.camera.right-this.camera.left))-8:100;item.button.style.left=(p.x+1)*this.width/2+"px";item.button.style.top=(1-p.y)*this.height/2+"px";item.button.style.maxWidth=Math.max(42,Math.min(100,spacing-8,edgeSpace))+"px";item.button.style.opacity=this.selected&&this.selected!==item.row.id&&this.selected!==item.row.code?".78":"1";}}
  render(){if(!this.renderer||!this.width)return;this.renderer.render(this.scene,this.camera);this.canvas.__stDrawCalls=this.renderer.info.render.calls;const time=performance.now();if(!this.motion||this.visible===false||document.hidden){this.canvas.dataset.renderFps="0";this.frameMark=0;this.frameCount=0;}else if(!this.frameMark){this.frameMark=time;}else if(++this.frameCount&&time-this.frameMark>=1000){this.canvas.dataset.renderFps=(this.frameCount*1000/(time-this.frameMark)).toFixed(1);this.frameMark=time;this.frameCount=0;}}
  animatePackets(time){if(this.packets)this.packets.material.uniforms.uTime.value=(time-this.clockStart)/1000;}
  tick(time){this.frame=requestAnimationFrame(this.tick);if(document.hidden||!this.motion||!this.visible)return;const begin=performance.now();if(!this.rafMark){this.rafMark=time;this.rafCount=0;this.cpuTotal=0;}this.master.rotation.y=Math.sin(time*.00035)*.1;this.master.position.y=8.9+Math.sin(time*.00065)*.04;for(const row of this.items){row.group.rotation.y=Math.sin(time*.0005+row.index)*.15;row.group.position.y=row.position[1]+Math.sin(time*.0008+row.index)*.035;}this.animatePackets(time);this.render();this.rafCount++;this.cpuTotal+=performance.now()-begin;if(time-this.rafMark>=1000){this.canvas.dataset.rafFps=(this.rafCount*1000/(time-this.rafMark)).toFixed(1);this.canvas.dataset.frameCpuMs=(this.cpuTotal/this.rafCount).toFixed(2);this.rafMark=time;this.rafCount=0;this.cpuTotal=0;}}
  disposeGroup(group){group?.traverse(obj=>{obj.geometry?.dispose();for(const material of Array.isArray(obj.material)?obj.material:[obj.material])material?.dispose();});}
  destroy(){cancelAnimationFrame(this.frame);this.flow?.destroy();this.resizeObserver.disconnect();this.visibilityObserver?.disconnect();this.curveTexture?.dispose();this.disposeGroup(this.scene);this.renderer.dispose();this.overlay.replaceChildren();}
}
window.LumoShortTermScene=ShortTermScene;
