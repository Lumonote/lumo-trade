/* 由用户点击直接请求全屏；返回时保留原页面、滚动位置与已有表单。 */
(() => {
  "use strict";
  const button = document.getElementById("shortTermScreenBtn");
  if (!button) return;
  let shell = null, frame = null, surface = null, ownedFullscreen = false, priorOverflow = "", opening = false;
  function close() {
    if (!shell) return;
    const element = shell, target = surface;
    shell = null; frame = null; surface = null;
    const done = () => {element.remove();document.body.style.overflow=priorOverflow;button.focus();};
    if (ownedFullscreen && document.fullscreenElement === target) {
      Promise.resolve(document.exitFullscreen()).catch(()=>{}).finally(done);
    } else done();
    ownedFullscreen = false;
  }
  button.addEventListener("click", () => {
    if (shell || opening) return;
    opening = true;
    priorOverflow = document.body.style.overflow;
    shell = document.createElement("dialog");
    shell.setAttribute("aria-label", "短线作战大屏");
    Object.assign(shell.style, {position:"fixed",inset:"0",width:"100vw",height:"100dvh",maxWidth:"none",maxHeight:"none",margin:"0",padding:"0",border:"0",background:"#040609",overflow:"hidden"});
    surface = document.createElement("div");
    Object.assign(surface.style,{width:"100%",height:"100%",background:"#040609"});
    frame = document.createElement("iframe");
    frame.title = "短线作战大屏";frame.src = "/desktop/short_term?embedded=1";frame.allow = "fullscreen";
    Object.assign(frame.style, {width:"100%",height:"100%",border:"0",display:"block"});
    surface.append(frame);shell.append(surface);document.body.append(shell);shell.showModal();document.body.style.overflow="hidden";
    shell.addEventListener("cancel", event=>{event.preventDefault();close();});
    // 原生Fullscreen API禁止将dialog本身全屏，使用其内部的显示容器。
    if (!document.fullscreenElement && surface.requestFullscreen) {
      ownedFullscreen = true;
      Promise.resolve(surface.requestFullscreen()).catch(()=>{ownedFullscreen=false;}).finally(()=>{opening=false;});
    } else opening=false;
  });
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== frame?.contentWindow) return;
    if (event.data?.type === "lumo-short-term-return") close();
    if (event.data?.type === "lumo-short-term-fullscreen") {
      if (document.fullscreenElement && ownedFullscreen) document.exitFullscreen().catch(()=>{});
      else if (!document.fullscreenElement && surface?.requestFullscreen) {ownedFullscreen=true;surface.requestFullscreen().catch(()=>{ownedFullscreen=false;});}
    }
  });
})();
