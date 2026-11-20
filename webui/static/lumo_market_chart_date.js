(() => {
  "use strict";
  const root = document.getElementById("marketHealthRoot");
  if (!root) return;
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const selector = "[data-mh-chart-date]";
  const marker = (x, date) => `data-mh-chart-x="${x}" data-mh-chart-date="${esc(date)}"`;
  const hitAt = target => target instanceof Element ? target.closest(selector) : null;

  function hits(points, {left, right, top, bottom}) {
    return points.map(({x, date, title}, i) => {
      const start = i ? (points[i-1].x+x)/2 : left;
      const end = i+1 < points.length ? (x+points[i+1].x)/2 : right;
      return `<rect ${marker(x,date)} x="${start}" y="${top}" width="${end-start}" height="${bottom-top}" fill="transparent" aria-hidden="true">${title?`<title>${esc(title)}</title>`:""}</rect>`;
    }).join("");
  }

  function render(className, label, {width, height, top, bottom}, content) {
    return `<div class="mh-date-chart"><svg class="${className}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}">${content}<line class="mh-date-guide" y1="${top}" y2="${bottom}" visibility="hidden" vector-effect="non-scaling-stroke" aria-hidden="true"/></svg><span class="mh-date-label" aria-hidden="true" hidden></span></div>`;
  }

  function show(mark) {
    const svg = mark.ownerSVGElement, frame = svg.parentElement;
    const matrix = svg.getScreenCTM();
    if (!matrix) return;
    const guide = svg.querySelector(".mh-date-guide"), label = frame.querySelector(".mh-date-label");
    const x = Number(mark.dataset.mhChartX);
    // SVG viewBoxes can be letterboxed; position the label in actual CSS pixels.
    const point = new DOMPoint(x, guide.y2.baseVal.value).matrixTransform(matrix);
    const bounds = frame.getBoundingClientRect();
    label.textContent = mark.dataset.mhChartDate;
    label.hidden = false;
    label.style.left = `${Math.max(4,Math.min(point.x-bounds.left-label.offsetWidth/2,bounds.width-label.offsetWidth-4))}px`;
    label.style.top = `${Math.max(0,Math.min(point.y-bounds.top+3,bounds.height-label.offsetHeight-2))}px`;
    guide.setAttribute("x1", x);
    guide.setAttribute("x2", x);
    guide.removeAttribute("visibility");
    svg.classList.add("mh-date-active");
  }

  function hide(svg) {
    svg.parentElement.querySelector(".mh-date-label").hidden = true;
    svg.querySelector(".mh-date-guide").setAttribute("visibility", "hidden");
    svg.classList.remove("mh-date-active");
  }

  for (const type of ["pointerover", "focusin"]) root.addEventListener(type, e => {
    const mark = hitAt(e.target);
    if (mark && mark !== hitAt(e.relatedTarget)) show(mark);
  });
  for (const type of ["pointerout", "focusout"]) root.addEventListener(type, e => {
    const mark = hitAt(e.target);
    if (mark && mark.ownerSVGElement !== hitAt(e.relatedTarget)?.ownerSVGElement) hide(mark.ownerSVGElement);
  });
  window.addEventListener("resize", () => root.querySelectorAll(".mh-date-active").forEach(hide));
  window.LumoMarketChartDate = {marker, hits, render};
})();
