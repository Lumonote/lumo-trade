/* 将光束的运动交给合成层；数据到达只启动独立事件轨道，不重置常态流星。 */
class ShortTermFlow {
  constructor(overlay){
    this.layer=document.createElement("div");this.layer.className="st-flow-layer";
    this.layer.setAttribute("aria-hidden","true");this.layer.style.cssText="position:absolute;inset:0;pointer-events:none;overflow:hidden;contain:layout paint;";
    overlay.prepend(this.layer);this.tracks=[];this.running=true;
    this.onVisibility=()=>this.sync();document.addEventListener("visibilitychange",this.onVisibility);
  }
  resample(points){
    const distances=[0];for(let i=1;i<points.length;i++)distances.push(distances[i-1]+Math.hypot(points[i][0]-points[i-1][0],points[i][1]-points[i-1][1]));
    const total=distances.at(-1);let cursor=1;
    return Array.from({length:96},(_,i)=>{const at=total*i/95;while(cursor<points.length-1&&distances[cursor]<at)cursor++;const a=points[cursor-1],b=points[cursor],mix=(at-distances[cursor-1])/(distances[cursor]-distances[cursor-1]||1);return [a[0]+(b[0]-a[0])*mix,a[1]+(b[1]-a[1])*mix];});
  }
  frames(points,event){
    let previousAngle=null;
    const frames=points.map((p,i)=>{const a=points[Math.max(0,i-1)],b=points[Math.min(points.length-1,i+1)];let angle=Math.atan2(b[1]-a[1],b[0]-a[0])*180/Math.PI;
      if(previousAngle!==null){while(angle-previousAngle>180)angle-=360;while(angle-previousAngle<-180)angle+=360;}previousAngle=angle;
      const t=i/(points.length-1),opacity=event?Math.min(1,t/.06,(1-t)/.05)*(.3+.7*Math.sin(t*Math.PI)):Math.min(1,t/.06,(1-t)/.08)*(.24+.4*Math.sin(t*Math.PI));
      return {transform:`translate3d(${p[0]}px,${p[1]}px,0) rotate(${angle}deg)`,opacity,offset:t*(event?1:.86)};
    });
    if(!event)frames.push({...frames.at(-1),opacity:0,offset:1});return frames;
  }
  head(event){
    const node=document.createElement("span"),tail=document.createElement("i");node.style.cssText="position:absolute;left:0;top:0;width:0;height:0;opacity:0;will-change:transform,opacity;";
    tail.style.cssText=`display:block;position:absolute;right:0;top:-1px;width:${event?42:30}px;height:${event?2:1.5}px;border-radius:50%;background:linear-gradient(90deg,transparent,#9595ca38 28%,#9dbbe7b0 75%,#f3fbff);box-shadow:2px 0 5px #bbd8ff80;`;
    node.append(tail);this.layer.append(node);return node;
  }
  setPaths(paths,clock=performance.now()){
    for(const track of this.tracks){track.normal.cancel();track.event?.cancel();}this.layer.replaceChildren();
    this.tracks=paths.map((path,index)=>{const points=this.resample(path.points),node=this.head(false),normal=node.animate(this.frames(points,false),{duration:6900,iterations:Infinity,easing:"linear"});normal.currentTime=(clock+index*.137*6900)%6900;return {...path,points,normal,event:null,eventNode:null,pulseAt:-10000};});this.sync();
  }
  pulse(test,now=performance.now()){
    for(const track of this.tracks)if(test(track)&&now-track.pulseAt>4000){track.pulseAt=now;track.event?.cancel();track.eventNode??=this.head(true);track.event=track.eventNode.animate(this.frames(track.points,true),{duration:track.kind==="uplink"?850:1800,delay:track.kind==="uplink"?0:850,easing:"linear",fill:"none"});}this.sync();
  }
  setRunning(value){this.running=value;this.sync();}
  sync(){const play=this.running&&!document.hidden;for(const track of this.tracks)for(const animation of [track.normal,track.event])if(animation&&animation.playState!=="finished")play?animation.play():animation.pause();}
  destroy(){for(const track of this.tracks){track.normal.cancel();track.event?.cancel();}document.removeEventListener("visibilitychange",this.onVisibility);this.layer.remove();}
}
window.LumoShortTermFlow=ShortTermFlow;
