/* TFLens diagram component: one editable canvas shared by the Graph and every diagram
   (Architecture, Module tree, each Flow, a root's Network / IAM diagram). Its look and
   behaviour follow IPLens's diagram (iplens/static/visual.js): the same toolbar — Fit,
   zoom out / in with the live zoom %, reset to 100%, full screen, Reset layout and the
   exports (PNG, SVG, draw.io .drawio) — over a Cytoscape.js canvas (vendored, as dagre).

   Editing, draw.io style:
     - drag a node; drag the background to pan; the wheel zooms;
     - click selects, Shift / Ctrl / Cmd + click adds to the selection, Shift + drag on
       the background draws a selection box; dragging a selected node moves them all;
     - dragging a root / module frame moves it with everything inside;
     - nodes snap to a 20 px grid when "Snap" is on (arrow keys nudge the selection);
     - edges are orthogonal ("taxi") and re-route while a node moves;
     - "Tidy" lays the shown nodes out with dagre.
   The layout is saved per run and view (POST api/layout?view=…, SQLite on the server)
   and restored on reload; "Reset layout" forgets it and goes back to the diagram's own
   layout. Clicking a node or an edge is left to the page (it opens the detail pane);
   hovering shows a short tooltip; the legend shows / hides each edge type.

   The export functions (toSvg, toDrawio) take a plain description of what is drawn and
   need no DOM: under Node.js this file exports them (tests/test_interactive_diagrams.py). */
