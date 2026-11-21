/* DOM nodes share screen coordinates with the local Three.js and Canvas renderers. */
(() => {
  "use strict";

  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  }

  const colors = {bull: "#a53e37", bear: "#15735d", market: "#446383"};

  class MarketCorrelationScene {
    constructor(container) {
      this.container = container;
      container.classList.add("mcorr-scene");
      this.board = element("div", "mcorr-board");
      this.detail = element("div", "mcorr-detail");
      this.detail.setAttribute("role", "status");
      this.detail.setAttribute("aria-atomic", "true");
      this.canvas = element("canvas");
      this.canvas.setAttribute("aria-hidden", "true");
      this.board.append(this.canvas);
      container.replaceChildren(this.board, this.detail);
      this.motionLayer = element("div", "mcorr-motion");
      this.board.append(this.motionLayer);
      this.reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
      if (window.LumoShortTermFlow && Element.prototype.animate) {
        this.flow = new window.LumoShortTermFlow(this.motionLayer);
        this.onMotionChange = () => this.flow.setRunning(this.visible && !this.reducedMotion.matches);
        this.visibility = new IntersectionObserver(([entry]) => {
          this.visible = entry.isIntersecting;
          this.onMotionChange();
        });
        this.visibility.observe(this.board);
        this.reducedMotion.addEventListener("change", this.onMotionChange);
      }
      this.useCanvas();
      this.observer = new ResizeObserver(() => this.draw());
      this.observer.observe(this.board);
      this.enableWebGL();
    }

    useCanvas() {
      this.canvas.dataset.engine = "canvas2d";
      this.context = this.canvas.getContext("2d");
    }

    async enableWebGL() {
      try {
        const THREE = await import("./vendor/three-r180/three.module.min.js");
        if (this.destroyed) return;
        const canvas = this.canvas.cloneNode();
        const context = canvas.getContext("webgl2", {alpha: true, antialias: true, powerPreference: "low-power"});
        if (!context) return;
        this.renderer = new THREE.WebGLRenderer({canvas, context, alpha: true, antialias: true});
        this.three = THREE;
        this.scene = new THREE.Scene();
        this.camera = new THREE.OrthographicCamera(0, 1, 0, 1, 0.1, 10);
        this.camera.position.z = 1;
        this.renderer.setClearColor(0, 0);
        canvas.dataset.engine = "three";
        this.canvas.replaceWith(canvas);
        this.canvas = canvas;
        this.context = null;
        canvas.addEventListener("webglcontextlost", () => this.restoreCanvas(), {once: true});
        this.draw();
      } catch {
        this.restoreCanvas();
      }
    }

    restoreCanvas() {
      if (this.destroyed || !this.renderer) return;
      this.clearLines();
      this.renderer.dispose();
      this.renderer = null;
      const canvas = this.canvas.cloneNode();
      this.canvas.replaceWith(canvas);
      this.canvas = canvas;
      this.useCanvas();
      this.draw();
    }

    setNetwork(network) {
      this.board.replaceChildren(this.canvas, this.motionLayer);
      this.nodes = [];
      this.selected = null;
      for (const side of ["bull", "bear"]) {
        const root = network?.[side];
        if (!root) continue;
        const panel = element("section", "mcorr-side");
        panel.dataset.side = side;
        panel.setAttribute("aria-label", root.label);
        const header = element("div", "mcorr-side-head");
        const rootNode = this.addNode(header, root, side, "root");
        const dimensions = root.children || [];
        header.append(element("small", "", dimensions.length + " 个驱动因素"));
        panel.append(header);
        if (!dimensions.length) panel.append(element("p", "mcorr-empty", "当前未检出相关驱动因素"));
        for (const dimension of dimensions) {
          const group = element("div", "mcorr-group");
          const category = this.addNode(group, dimension, side, "dimension", rootNode);
          const leaves = element("div", "mcorr-leaves");
          for (const signal of dimension.children || []) this.addNode(leaves, signal, side, "signal", category);
          group.append(leaves);
          panel.append(group);
        }
        this.board.append(panel);
      }
      this.detail.hidden = !this.nodes.length;
      const first = this.nodes.find(node => node.kind === "signal") || this.nodes[0];
      if (first) this.select(first);
      else {
        this.board.append(element("p", "mcorr-empty", "当前暂无关联网络数据"));
        this.draw();
      }
    }

    setSections(groups, onSelect, current) {
      this.board.classList.add("mcorr-sections");
      this.detail.hidden = true;
      this.nodes = [];
      const root = element("div", "mcorr-hub", "大盘体检");
      this.board.append(root);
      const hub = {button: root};
      this.nodes.push(hub);
      for (const [label, sections] of groups) {
        const column = element("section", "mcorr-column");
        column.setAttribute("aria-label", label);
        const heading = element("h4", "", label);
        column.append(heading);
        const group = {button: heading, parent: hub, side: "market"};
        this.nodes.push(group);
        for (const [key, name] of sections) {
          const button = element("button", "mcorr-node", name);
          const summary = element("small", "mcorr-node-summary");
          button.append(summary);
          button.type = "button";
          button.dataset.section = key;
          button.setAttribute("aria-controls", "corrEvidence");
          button.setAttribute("aria-pressed", String(key === current));
          const node = {button, summary, parent: group, side: "market"};
          this.nodes.push(node);
          if (key === current) this.selected = node;
          button.addEventListener("click", () => {
            this.selected?.button.setAttribute("aria-pressed", "false");
            this.selected = node;
            button.setAttribute("aria-pressed", "true");
            onSelect(key);
            this.draw();
          });
          column.append(button);
        }
        this.board.append(column);
      }
      this.draw();
    }

    setSummaries(summaries) {
      let changed = false;
      for (const node of this.nodes) {
        if (!node.summary) continue;
        const text = summaries[node.button.dataset.section] || "";
        if (node.summary.textContent === text) continue;
        node.summary.textContent = text;
        changed = true;
      }
      if (changed) this.draw();
    }

    addNode(container, data, side, kind, parent = null) {
      const button = element("button", "mcorr-node", data.label);
      button.type = "button";
      Object.assign(button.dataset, {side, kind, id: data.id});
      button.setAttribute("aria-pressed", "false");
      const node = {data, side, kind, button, parent};
      button.addEventListener("click", () => this.select(node));
      this.nodes.push(node);
      container.append(button);
      return node;
    }

    select(node) {
      this.selected?.button.setAttribute("aria-pressed", "false");
      this.selected = node;
      node.button.setAttribute("aria-pressed", "true");
      const {data} = node;
      this.detail.dataset.side = node.side;
      this.detail.replaceChildren(element("strong", "", data.label));
      if (data.description) this.detail.append(element("span", "", data.description));
      const fields = [];
      if (data.strength) fields.push(data.strength);
      if (Number.isFinite(data.value)) fields.push("指标值 " + Number(data.value.toFixed(2)));
      if (fields.length) this.detail.append(element("small", "", fields.join(" · ")));
      this.draw();
    }

    edges(bounds) {
      const rects = new Map(this.nodes.map(node => [node, node.button.getBoundingClientRect()]));
      const selectedPath = new Set();
      for (let node = this.selected; node; node = node.parent) selectedPath.add(node);
      return this.nodes.filter(node => node.parent).map(node => {
        const source = rects.get(node.parent), target = rects.get(node);
        const active = selectedPath.has(node) || node.parent === this.selected;
        if (node.side === "market") {
          const from = [source.left + source.width / 2 - bounds.left, source.bottom - bounds.top];
          const to = [target.left + target.width / 2 - bounds.left, target.top - bounds.top];
          if (node.button.dataset.section) {
            const left = target.left - bounds.left - 10;
            const y = to[1] + target.height / 2;
            return {points: [[left, source.bottom - bounds.top], [left, y], [left + 10, y]], active, side: node.side};
          }
          const y = (from[1] + to[1]) / 2;
          return {points: [from, [from[0], y], [to[0], y], to], active, side: node.side};
        }
        const isRoot = node.parent.kind === "root";
        const fromX = (isRoot ? source.left + 7 : source.right) - bounds.left;
        const fromY = (isRoot ? source.bottom : source.top + source.height / 2) - bounds.top;
        const toX = target.left - bounds.left - 2, toY = target.top + target.height / 2 - bounds.top;
        const middle = (fromX + toX) / 2;
        const points = [[fromX, fromY]];
        if (isRoot) points.push([fromX, toY - 8]);
        const [startX, startY] = points[points.length - 1];
        const c1x = isRoot ? startX : middle, c1y = isRoot ? toY : startY;
        for (let step = 1; step <= 20; step++) {
          const t = step / 20, u = 1 - t;
          points.push([
            u**3 * startX + 3*u*u*t*c1x + 3*u*t*t*middle + t**3 * toX,
            u**3 * startY + 3*u*u*t*c1y + 3*u*t*t*toY + t**3 * toY,
          ]);
        }
        return {points, active, side: node.side};
      }).sort((a, b) => Number(a.active) - Number(b.active));
    }

    draw() {
      const rect = this.board.getBoundingClientRect();
      if (!this.nodes || !rect.width || !rect.height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const edges = this.edges(rect);
      if (this.flow) {
        this.flow.setPaths(edges.filter(edge => edge.active));
        this.onMotionChange();
      }
      if (this.renderer) {
        this.clearLines();
        this.renderer.setPixelRatio(dpr);
        this.renderer.setSize(rect.width, rect.height, false);
        this.camera.right = rect.width;
        this.camera.bottom = rect.height;
        this.camera.updateProjectionMatrix();
        const T = this.three;
        for (const {points, active, side} of edges) {
          const geometry = new T.BufferGeometry().setFromPoints(points.map(([x, y]) => new T.Vector3(x, y, 0)));
          const material = new T.LineBasicMaterial({color: colors[side], transparent: true, opacity: active ? 1 : 0.45, depthTest: false});
          this.scene.add(new T.Line(geometry, material));
        }
        this.renderer.render(this.scene, this.camera);
      } else if (this.context) {
        const ctx = this.context;
        this.canvas.width = Math.round(rect.width * dpr);
        this.canvas.height = Math.round(rect.height * dpr);
        ctx.scale(dpr, dpr);
        for (const {points, active, side} of edges) {
          ctx.beginPath();
          ctx.strokeStyle = colors[side];
          ctx.lineWidth = active ? 2 : 1;
          ctx.globalAlpha = active ? 1 : 0.45;
          ctx.moveTo(...points[0]);
          for (const point of points.slice(1)) ctx.lineTo(...point);
          ctx.stroke();
        }
      }
    }

    clearLines() {
      for (const line of this.scene?.children || []) {
        line.geometry.dispose();
        line.material.dispose();
      }
      this.scene?.clear();
    }

    destroy() {
      this.destroyed = true;
      this.observer.disconnect();
      this.visibility?.disconnect();
      if (this.flow) {
        this.reducedMotion.removeEventListener("change", this.onMotionChange);
        this.flow.destroy();
      }
      this.clearLines();
      this.renderer?.dispose();
      this.container.replaceChildren();
    }
  }

  window.LumoMarketCorrelationScene = MarketCorrelationScene;
})();
