/* TFLens web UI: graph (Cytoscape, vendored), repository tree, detail panel.
   All data comes from the local /api/* endpoints; no external requests. */
"use strict";

(function () {
  let cy = null;
  let graphData = null;

  // ------------------------------------------------------------ helpers
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "style") node.setAttribute("style", v);
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of children) {
      if (c === null || c === undefined) continue;
      node.append(typeof c === "string" || typeof c === "number" ? String(c) : c);
    }
    return node;
  }

  async function api(path, options) {
    const resp = await fetch(path, options);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || data.error || resp.statusText);
    return data;
  }

  function locText(loc) {
    if (!loc) return "?";
    return loc.start_line === loc.end_line
      ? `${loc.file}:${loc.start_line}`
      : `${loc.file}:${loc.start_line}-${loc.end_line}`;
  }

  function link(text, onclick) {
    return el("a", { class: "link", onclick: (e) => { e.preventDefault(); onclick(); } }, text);
  }

  function kindBadge(kind) {
    const color = (graphData && graphData.colors.kinds[kind]) || "#57606a";
    return el("span", { class: "kind", style: `background:${color}` }, kind);
  }

  function snippet(lines) {
    const pre = el("pre", { class: "snippet" });
    for (const ln of lines) {
      pre.append(
        el("span", { class: ln.highlight ? "line hl" : "line" },
          el("span", { class: "ln" }, ln.line === null ? "" : ln.line),
          ln.text)
      );
    }
    return pre;
  }

  function evidenceBlock(title, lines, note) {
    return el("div", { class: "evidence" },
      el("div", { class: "loc" }, title, note ? ` — ${note}` : ""),
      snippet(lines));
  }

  // ------------------------------------------------------------ views
  function setView(view) {
    for (const t of document.querySelectorAll(".tab")) {
      t.classList.toggle("active", t.dataset.view === view);
    }
    for (const n of document.querySelectorAll(".view-graph")) n.classList.toggle("hidden", view !== "graph");
    for (const n of document.querySelectorAll(".view-repo")) n.classList.toggle("hidden", view !== "repo");
    if (view === "graph" && cy) cy.resize();
  }

  // ------------------------------------------------------------ detail panel
  async function showDetail(type, id) {
    const panel = document.getElementById("detail");
    let d;
    try {
      d = await api(`/api/${type}?id=${encodeURIComponent(id)}`);
    } catch (err) {
      panel.replaceChildren(el("p", { class: "muted" }, `${type} ${id}: ${err.message}`));
      return;
    }
    if (cy) {
      cy.elements().unselect();
      const ele = cy.getElementById(id);
      if (ele && ele.length) ele.select();
    }
    const parts = [el("h2", {}, d.title)];
    const s = d.summary;
    if (type === "node") {
      parts.push(el("dl", {},
        el("dt", {}, "What"), el("dd", {}, s.description),
        el("dt", {}, "Type"), el("dd", {}, s.type),
        el("dt", {}, "Service"), el("dd", {}, s.service || "—"),
        el("dt", {}, "Module"), el("dd", {}, s.module),
        el("dt", {}, "Edges"), el("dd", {}, `${s.outgoing} out, ${s.incoming} in`)));
      parts.push(el("h3", {}, "Defined by"));
      if (d.defined_by) {
        parts.push(evidenceBlock(`${locText(d.defined_by)} (module ${d.defined_by.module})`, d.defined_by.lines));
      } else {
        parts.push(el("p", { class: "muted" }, "Not defined in this repository (external)."));
      }
      for (const [label, list] of [["Outgoing", d.edges.outgoing], ["Incoming", d.edges.incoming]]) {
        if (!list.length) continue;
        parts.push(el("h3", {}, label));
        parts.push(el("ul", { class: "links" }, ...list.map((e) =>
          el("li", {}, kindBadge(e.kind), " ", link(e.other, () => showDetail("node", e.other)),
            " — ", link(e.label, () => showDetail("edge", e.id))))));
      }
    } else {
      parts.push(el("dl", {},
        el("dt", {}, "Kind"), el("dd", {}, kindBadge(s.kind)),
        el("dt", {}, "Label"), el("dd", {}, s.label),
        el("dt", {}, "From"), el("dd", {}, link(s.source, () => showDetail("node", s.source))),
        el("dt", {}, "To"), el("dd", {}, link(s.target, () => showDetail("node", s.target))),
        s.via.length ? el("dt", {}, "Via") : null,
        s.via.length ? el("dd", {}, ...s.via.map((v, i) =>
          el("span", {}, i ? ", " : "", link(v, () => showDetail("node", v))))) : null));
      const attrs = Object.entries(s.attributes);
      if (attrs.length) {
        parts.push(el("h3", {}, "Attributes"));
        parts.push(el("dl", {}, ...attrs.flatMap(([k, v]) =>
          [el("dt", {}, k), el("dd", {}, typeof v === "string" ? v : JSON.stringify(v))])));
      }
      parts.push(el("h3", {}, "Defined by"));
      for (const def of d.defined_by) {
        parts.push(evidenceBlock(`${def.node} — ${locText(def)} (module ${def.module})`, def.lines));
      }
    }
    parts.push(el("h3", {}, `Evidence (${d.evidence.length})`));
    if (!d.evidence.length) parts.push(el("p", { class: "muted" }, "No evidence recorded."));
    for (const ev of d.evidence) {
      const head = type === "node" ? `${ev.kind} ${ev.label} — ${locText(ev)}` : locText(ev);
      parts.push(evidenceBlock(head, ev.lines, ev.note));
    }
    parts.push(el("h3", {}, `Unknowns (${d.unknowns.length})`));
    if (!d.unknowns.length) parts.push(el("p", { class: "muted" }, "None."));
    for (const u of d.unknowns) {
      parts.push(el("div", { class: "unknown" },
        el("strong", {}, `${u.subject} ${u.attribute}`), ": ", u.reason,
        el("div", { class: "tag" }, locText(u.location))));
    }
    panel.replaceChildren(...parts);
    panel.scrollTop = 0;
  }

  // ------------------------------------------------------------ graph
  function checkboxList(container, values, colorOf, line) {
    container.replaceChildren(...values.map((v) =>
      el("label", {},
        el("input", { type: "checkbox", value: v, checked: "", onchange: applyFilters }),
        colorOf ? el("span", { class: line ? "swatch line" : "swatch", style: `background:${colorOf(v)}` }) : null,
        v)));
  }

  function checked(id) {
    return new Set([...document.querySelectorAll(`#${id} input:checked`)].map((i) => i.value));
  }

  function applyFilters() {
    if (!cy) return;
    const kinds = checked("filter-kinds");
    const services = checked("filter-services");
    const modules = checked("filter-modules");
    const hideIsolated = document.getElementById("hide-isolated").checked;
    const q = document.getElementById("search").value.trim().toLowerCase();
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const ok = services.has(n.data("service")) && modules.has(n.data("module"));
        n.style("display", ok ? "element" : "none");
      });
      cy.edges().forEach((e) => {
        const ok = kinds.has(e.data("kind")) &&
          e.source().style("display") !== "none" && e.target().style("display") !== "none";
        e.style("display", ok ? "element" : "none");
      });
      if (hideIsolated) {
        cy.nodes().forEach((n) => {
          if (n.style("display") === "none") return;
          const visible = n.connectedEdges().filter((e) => e.style("display") !== "none");
          if (!visible.length) n.style("display", "none");
        });
      }
      cy.elements().removeClass("faded match");
      if (q) {
        const hits = cy.nodes().filter((n) =>
          n.style("display") !== "none" &&
          (n.id().toLowerCase().includes(q) || String(n.data("name")).toLowerCase().includes(q)));
        cy.elements().addClass("faded");
        hits.removeClass("faded").addClass("match");
        hits.connectedEdges().removeClass("faded");
      }
    });
  }

  function buildGraph(data) {
    graphData = data;
    checkboxList(document.getElementById("filter-kinds"), data.filters.kinds, (k) => data.colors.kinds[k], true);
    checkboxList(document.getElementById("filter-services"), data.filters.services, (s) => data.colors.services[s]);
    checkboxList(document.getElementById("filter-modules"), data.filters.modules, null);
    const container = document.getElementById("graph");
    if (typeof window.cytoscape !== "function") {
      container.replaceChildren(el("div", { class: "banner" },
        "Graph library not found: copy cytoscape.min.js into src/tflens/static/vendor/. ",
        "The repository view and detail panel still work."));
      return;
    }
    if (cy) cy.destroy();
    cy = window.cytoscape({
      container,
      elements: { nodes: data.nodes, edges: data.edges },
      wheelSensitivity: 0.3,
      style: [
        { selector: "node", style: {
          "background-color": "data(color)", label: "data(label)", "font-size": 9,
          "text-valign": "bottom", "text-margin-y": 3, width: 18, height: 18,
          "text-wrap": "ellipsis", "text-max-width": 160 } },
        { selector: 'node[kind = "external"]', style: { shape: "diamond" } },
        { selector: 'node[kind = "data"]', style: { shape: "round-rectangle" } },
        { selector: 'node[category = "relationship"], node[category = "policy"]', style: { width: 12, height: 12 } },
        { selector: "edge", style: {
          "line-color": "data(color)", "target-arrow-color": "data(color)",
          "target-arrow-shape": "triangle", "curve-style": "bezier", width: 1.5, "arrow-scale": 0.8 } },
        { selector: 'edge[kind = "CONFIG"]', style: { "line-style": "dashed", width: 1 } },
        { selector: 'edge[kind = "RUNS_ON"]', style: { "line-style": "dotted" } },
        { selector: ":selected", style: { "border-width": 3, "border-color": "#0969da", "line-color": "#0969da", "target-arrow-color": "#0969da", width: 3 } },
        { selector: ".faded", style: { opacity: 0.15 } },
        { selector: ".match", style: { "border-width": 3, "border-color": "#bf8700" } },
      ],
      layout: { name: "grid" },
    });
    cy.on("tap", "node", (evt) => showDetail("node", evt.target.id()));
    cy.on("tap", "edge", (evt) => showDetail("edge", evt.target.id()));
    applyFilters();
    cy.layout({ name: "cose", animate: false, randomize: false, nodeDimensionsIncludeLabels: true,
      eles: cy.elements(":visible") }).run();
    cy.fit(cy.elements(":visible"), 30);
  }

  // ------------------------------------------------------------ repository
  function treeNode(node) {
    if (node.type === "file") {
      return el("a", { class: "file link", "data-path": node.path, onclick: () => showFile(node.path) },
        node.name, " ", el("span", { class: "tag" }, `(${node.resources})`));
    }
    const label = [node.name + "/"];
    if (node.modules.length) label.push(" ", el("span", { class: "tag" }, node.modules.join(", ")));
    return el("details", { class: "dir", open: "" },
      el("summary", {}, ...label),
      el("div", {}, ...node.children.map(treeNode)));
  }

  async function showFile(path) {
    for (const a of document.querySelectorAll("#tree .file")) {
      a.classList.toggle("active", a.dataset.path === path);
    }
    const box = document.getElementById("file");
    const d = await api(`/api/file?path=${encodeURIComponent(path)}`);
    const parts = [el("h2", {}, d.path)];
    if (d.modules.length) parts.push(el("p", { class: "muted" }, `Module instances: ${d.modules.join(", ")}`));
    if (!d.resources.length) {
      parts.push(el("p", { class: "muted" }, "No resources, data sources or module calls defined here."));
    } else {
      parts.push(el("table", {},
        el("tr", {}, el("th", {}, "Lines"), el("th", {}, "Kind"), el("th", {}, "Type"), el("th", {}, "Id")),
        ...d.resources.map((r) => el("tr", {},
          el("td", {}, `${r.location.start_line}-${r.location.end_line}`),
          el("td", {}, r.kind), el("td", {}, r.type),
          el("td", {}, link(r.id, () => showDetail("node", r.id)))))));
    }
    if (d.unknowns.length) {
      parts.push(el("h3", {}, `Unknowns in this file (${d.unknowns.length})`));
      for (const u of d.unknowns) {
        parts.push(el("div", { class: "unknown" },
          el("strong", {}, `${u.subject} ${u.attribute}`), ": ", u.reason,
          el("div", { class: "tag" }, locText(u.location))));
      }
    }
    box.replaceChildren(...parts);
  }

  // ------------------------------------------------------------ boot
  async function loadAll() {
    const [summary, graph, tree] = await Promise.all([api("/api/summary"), api("/api/graph"), api("/api/tree")]);
    document.getElementById("title").textContent = summary.title;
    const s = summary.summary;
    const nodes = Object.values(s.nodes).reduce((a, b) => a + b, 0);
    const edges = Object.entries(s.edges).map(([k, v]) => `${k} ${v}`).join(", ") || "none";
    document.getElementById("summary").textContent =
      `${s.files} files · ${s.modules} modules · ${nodes} nodes · edges: ${edges} · ` +
      `${s.unknowns} unknowns · ${s.parse_errors} parse errors`;
    document.getElementById("tree").replaceChildren(treeNode(tree));
    buildGraph(graph);
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => setView(t.dataset.view));
    document.getElementById("search").addEventListener("input", applyFilters);
    document.getElementById("hide-isolated").addEventListener("change", applyFilters);
    const regen = document.getElementById("regenerate");
    regen.addEventListener("click", async () => {
      regen.disabled = true;
      try {
        await api("/api/regenerate", { method: "POST", headers: { "X-TFLens": "1" } });
        await loadAll();
      } finally {
        regen.disabled = false;
      }
    });
    loadAll().catch((err) => {
      document.getElementById("detail").replaceChildren(el("p", {}, `Failed to load: ${err.message}`));
    });
  });
})();