(function (root) {
  "use strict";

  const GRID = 20;                // snap-to-grid step (model px)
  const ZOOM_STEP = 1.25;
  const MIN_READABLE_ZOOM = 0.55; // a first fit never goes below it (labels stay legible)
  const SAVE_DELAY_MS = 400;
  const EXPORT_PAD = 24;
  const ICON_LABEL_H = 40;        // room under an icon node for its two label lines
  const SANS = "Inter, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
  const KIND_LABELS = {
    TRIGGER: "Trigger", ACCESS: "Access", NETWORK: "Network", CONFIG: "Config",
    RUNS_ON: "Runs on", CONTAINS: "Contains",
  };
  const DASHES = { dashed: "7 4", dotted: "2 4" };

  // ------------------------------------------------------------------ export (no DOM)

  function xml(text) {
    return String(text === undefined || text === null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/\n/g, "&#10;");
  }

  function textLines(text) {
    return String(text || "").split("\n").map((s) => s.trim()).filter(Boolean);
  }

  function round(v) {
    return Math.round(v * 10) / 10;
  }

  function snap(v, grid) {
    const g = grid || GRID;
    return Math.round(v / g) * g;
  }

  // The drawn area: frames, nodes (with an icon node's label below it) and edge points.
  function bounds(view) {
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    const add = (x, y) => {
      x1 = Math.min(x1, x); y1 = Math.min(y1, y); x2 = Math.max(x2, x); y2 = Math.max(y2, y);
    };
    for (const g of view.groups || []) { add(g.x, g.y - 4); add(g.x + g.w, g.y + g.h); }
    for (const n of view.nodes || []) {
      const below = n.shape === "box" ? 0 : ICON_LABEL_H;
      add(n.x - (n.shape === "box" ? 0 : 60), n.y);
      add(n.x + n.w + (n.shape === "box" ? 0 : 60), n.y + n.h + below);
    }
    for (const e of view.edges || []) for (const [x, y] of e.points || []) add(x, y);
    if (x1 === Infinity) return { x1: 0, y1: 0, x2: 200, y2: 80 };
    return { x1, y1, x2, y2 };
  }

  function midpoint(points) {
    if (!points || !points.length) return [0, 0];
    if (points.length === 1) return points[0];
    // the middle of the polyline, by length
    const seg = [];
    let total = 0;
    for (let i = 1; i < points.length; i++) {
      const d = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
      seg.push(d);
      total += d;
    }
    let left = total / 2;
    for (let i = 0; i < seg.length; i++) {
      if (left <= seg[i] || i === seg.length - 1) {
        const t = seg[i] ? left / seg[i] : 0;
        return [points[i][0] + (points[i + 1][0] - points[i][0]) * t,
          points[i][1] + (points[i + 1][1] - points[i][1]) * t];
      }
      left -= seg[i];
    }
    return points[0];
  }

  /* An SVG of what is drawn. `view`: {title, groups: [{id, parent, x, y, w, h, label,
     color, dashed}], nodes: [{id, parent, x, y, w, h, shape: "icon" | "box" | "dot",
     label, sub, color, stroke, icon}], edges: [{id, source, target, points: [[x, y]…],
     color, dash, label, reverse}]} — model coordinates, x / y the top-left corner. */
  function toSvg(view) {
    const b = bounds(view);
    const dx = EXPORT_PAD - b.x1, dy = EXPORT_PAD - b.y1;
    const w = Math.ceil(b.x2 - b.x1 + 2 * EXPORT_PAD), h = Math.ceil(b.y2 - b.y1 + 2 * EXPORT_PAD);
    const colours = [...new Set((view.edges || []).map((e) => e.color || "#57606a"))];
    const marker = (c) => `m${colours.indexOf(c)}`;
    const out = [
      `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" font-family="${xml(SANS)}" font-size="13">`,
      `<title>${xml(view.title || "TFLens diagram")}</title>`,
      "<defs>" + colours.map((c) =>
        `<marker id="${marker(c)}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="${xml(c)}"/></marker>`).join("") + "</defs>",
      `<rect x="0" y="0" width="${w}" height="${h}" fill="${xml(view.bg || "#ffffff")}"/>`,
    ];
    for (const g of view.groups || []) {
      const c = g.color || "#6e7781";
      out.push(`<g class="frame" data-id="${xml(g.id)}">` +
        `<rect x="${round(g.x + dx)}" y="${round(g.y + dy)}" width="${round(g.w)}" height="${round(g.h)}" rx="8" fill="${xml(c)}" fill-opacity="0.05" stroke="${xml(c)}" stroke-width="1.5"${g.dashed ? ' stroke-dasharray="6 4"' : ""}/>` +
        `<text x="${round(g.x + dx + 10)}" y="${round(g.y + dy + 18)}" font-weight="600" fill="${xml(c)}">${xml(g.label)}</text></g>`);
    }
    for (const e of view.edges || []) {
      const pts = (e.points || []).map(([x, y]) => `${round(x + dx)},${round(y + dy)}`);
      if (pts.length < 2) continue;
      const c = e.color || "#57606a";
      const dash = DASHES[e.dash] ? ` stroke-dasharray="${DASHES[e.dash]}"` : "";
      const ends = e.reverse ? ` marker-start="url(#${marker(c)})"` : ` marker-end="url(#${marker(c)})"`;
      out.push(`<path class="edge" data-id="${xml(e.id)}" d="M${pts.join(" L")}" fill="none" stroke="${xml(c)}" stroke-width="2"${dash}${ends}/>`);
      if (e.label) {
        const [mx, my] = midpoint(e.points);
        const lw = String(e.label).length * 6.6 + 8;
        out.push(`<g><rect x="${round(mx + dx - lw / 2)}" y="${round(my + dy - 9)}" width="${round(lw)}" height="16" rx="3" fill="#ffffff" fill-opacity="0.9"/>` +
          `<text x="${round(mx + dx)}" y="${round(my + dy + 3)}" font-size="11" text-anchor="middle" fill="${xml(c)}">${xml(e.label)}</text></g>`);
      }
    }
    for (const n of view.nodes || []) {
      const x = n.x + dx, y = n.y + dy;
      const c = n.stroke || n.color || "#57606a";
      const parts = [`<g class="node" data-id="${xml(n.id)}">`];
      if (n.shape === "box") {
        parts.push(`<rect x="${round(x)}" y="${round(y)}" width="${round(n.w)}" height="${round(n.h)}" rx="8" fill="#ffffff" stroke="${xml(c)}" stroke-width="2"/>`);
        const lines = [n.label, ...textLines(n.sub)];
        const top = y + n.h / 2 - (lines.length - 1) * 9 + 4;
        lines.forEach((t, i) => parts.push(`<text x="${round(x + n.w / 2)}" y="${round(top + i * 18)}" text-anchor="middle"${i ? ' font-size="12" fill="#424a53"' : ' font-weight="600" fill="#1a1f24"'}>${xml(t)}</text>`));
      } else {
        if (n.icon) parts.push(`<image x="${round(x)}" y="${round(y)}" width="${round(n.w)}" height="${round(n.h)}" href="${xml(n.icon)}" xlink:href="${xml(n.icon)}"/>`);
        else parts.push(`<rect x="${round(x)}" y="${round(y)}" width="${round(n.w)}" height="${round(n.h)}" rx="6" fill="${xml(n.color || "#8c959f")}"/>`);
        parts.push(`<rect x="${round(x - 1)}" y="${round(y - 1)}" width="${round(n.w + 2)}" height="${round(n.h + 2)}" rx="7" fill="none" stroke="${xml(c)}" stroke-width="2"/>`);
        [n.label, ...textLines(n.sub)].forEach((t, i) => parts.push(`<text x="${round(x + n.w / 2)}" y="${round(y + n.h + 16 + i * 16)}" text-anchor="middle"${i ? ' font-size="11" fill="#424a53"' : ' font-weight="600" fill="#1a1f24"'}>${xml(t)}</text>`));
      }
      parts.push("</g>");
      out.push(parts.join(""));
    }
    out.push("</svg>");
    return out.join("\n") + "\n";
  }

  // draw.io takes an image as data:image/svg+xml,<base64> (a ";" would end the style).
  function drawioImage(uri) {
    return String(uri || "").replace(/^data:([^;,]+);base64,/, "data:$1,");
  }

  /* The same view as an editable draw.io (diagrams.net) file: frames are containers
     (what is inside moves with them), nodes keep their icon, colour and position, edges
     are orthogonal and keep their bends. */
  function toDrawio(view) {
    const ids = new Map();
    let count = 1;
    const cellId = (key) => {
      if (!ids.has(key)) ids.set(key, `c${++count}`);
      return ids.get(key);
    };
    const origin = new Map(); // container id -> its top-left (children are relative to it)
    const cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>'];
    const geo = (x, y, w, h) =>
      `<mxGeometry x="${round(x)}" y="${round(y)}" width="${round(w)}" height="${round(h)}" as="geometry"/>`;
    const parentOf = (p) => (p && origin.has(p) ? [cellId(p), origin.get(p)] : ["1", [0, 0]]);
    for (const g of view.groups || []) {
      const [parent, [ox, oy]] = parentOf(g.parent);
      const c = g.color || "#6e7781";
      const style = "rounded=1;arcSize=3;whiteSpace=wrap;html=1;container=1;collapsible=0;" +
        "verticalAlign=top;align=left;spacingLeft=10;spacingTop=4;fontStyle=1;fontSize=13;" +
        `fillColor=none;strokeColor=${c};fontColor=${c};` + (g.dashed ? "dashed=1;dashPattern=6 4;" : "");
      cells.push(`<mxCell id="${cellId(g.id)}" value="${xml(g.label)}" style="${xml(style)}" vertex="1" parent="${parent}">${geo(g.x - ox, g.y - oy, g.w, g.h)}</mxCell>`);
      origin.set(g.id, [g.x, g.y]);
    }
    for (const n of view.nodes || []) {
      const [parent, [ox, oy]] = parentOf(n.parent);
      const c = n.stroke || n.color || "#57606a";
      const value = xml(n.label) + textLines(n.sub).map((t) => `&lt;br&gt;&lt;font color=&quot;#424a53&quot; style=&quot;font-size:11px&quot;&gt;${xml(t)}&lt;/font&gt;`).join("");
      let style;
      if (n.shape === "box") {
        style = `rounded=1;arcSize=8;whiteSpace=wrap;html=1;fillColor=#ffffff;strokeColor=${c};strokeWidth=2;fontStyle=1;fontSize=13;`;
      } else if (n.icon) {
        style = `shape=image;html=1;imageAspect=0;aspect=fixed;image=${drawioImage(n.icon)};` +
          `imageBorder=${c};strokeWidth=2;verticalLabelPosition=bottom;verticalAlign=top;labelBackgroundColor=none;fontStyle=1;fontSize=12;`;
      } else {
        style = `rounded=1;whiteSpace=wrap;html=1;fillColor=${n.color || "#8c959f"};strokeColor=${c};verticalLabelPosition=bottom;verticalAlign=top;fontSize=12;`;
      }
      cells.push(`<mxCell id="${cellId(n.id)}" value="${value}" style="${xml(style)}" vertex="1" parent="${parent}">${geo(n.x - ox, n.y - oy, n.w, n.h)}</mxCell>`);
    }
    for (const e of view.edges || []) {
      if (!ids.has(e.source) || !ids.has(e.target)) continue;
      const c = e.color || "#57606a";
      let style = `edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;html=1;strokeWidth=2;strokeColor=${c};fontColor=${c};fontSize=11;labelBackgroundColor=#ffffff;`;
      style += e.reverse ? "startArrow=block;startFill=1;endArrow=none;" : "endArrow=block;endFill=1;";
      if (DASHES[e.dash]) style += `dashed=1;dashPattern=${DASHES[e.dash]};`;
      const bends = (e.points || []).slice(1, -1)
        .map(([x, y]) => `<mxPoint x="${round(x)}" y="${round(y)}"/>`).join("");
      cells.push(`<mxCell id="${cellId(`edge:${e.id}`)}" value="${xml(e.label || "")}" style="${xml(style)}" edge="1" parent="1" source="${cellId(e.source)}" target="${cellId(e.target)}">` +
        `<mxGeometry relative="1" as="geometry">${bends ? `<Array as="points">${bends}</Array>` : ""}</mxGeometry></mxCell>`);
    }
    return '<?xml version="1.0" encoding="UTF-8"?>\n' +
      `<mxfile host="TFLens" type="device"><diagram id="tflens" name="${xml((view.title || "TFLens").slice(0, 80))}">` +
      `<mxGraphModel grid="1" gridSize="${GRID}" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="0" pageScale="1" math="0" shadow="0">` +
      `<root>${cells.join("")}</root></mxGraphModel></diagram></mxfile>\n`;
  }

  const pure = { toSvg, toDrawio, snap, midpoint, bounds, GRID, KIND_LABELS };
  if (typeof module === "object" && module.exports) {
    module.exports = pure;
    return;
  }

  // ------------------------------------------------------------------ browser

  const doc = root.document;
  let icons = {};

  function el(tag, attrs, ...children) {
    const node = doc.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) {
      if (c === null || c === undefined) continue;
      node.append(typeof c === "string" || typeof c === "number" ? String(c) : c);
    }
    return node;
  }

  function themeVar(name, fallback) {
    const v = getComputedStyle(doc.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function iconUrl(name) {
    return (name && icons[name]) || icons.generic || "none";
  }

  function download(name, blob) {
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: name, hidden: true });
    doc.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function sendJson(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "X-TFLens": "1", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then((r) => (r.ok ? r.json() : r.json().then((d) => Promise.reject(new Error(d.detail || d.error || r.statusText)))));
  }

  // Run `fn` once the element has a size (a canvas built in a hidden tab has none).
  function whenVisible(node, fn) {
    if (node.clientWidth && node.clientHeight) { fn(); return; }
    const ro = new ResizeObserver(() => {
      if (!node.clientWidth || !node.clientHeight) return;
      ro.disconnect();
      fn();
    });
    ro.observe(node);
  }

  // Run `fn` once the element is shown and near the viewport: a page of many diagrams
  // (the Flow view) only builds the canvases scrolled to.
  function whenNear(node, fn) {
    if (typeof root.IntersectionObserver !== "function") { whenVisible(node, fn); return; }
    const io = new root.IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) return;
      io.disconnect();
      fn();
    }, { rootMargin: "400px 0px" });
    io.observe(node);
  }

  // The shared look of the diagrams (the Graph adds its own rules on top).
  function canvasStyle() {
    const fg = themeVar("--fg", "#1a1f24");
    const panel = themeVar("--bg-panel", "#ffffff");
    const muted = themeVar("--fg-muted", "#3d454e");
    const accent = themeVar("--accent", "#0757c7");
    const selBg = themeVar("--sel-bg", "#ddeeff");
    const selFg = themeVar("--sel-fg", fg);
    const font = themeVar("--sans", SANS);
    return [
      { selector: "node", style: {
        "font-family": font, "font-size": 13, color: fg, "min-zoomed-font-size": 6,
        "text-wrap": "wrap", "text-overflow-wrap": "anywhere" } },
      { selector: "node.icon-node", style: {
        shape: "round-rectangle", width: 46, height: 46,
        "background-color": panel, "background-image": (n) => iconUrl(n.data("icon")),
        "background-fit": "contain", "background-clip": "node",
        "border-width": 2, "border-color": "data(color)",
        label: "data(text)", "text-valign": "bottom", "text-halign": "center", "text-margin-y": 6,
        "text-max-width": 210, "line-height": 1.3,
        "text-background-color": panel, "text-background-opacity": 0.85, "text-background-padding": 2 } },
      { selector: "node.tree-root", style: { width: 52, height: 52, "font-weight": 600 } },
      { selector: "node.box-node", style: {
        shape: "round-rectangle", width: (n) => n.data("w") || 220, height: (n) => n.data("h") || 70,
        "background-color": panel, "border-width": 2, "border-color": (n) => n.data("stroke") || n.data("color"),
        label: "data(text)", "text-valign": "center", "text-halign": "center", "line-height": 1.35,
        "text-max-width": (n) => `${(n.data("w") || 220) - 20}px` } },
      { selector: "node.hung", style: { "border-style": "dashed" } },
      { selector: "node.frame", style: {
        shape: "round-rectangle", "background-color": themeVar("--group-bg", "#eef1f4"), "background-opacity": 0.5,
        "border-width": 1.5, "border-style": "dashed", "border-color": themeVar("--group-border", "#6e7781"),
        label: "data(label)", "font-size": 13, "font-weight": 600, color: muted,
        "text-valign": "top", "text-halign": "center", "text-margin-y": -4, "text-max-width": 600,
        padding: 26, "min-zoomed-font-size": 5 } },
      { selector: "node.root-frame", style: {
        "border-style": "solid", "border-width": 2, "border-color": themeVar("--tf", "#7340b3"),
        color: themeVar("--tf", "#7340b3"), "font-size": 15, "background-opacity": 0.25 } },
      { selector: "node.net-frame", style: { "border-color": themeVar("--accent", "#0757c7") } },
      { selector: "edge", style: {
        "curve-style": "taxi", "taxi-direction": "auto", "taxi-turn": "50%", "taxi-turn-min-distance": 12,
        width: 2, "line-color": "data(color)", "target-arrow-color": "data(color)", "source-arrow-color": "data(color)",
        "target-arrow-shape": "triangle", "arrow-scale": 0.9,
        label: "data(label)", "font-family": font, "font-size": 11, color: fg,
        "text-background-color": panel, "text-background-opacity": 0.9, "text-background-padding": 2,
        "min-zoomed-font-size": 7 } },
      { selector: "edge.down", style: { "taxi-direction": "downward" } },
      { selector: "edge.dash-dashed", style: { "line-style": "dashed", "line-dash-pattern": [7, 4] } },
      { selector: "edge.dash-dotted", style: { "line-style": "dashed", "line-dash-pattern": [2, 4] } },
      { selector: "edge.reverse", style: { "source-arrow-shape": "triangle", "target-arrow-shape": "none" } },
      { selector: 'edge[kind = "CONTAINS"]', style: { width: 1.5, "target-arrow-shape": "none", opacity: 0.8 } },
      { selector: "edge.hover", style: { width: 3.5, "z-index": 10 } },
      // selected: the theme's selection colours
      { selector: "node:selected", style: {
        "border-color": accent, "border-width": 4, "background-color": selBg, color: selFg,
        "text-background-color": selBg, "text-background-opacity": 1 } },
      { selector: "node.frame:selected", style: { "border-color": accent, "border-width": 3, "background-color": selBg, "background-opacity": 0.35 } },
      { selector: "edge:selected", style: { "line-color": accent, "target-arrow-color": accent, "source-arrow-color": accent, width: 4 } },
      { selector: ".kind-off", style: { display: "none" } },
      { selector: ".dimmed", style: { opacity: 0.15 } },
    ];
  }

  // A canvas payload (api/canvas) as Cytoscape elements.
  function canvasElements(data) {
    const nodes = data.nodes.map((n) => {
      const d = { ...n.data };
      d.text = [d.label, d.sub].filter(Boolean).join("\n");
      return { data: d, classes: n.classes || "", position: n.position ? { ...n.position } : undefined };
    });
    const edges = data.edges.map((e) => {
      const cls = [];
      if (e.data.dash && e.data.dash !== "solid") cls.push(`dash-${e.data.dash}`);
      if (e.data.reverse) cls.push("reverse");
      if (e.data.direction === "downward") cls.push("down");
      return { data: { ...e.data }, classes: cls.join(" ") };
    });
    return { nodes, edges };
  }

  function leaves(cy) {
    return cy.nodes().filter((n) => !n.isParent());
  }

  /* One canvas with its toolbar inside `host`. Options:
       key        layout key (graph, architecture, flow-3@stacks/api, …)
       layoutUrl  api/layout (page-relative)
       title, fileName   for the exports
       positions  the diagram's own layout {id: {x, y}} (Reset layout goes back to it)
       kinds      [{kind, colour, count}] for the legend
       kindFilter {isOn(kind), set(kind, on)}: the page owns edge filtering (the Graph)
       onTap(ele), onBackground(), tooltip(ele) -> [title, sub…]
       onReset()  puts the diagram's own layout back (default: `positions`)
       manualFit  the page fits the view itself (the Graph, fitted when first shown)
       style()    the Cytoscape style (default: canvasStyle)
       rankdir    dagre direction for Tidy ("LR" | "TB") */
  function create(host, opts) {
    const o = { layoutUrl: "api/layout", rankdir: "LR", ...opts };
    const state = {
      cy: null,
      snap: root.localStorage ? root.localStorage.getItem("tflens.snap") !== "0" : true,
      saved: null,
      off: new Set(o.hiddenKinds || []),
      kinds: o.kinds || [],
      saveTimer: 0,
      fitted: false,
    };
    const action = (name, label, title, onclick, extra) =>
      el("button", { type: "button", class: "tfd-btn", "data-action": name, title, onclick, ...(extra || {}) }, label);
    const zoomLabel = el("span", { class: "tfd-zoom", "data-zoom-label": "", title: "Current zoom" }, "100%");
    const note = el("span", { class: "tfd-note", "aria-live": "polite" });
    const snapBox = el("input", { type: "checkbox", "data-action": "snap", checked: state.snap,
      onchange: () => {
        state.snap = snapBox.checked;
        if (root.localStorage) root.localStorage.setItem("tflens.snap", state.snap ? "1" : "0");
        drawGrid();
      } });
    const fullBtn = action("fullscreen", "⛶ Full screen", "Full screen (Esc leaves)", () => toggleFullscreen());
    const legendBtn = action("legend", "Legend", "Show / hide the legend (edge types)", () => {
      legend.hidden = !legend.hidden;
      legendBtn.setAttribute("aria-pressed", String(!legend.hidden));
    }, { "aria-pressed": "true" });
    const toolbar = el("div", { class: "tfd-toolbar", role: "toolbar", "aria-label": `${o.title || "Diagram"} tools` },
      el("span", { class: "tfd-group" },
        action("fit", "Fit", "Fit the whole diagram in view", () => fit(true)),
        action("zoom-out", "−", "Zoom out", () => zoomBy(1 / ZOOM_STEP)),
        zoomLabel,
        action("zoom-in", "+", "Zoom in", () => zoomBy(ZOOM_STEP)),
        action("zoom-reset", "100%", "Reset the zoom to 100%", () => zoomTo(1)),
        fullBtn),
      el("span", { class: "tfd-group" },
        el("label", { class: "tfd-check", title: `Snap moved nodes to a ${GRID} px grid` }, snapBox, " Snap"),
        action("tidy", "Tidy", "Lay the shown nodes out automatically (dagre)", () => tidy()),
        action("reset-layout", "Reset layout", "Forget the moved positions of this view and run", () => resetLayout())),
      el("span", { class: "tfd-group" }, legendBtn),
      el("span", { class: "tfd-group tfd-export" },
        el("span", { class: "tfd-label" }, "Export"),
        action("export-png", "PNG", "Export as a PNG image", () => exportPng()),
        action("export-svg", "SVG", "Export as an SVG drawing", () => exportSvg()),
        action("export-drawio", ".drawio", "Export as an editable draw.io (diagrams.net) file", () => exportDrawio())),
      note);
    const canvas = el("div", { class: "tfd-canvas", tabindex: "0", "aria-label": o.title || "Diagram" });
    const legend = el("div", { class: "tfd-legend" });
    const tip = el("div", { class: "tfd-tip", hidden: true, role: "tooltip" });
    const stage = el("div", { class: "tfd-stage" }, canvas, legend, tip);
    const wrap = el("div", { class: "tfd", "data-diagram-key": o.key || "" }, toolbar, stage);
    host.replaceChildren(wrap);

    function say(text) {
      note.textContent = text;
    }

    // -- zoom, fit, full screen
    function updateZoom() {
      if (state.cy) zoomLabel.textContent = `${Math.round(state.cy.zoom() * 100)}%`;
    }
    function zoomTo(level) {
      const cy = state.cy;
      if (!cy) return;
      cy.zoom({ level, renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } });
    }
    function zoomBy(f) {
      if (state.cy) zoomTo(state.cy.zoom() * f);
    }
    // whole: everything shown; else a readable size from the top-left corner when the
    // whole diagram would be too small to read
    function fit(whole) {
      const cy = state.cy;
      if (!cy) return;
      const eles = cy.elements(":visible");
      if (!eles.length) return;
      cy.fit(eles, 30);
      if (!whole && cy.zoom() < MIN_READABLE_ZOOM) {
        const bb = eles.boundingBox();
        cy.zoom(MIN_READABLE_ZOOM);
        cy.pan({ x: 30 - bb.x1 * MIN_READABLE_ZOOM, y: 30 - bb.y1 * MIN_READABLE_ZOOM });
      }
      if (cy.zoom() > 1.5) zoomTo(1.5); // a small diagram is not blown up
    }
    function toggleFullscreen() {
      if (doc.fullscreenElement === wrap) doc.exitFullscreen();
      else if (wrap.requestFullscreen) wrap.requestFullscreen().catch((err) => say(`Full screen not available (${err.message}).`));
    }
    doc.addEventListener("fullscreenchange", () => {
      const on = doc.fullscreenElement === wrap;
      wrap.classList.toggle("tfd-full", on);
      fullBtn.textContent = on ? "⛶ Exit full screen" : "⛶ Full screen";
      if (state.cy) setTimeout(() => { state.cy.resize(); drawGrid(); }, 50);
    });

    // -- grid
    function drawGrid() {
      const cy = state.cy;
      const step = GRID * (cy ? cy.zoom() : 1);
      const show = state.snap && step >= 6;
      canvas.classList.toggle("tfd-grid", show);
      if (!show || !cy) return;
      const p = cy.pan();
      canvas.style.backgroundSize = `${step}px ${step}px`;
      canvas.style.backgroundPosition = `${p.x}px ${p.y}px`;
    }

    // -- layout: own positions, saved positions, save, reset, tidy
    const viewUrl = (path) => `${path}?view=${encodeURIComponent(o.key)}`;
    function positions() {
      return { ...(o.positions || {}), ...(state.saved || {}) };
    }
    function applyPositions(pos) {
      const cy = state.cy;
      if (!cy) return 0;
      let moved = 0;
      cy.batch(() => leaves(cy).forEach((n) => {
        const p = pos[n.id()];
        if (p) { n.position({ x: p.x, y: p.y }); moved++; }
      }));
      return moved;
    }
    function loadSaved() {
      if (!o.key) return Promise.resolve(null);
      return fetch(viewUrl(o.layoutUrl), { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          state.saved = d && d.positions ? d.positions : null;
          if (state.saved && applyPositions(state.saved)) {
            say("Saved layout restored.");
            if (state.fitted && !o.manualFit) fit(false); // fitted before it arrived
          }
          return state.saved;
        })
        .catch(() => null);
    }
    function scheduleSave() {
      if (!o.key || !state.cy) return;
      clearTimeout(state.saveTimer);
      state.saveTimer = setTimeout(() => {
        const out = {};
        leaves(state.cy).forEach((n) => {
          const p = n.position();
          out[n.id()] = { x: Math.round(p.x * 10) / 10, y: Math.round(p.y * 10) / 10 };
        });
        state.saved = out;
        sendJson(viewUrl(o.layoutUrl), { positions: out })
          .then(() => say("Layout saved for this run."))
          .catch((err) => say(`Could not save the layout (${err.message}).`));
      }, SAVE_DELAY_MS);
    }
    function resetLayout() {
      const done = () => {
        state.saved = null;
        if (o.onReset) o.onReset();
        else applyPositions(o.positions || {});
        fit(false);
      };
      if (!o.key) { done(); return; }
      sendJson(viewUrl(`${o.layoutUrl}/reset`))
        .then(() => { done(); say("Layout reset."); })
        .catch((err) => say(`Could not reset the layout (${err.message}).`));
    }
    function tidy() {
      const cy = state.cy;
      if (!cy || typeof root.dagre === "undefined") {
        say("Automatic layout not available (dagre.min.js missing from static/vendor/).");
        return;
      }
      try {
        const g = new root.dagre.graphlib.Graph({ compound: true, multigraph: true });
        g.setGraph({ rankdir: o.rankdir, nodesep: 36, ranksep: 110, edgesep: 16, marginx: 20, marginy: 20 });
        g.setDefaultEdgeLabel(() => ({}));
        const shown = cy.nodes(":visible");
        shown.forEach((n) => {
          if (n.isParent()) { g.setNode(n.id(), {}); return; }
          const d = n.layoutDimensions({ nodeDimensionsIncludeLabels: true });
          g.setNode(n.id(), { width: d.w, height: d.h });
        });
        shown.forEach((n) => {
          const p = n.parent();
          if (p.length && p.visible()) g.setParent(n.id(), p.id());
        });
        cy.edges(":visible").forEach((e) => {
          if (!e.source().isParent() && !e.target().isParent()) g.setEdge(e.source().id(), e.target().id(), {}, e.id());
        });
        root.dagre.layout(g);
        cy.batch(() => shown.filter((n) => !n.isParent()).forEach((n) => {
          const p = g.node(n.id());
          if (p) n.position({ x: state.snap ? snap(p.x) : p.x, y: state.snap ? snap(p.y) : p.y });
        }));
        fit(false);
        scheduleSave();
      } catch (err) {
        say(`Automatic layout failed (${err.message}).`);
        if (root.console) root.console.error(err);
      }
    }

    // -- legend: show / hide each edge type
    const kindOn = (k) => (o.kindFilter ? o.kindFilter.isOn(k) : !state.off.has(k));
    function applyKinds() {
      const cy = state.cy;
      if (!cy || o.kindFilter) return;
      cy.batch(() => cy.edges().forEach((e) => e.toggleClass("kind-off", state.off.has(e.data("kind")))));
    }
    function renderLegend() {
      if (!state.kinds.length) {
        legend.hidden = true;
        legendBtn.hidden = true;
        return;
      }
      legendBtn.hidden = false;
      legend.replaceChildren(el("div", { class: "tfd-legend-head" }, "Edge types"),
        ...state.kinds.map((k) => el("label", { class: "tfd-legend-item", "data-kind": k.kind },
          el("input", { type: "checkbox", checked: kindOn(k.kind), onchange: (e) => {
            if (o.kindFilter) o.kindFilter.set(k.kind, e.target.checked);
            else {
              if (e.target.checked) state.off.delete(k.kind); else state.off.add(k.kind);
              applyKinds();
            }
          } }),
          el("i", { class: `tfd-swatch${k.kind === "CONFIG" || k.kind === "CONTAINS" ? " dashed" : ""}`, style: `border-top-color:${k.colour}` }),
          KIND_LABELS[k.kind] || k.kind,
          k.count !== undefined ? el("span", { class: "tfd-count" }, ` ${k.count}`) : null)));
    }

    // -- tooltip
    function showTip(evt) {
      const t = evt.target;
      const lines = (o.tooltip ? o.tooltip(t) : null) ||
        [t.data("label") || t.id(), t.isEdge() ? `${t.data("kind")} · click for its evidence` : "click for its code, evidence and unknowns"];
      tip.replaceChildren(el("strong", {}, lines[0] || ""), ...lines.slice(1).filter(Boolean).map((l) => el("div", { class: "tfd-tip-sub" }, l)));
      const pos = evt.renderedPosition || (t.isEdge() ? t.renderedMidpoint() : t.renderedPosition());
      tip.style.left = `${Math.min(pos.x + 14, Math.max(canvas.clientWidth - 300, 0))}px`;
      tip.style.top = `${pos.y + 14}px`;
      tip.hidden = false;
    }
    function hideTip() {
      tip.hidden = true;
    }

    // -- exports
    function frameColour(n) {
      return n.style("border-color") || "#6e7781";
    }
    function currentView() {
      const cy = state.cy;
      const shown = cy.nodes(":visible");
      const body = (n) => n.boundingBox({ includeLabels: false, includeOverlays: false });
      const groups = shown.filter((n) => n.isParent())
        .sort((a, b) => a.ancestors().length - b.ancestors().length)
        .map((n) => {
          const bb = body(n);
          const p = n.parent();
          return { id: n.id(), parent: p.length && p.visible() ? p.id() : null, x: bb.x1, y: bb.y1, w: bb.w, h: bb.h,
            label: n.data("label") || "", color: frameColour(n), dashed: n.style("border-style") === "dashed" };
        });
      const nodes = shown.filter((n) => !n.isParent()).map((n) => {
        const bb = body(n);
        const p = n.parent();
        const box = n.hasClass("box-node");
        return { id: n.id(), parent: p.length && p.visible() ? p.id() : null, x: bb.x1, y: bb.y1, w: bb.w, h: bb.h,
          shape: box ? "box" : n.data("icon") ? "icon" : "dot",
          label: n.data("label") === undefined ? n.data("display") || n.id() : (n.data("display") || n.data("label")),
          sub: box || n.hasClass("icon-node") ? n.data("sub") || "" : "",
          color: n.data("color"), stroke: n.data("stroke") || n.data("color"),
          icon: n.data("icon") ? iconUrl(n.data("icon")) : null };
      });
      const edges = cy.edges(":visible").map((e) => {
        const s = e.sourceEndpoint(), t = e.targetEndpoint();
        const bends = e.segmentPoints() || [];
        const dash = e.hasClass("dash-dotted") ? "dotted"
          : e.hasClass("dash-dashed") || e.style("line-style") === "dashed" ? "dashed"
            : e.style("line-style") === "dotted" ? "dotted" : "solid";
        return { id: e.id(), source: e.source().id(), target: e.target().id(),
          points: [[s.x, s.y], ...bends.map((p) => [p.x, p.y]), [t.x, t.y]],
          color: e.data("color"), dash, label: e.data("short_label") || e.data("label") || "",
          reverse: e.hasClass("reverse") };
      });
      return { title: o.title || "TFLens diagram", groups, nodes, edges };
    }
    const fileName = (ext) => `${(o.fileName || o.key || "diagram").replace(/[^\w.-]+/g, "_")}.${ext}`;
    function exportPng() {
      const cy = state.cy;
      if (!cy) return;
      const blob = cy.png({ output: "blob", full: true, scale: 2, bg: themeVar("--bg-panel", "#ffffff"), maxWidth: 12000, maxHeight: 12000 });
      download(fileName("png"), blob);
    }
    function exportSvg() {
      if (!state.cy) return;
      download(fileName("svg"), new Blob([toSvg(currentView())], { type: "image/svg+xml" }));
    }
    function exportDrawio() {
      if (!state.cy) return;
      download(fileName("drawio"), new Blob([toDrawio(currentView())], { type: "application/vnd.jgraph.mxfile" }));
    }

    // -- keyboard: arrows nudge the selection, Esc clears it
    canvas.addEventListener("keydown", (e) => {
      const cy = state.cy;
      if (!cy) return;
      const step = state.snap ? GRID : (e.shiftKey ? 10 : 1);
      const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (d) {
        const sel = cy.nodes(":selected");
        if (!sel.length) return;
        e.preventDefault();
        const moving = sel.union(sel.descendants()).filter((n) => !n.isParent());
        cy.batch(() => moving.forEach((n) => {
          const p = n.position();
          n.position({ x: p.x + d[0], y: p.y + d[1] });
        }));
        scheduleSave();
      } else if (e.key === "Escape") {
        cy.elements().unselect();
        if (o.onBackground) o.onBackground();
      }
    });

    // -- attach a Cytoscape instance (made by the page, or by draw())
    function attach(cy) {
      state.cy = cy;
      cy.boxSelectionEnabled(true); // Shift + drag on the background: a selection box
      cy.on("zoom", updateZoom);
      cy.on("viewport", () => { hideTip(); drawGrid(); });
      cy.on("tap", (evt) => {
        const t = evt.target;
        const oe = evt.originalEvent || {};
        canvas.focus({ preventScroll: true });
        if (t === cy) { if (o.onBackground) o.onBackground(); return; }
        if (oe.shiftKey || oe.ctrlKey || oe.metaKey) return; // adding to the selection only
        if (o.onTap) o.onTap(t, evt);
      });
      cy.on("mouseover", "node, edge", (evt) => {
        if (evt.target.isEdge()) evt.target.addClass("hover");
        canvas.style.cursor = "pointer";
        showTip(evt);
      });
      cy.on("mousemove", "node, edge", showTip);
      cy.on("mouseout", "node, edge", (evt) => {
        evt.target.removeClass("hover");
        canvas.style.cursor = "";
        hideTip();
      });
      cy.on("grab", "node", hideTip);
      // released after a drag: snap what moved to the grid, then save
      cy.on("dragfree", "node", (evt) => {
        if (state.snap) {
          const moved = evt.target.union(cy.nodes(":selected")).union(evt.target.descendants());
          cy.batch(() => moved.filter((n) => !n.isParent()).forEach((n) => {
            const p = n.position();
            n.position({ x: snap(p.x), y: snap(p.y) });
          }));
        }
        scheduleSave();
      });
      applyKinds();
      renderLegend();
      updateZoom();
      drawGrid();
      whenVisible(canvas, () => {
        cy.resize();
        if (!state.fitted && !o.manualFit) {
          state.fitted = true;
          fit(false);
        }
        drawGrid();
      });
      loadSaved();
      return cy;
    }

    // a canvas payload (api/canvas), drawn with the shared style
    function draw(data) {
      if (typeof root.cytoscape !== "function") throw new Error("cytoscape.min.js missing from static/vendor/");
      const els = canvasElements(data);
      o.positions = {};
      for (const n of els.nodes) if (n.position) o.positions[n.data.id] = { ...n.position };
      state.kinds = data.kinds || [];
      const cy = root.cytoscape({
        container: canvas,
        elements: els,
        style: (o.style || canvasStyle)(),
        layout: { name: "preset", fit: false },
        wheelSensitivity: 0.3, minZoom: 0.05, maxZoom: 4,
        textureOnViewport: els.nodes.length > 250,
      });
      return attach(cy);
    }

    const api = {
      el: wrap,
      canvas,
      get cy() { return state.cy; },
      attach,
      draw,
      fit,
      positions,
      say,
      setKinds(kinds) { state.kinds = kinds || []; renderLegend(); },
      refreshLegend: renderLegend,
      view: () => currentView(),
      save: scheduleSave,
      destroy() {
        if (state.cy) state.cy.destroy();
        state.cy = null;
      },
    };
    return api;
  }

  root.TFDiagram = {
    ...pure,
    create,
    canvasStyle,
    canvasElements,
    whenVisible,
    whenNear,
    themeVar,
    setIcons(map) { icons = map || {}; },
    iconUrl,
  };
})(typeof window !== "undefined" ? window : globalThis);
