/* TFLens web UI: root view (the landing view: each root in the view its shape calls
   for), flow, overview, graph (Cytoscape, vendored),
   diagrams, repository tree, detail panel.
   All data comes from the local api/* endpoints next to this page (/api/* for
   `tflens serve --dir`, /runs/<id>/api/* for a recorded run); no external requests. */
"use strict";

(function () {
  let cy = null;
  let graphData = null;
  let graphCanvas = null; // the Graph's editable canvas (diagram.js), toolbar and legend
  let iconsData = null; // api/icons: service icons and colours shared by every view

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

  // A service's icon and colour, the same as on the Graph and the diagrams (api/icons):
  // `style` is {service, colour, icon}, e.g. iconsData.types[type].
  function svcIcon(style) {
    if (!style || !iconsData) return null;
    return el("img", { class: "svc-icon", src: iconsData.icons[style.icon] || iconsData.icons.generic,
      alt: "", title: style.service, style: `border-color:${style.colour}` });
  }

  function typeStyle(type) {
    return iconsData ? iconsData.types[type] || null : null;
  }

  // A table cell's service label: icon, colour bar, text.
  function svcLabel(style, ...text) {
    if (!style) return el("span", {}, ...text);
    return el("span", { class: "svc", style: `--svc:${style.colour}` }, svcIcon(style), ...text);
  }

  function evidenceBlock(title, lines, note) {
    return el("div", { class: "evidence" },
      el("div", { class: "loc" }, title, note ? ` — ${note}` : ""),
      snippet(lines));
  }

  // ------------------------------------------------------------ views
  const VIEWS = ["environments", "roots", "flow", "overview", "graph", "repo", "diagrams"];
  let graphFitted = false; // the graph is laid out while hidden: fit it when first shown

  function setView(view) {
    for (const t of document.querySelectorAll(".tab")) {
      t.classList.toggle("active", t.dataset.view === view);
    }
    for (const v of VIEWS) {
      for (const n of document.querySelectorAll(`.view-${v}`)) n.classList.toggle("hidden", view !== v);
    }
    if (view === "graph" && cy) {
      cy.resize();
      if (!graphFitted) {
        graphFitted = true;
        layoutGraph();
      }
    }
    const note = document.getElementById("graph-note");
    if (note) note.classList.toggle("hidden", view !== "graph" || !note.textContent);
  }

  // ------------------------------------------------------------ file:line references
  // Evidence is real files only: a doc is never something the UI points at.
  const DOC_FILE = /\.(md|markdown)$/i;

  function locLink(loc) {
    if (!loc || DOC_FILE.test(loc.file)) return null;
    if (!loc.start_line) return el("span", { class: "tag" }, `${loc.file}/`);
    return link(locText(loc), () => showSource(loc));
  }

  async function showSource(loc) {
    const panel = document.getElementById("detail");
    let d;
    try {
      d = await api(`api/source?file=${encodeURIComponent(loc.file)}&start=${loc.start_line}&end=${loc.end_line}`);
    } catch (err) {
      panel.replaceChildren(el("p", { class: "muted" }, `${locText(loc)}: ${err.message}`));
      return;
    }
    panel.replaceChildren(el("h2", {}, locText(d)), evidenceBlock(locText(d), d.lines));
    panel.scrollTop = 0;
  }

  // ------------------------------------------------------------ overview
  function overviewLine(ln) {
    // a line naming a view (Start here's "Environments ...") opens it
    const parts = [ln.view ? link(ln.text, () => setView(ln.view)) : ln.text];
    if (ln.location) parts.push(" ", locLink(ln.location));
    if (ln.node) parts.push(" ", link("details", () => showDetail("node", ln.node)));
    if (ln.edge) parts.push(" ", link("evidence", () => showDetail("edge", ln.edge)));
    const cls = ["ov-line", `lvl-${Math.min(ln.level || 0, 3)}`];
    if (ln.status) cls.push(`st-${ln.status}`);
    return el("li", { class: cls.join(" ") }, ...parts);
  }

  function overviewList(lines) {
    return el("ul", { class: "overview-lines" }, ...lines.map(overviewLine));
  }

  function drilldown(d) {
    const parts = [el("summary", {}, d.title, " ", locLink(d.location), " ",
      link("details", () => showDetail("node", d.node)))];
    parts.push(overviewList(d.lines));
    if (d.snippet && d.snippet.length) parts.push(snippet(d.snippet));
    return el("details", { class: "drilldown" }, ...parts);
  }

  // ------------------------------------------------------------ environments
  // One row per environment name (dev | intg | uat | prod: every stack with its own
  // tfvars), never a total across environments; the files of each stack's environment.
  function envCountsTable(envs, columns) {
    const types = envs.types || [];
    const head = el("tr", {}, el("th", {}, "Environment"),
      ...types.map((t) => el("th", {}, t.label)), el("th", {}, "Unknowns"), el("th", {}, "Files"));
    const byId = new Map(envs.environments.map((r) => [r.id, r]));
    const rows = (columns || envColumns(envs)).map((c) => {
      const files = (c.environments || [c.id]).flatMap((id) => {
        const r = byId.get(id);
        return r ? [...r.backend_hcl, ...r.var_files] : [];
      });
      const active = (c.environments || []).some((id) => isSelected(envs, id));
      return el("tr", { class: active ? "env-row active" : "env-row", "data-env": c.id },
        el("td", {}, c.id),
        ...types.map((t) => el("td", { class: "num" }, String(c.counts[t.type] || 0))),
        el("td", { class: c.unknown_count ? "num st-unknown" : "num" }, unknownCount(c, `Unknowns in ${c.id}`)),
        el("td", {}, ...files.flatMap((f, i) => [i ? ", " : "",
          locLink({ file: f, start_line: 1, end_line: 1 })])));
    });
    return el("table", { class: "env-counts" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  // The inventory's columns: one per environment name (older models: one per row).
  function envColumns(envs) {
    return envs.columns && envs.columns.length ? envs.columns : envs.environments;
  }

  // What is still unknown, grouped by why: each value with its reason and real file:line.
  function groupUnknowns(list) {
    const groups = new Map();
    for (const u of list || []) {
      const key = u.category || "other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(u);
    }
    return [...groups.entries()].map(([category, items]) => ({ category, count: items.length, items }))
      .sort((a, b) => b.count - a.count || a.category.localeCompare(b.category));
  }

  function unknownItem(u) {
    return el("li", { class: "unknown-item" },
      link(`${u.subject}.${u.attribute}`, () => showDetail("node", u.subject)), ` — ${u.reason} `,
      locLink(u.location),
      u.defined_at ? el("span", { class: "tag" }, " · defined at ", locLink(u.defined_at)) : null);
  }

  function unknownsPanel(groups) {
    return el("div", { class: "unknowns-panel" }, ...groups.map((g) => el("details", { class: "unknown-group", open: "" },
      el("summary", {}, g.category, " ", el("span", { class: "tag" }, `(${g.count})`)),
      el("ul", { class: "unknown-items" }, ...g.items.map(unknownItem)))));
  }

  // A count of unknowns in a table: opens that group of values in the detail pane.
  function unknownCount(row, title) {
    if (!row.unknown_count) return "0";
    return link(String(row.unknown_count), () => {
      const panel = document.getElementById("detail");
      panel.replaceChildren(el("h2", {}, `${title} (${row.unknown_count})`),
        unknownsPanel(row.unknown_groups || groupUnknowns(row.unknowns)));
      panel.scrollTop = 0;
    });
  }

  function envUnknowns(envs) {
    const shown = envs.current;
    const name = (envs.selected || []).join(", ") || envs.active;
    const title = name ? `Unknowns in ${name}, after applying its variables` : "Unknowns (no environment applied)";
    if (!shown.unknown_count) return [el("p", { class: "muted" }, `${title}: none.`)];
    const parts = [el("h3", {}, `${title} (${shown.unknown_count})`),
      unknownsPanel(shown.unknown_groups || groupUnknowns(shown.unknowns))];
    if (shown.unknown_count > shown.unknowns.length) {
      parts.push(el("p", { class: "muted" }, `… ${shown.unknown_count - shown.unknowns.length} more`));
    }
    return parts;
  }

  // The verified init / plan / apply of one stack + environment, once each, in a small popover.
  function commandsInfo(r) {
    const commands = r.commands || [];
    if (!commands.length) return null;
    const box = el("div", { class: "cmd-popover", role: "dialog", "aria-label": `Verified commands of ${r.id}` },
      el("div", { class: "cmd-head" }, r.stack ? `${r.stack} · ${r.id}` : r.id),
      ...commands.map((c) => el("div", { class: "cmd-line", "data-step": c.step },
        el("code", {}, c.command),
        el("div", { class: "tag" }, c.source === "ci" ? "" : "from this environment's files: ", locLink(c.location)))));
    box.hidden = true;
    const button = el("button", { type: "button", class: "cmd-info-btn", title: "Verified init / plan / apply commands",
      "aria-label": `Verified commands of ${r.id}`, "aria-expanded": "false" }, "i");
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = box.hidden;
      closeCommandPopovers();
      box.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
    });
    box.addEventListener("click", (e) => e.stopPropagation());
    return el("span", { class: "cmd-info" }, button, box);
  }

  function closeCommandPopovers() {
    for (const p of document.querySelectorAll(".cmd-popover")) p.hidden = true;
    for (const b of document.querySelectorAll(".cmd-info-btn")) b.setAttribute("aria-expanded", "false");
  }

  // ------------------------------------------------------------ environments view
  // The first tab (what Start here opens): the detected environments and their files,
  // the state file of each, the service inventory with one column per environment (the
  // rows that differ highlighted; click a count for the names, file:line and why it is
  // counted so), and the differences between environments. Flows, Network and IAM
  // follow the environment selected here or in the switcher.
  function fileLinks(files) {
    if (!files.length) return [el("span", { class: "muted" }, "—")];
    return files.flatMap((f, i) => [i ? ", " : "", locLink({ file: f, start_line: 1, end_line: 1 })]);
  }

  // The environments applied in the model shown: one per stack.
  function isShown(envs, id) {
    return (envs.selected || []).includes(id) || id === envs.active;
  }

  // The one selected row of the environment tables: the environment picked last, else the
  // one the analysis shows. The other stacks' applied environments are not highlighted.
  let pickedEnv = null;

  function isSelected(envs, id) {
    const picked = envs.environments.some((r) => r.id === pickedEnv) ? pickedEnv : envs.active;
    return id === picked;
  }

  function multiStack(envs) {
    return (envs.stacks || []).filter((s) => s.environments.length).length > 1;
  }

  // One row per stack: its root(s), and its own environment selector.
  function stacksTable(envs) {
    const head = el("tr", {}, el("th", {}, "Stack"), el("th", {}, "Root(s)"), el("th", {}, "Layout"),
      el("th", {}, "Environment shown"));
    const rows = envs.stacks.map((s) => {
      let pick;
      if (s.library) {
        pick = el("span", { class: "muted" }, "— reusable module nothing calls: not deployed from here, not counted");
      } else if (!s.environments.length) {
        pick = el("span", { class: "st-unknown" }, `— ${s.no_env_reason || "no environment files found"}`);
      } else if (!envs.switchable) {
        pick = el("strong", {}, s.selected ? `${s.selected}${s.default ? " (default)" : ""}` : "none (variable defaults)");
      } else {
        pick = el("select", { "data-stack": s.id, onchange: (ev) => selectStackEnv(s.id, ev.target.value) },
          el("option", { value: "" }, "none (variable defaults)"),
          ...s.environments.map((id) => el("option", { value: id },
            `${id}${s.default && id === s.selected ? " (default)" : ""}`)));
        pick.value = s.selected || "";
      }
      const roots = s.analyzed.length ? s.analyzed : s.roots;
      return el("tr", { class: "stack-row", "data-stack": s.id },
        el("td", { class: "mono" }, s.id), el("td", { class: "mono" }, roots.join(", ") || "—"),
        el("td", {}, s.layout), el("td", {}, pick));
    });
    return el("table", { class: "env-stacks" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  function stackLinksList(envs) {
    return el("ul", { class: "overview-lines" }, ...envs.stack_links.map((l) => {
      const target = l.to_stack ? `stack ${l.to_stack}${l.env ? ` (${l.env})` : ""}` : `${l.backend} state outside this repository`;
      return el("li", { class: l.status === "unresolved" ? "ov-line st-unknown" : "ov-line" },
        `${l.from_stack || "?"} reads ${target} via `, link(l.data_source, () => showDetail("node", l.data_source)),
        " ", locLink(l.location), l.to_stack ? null : el("span", { class: "tag" }, ` ${l.reason}`));
    }));
  }

  async function selectStackEnv(stack, name) {
    pickedEnv = name || null;
    for (const s of document.querySelectorAll("select[data-stack]")) s.disabled = true;
    try {
      await api(`api/env?stack=${encodeURIComponent(stack)}&name=${encodeURIComponent(name)}`,
        { method: "POST", headers: { "X-TFLens": "1" } });
      await loadAll();
    } catch (err) {
      document.getElementById("detail").replaceChildren(el("p", {}, `Environment not applied: ${err.message}`));
      for (const s of document.querySelectorAll("select[data-stack]")) s.disabled = false;
    }
  }

  function envFilesTable(envs) {
    const stacked = multiStack(envs);
    const head = el("tr", {}, el("th", {}, "Environment"), stacked ? el("th", {}, "Stack") : null, el("th", {}, "Wired"),
      el("th", {}, "tfvars"), el("th", {}, "Backend config"));
    const rows = envs.environments.map((r) => {
      const wired = r.wired
        ? el("td", { title: r.wiring.join("; ") }, "yes")
        : el("td", { class: "st-unknown", title: r.not_wired_reason || "" }, "not wired");
      return el("tr", { class: `env-row${isSelected(envs, r.id) ? " active" : ""}${r.wired ? "" : " not-wired"}`, "data-env": r.id },
        el("td", {}, envButton(envs, r.id), " ", commandsInfo(r)), stacked ? el("td", { class: "mono" }, r.stack || "—") : null, wired,
        el("td", {}, ...fileLinks(r.var_files)), el("td", {}, ...fileLinks(r.backend_hcl)));
    });
    return el("table", { class: "env-files" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  // An environment's name; a button applying it when the analysis can switch.
  function envButton(envs, id) {
    const shown = isShown(envs, id);
    const label = shown ? `${id} (shown)` : id;
    if (!envs.switchable || shown) return el("strong", {}, label);
    if (multiStack(envs)) {
      const row = envs.environments.find((r) => r.id === id);
      if (row && row.stack) {
        return el("button", { type: "button", class: "env-apply", title: `Show ${row.stack} as ${id}`, onclick: () => selectStackEnv(row.stack, id) }, label);
      }
    }
    return el("button", { type: "button", class: "env-apply", title: `Analyze as ${id}`, onclick: () => selectEnv(id) }, label);
  }

  function stateTable(envs) {
    const keys = [];
    for (const r of envs.environments) {
      for (const s of (r.state && r.state.settings) || []) if (!keys.includes(s.key)) keys.push(s.key);
    }
    const head = el("tr", {}, el("th", {}, "Environment"), el("th", {}, "Backend"),
      ...keys.map((k) => el("th", {}, k)));
    const rows = envs.environments.map((r) => {
      const st = r.state || { type: "?", settings: [] };
      const by = new Map(st.settings.map((s) => [s.key, s]));
      return el("tr", { class: isSelected(envs, r.id) ? "env-row active" : "env-row" },
        el("td", {}, r.id), el("td", { title: st.origin || st.note || "" }, st.type),
        ...keys.map((k) => {
          const s = by.get(k);
          if (!s) return el("td", { class: "muted" }, "—");
          if (!s.known) return el("td", { class: "st-unknown", title: s.reason || "" }, "UNKNOWN");
          return el("td", { class: "mono" }, s.value, " ", locLink(s.location));
        }));
    });
    return el("table", { class: "env-state" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  function whySteps(why) {
    return el("ol", { class: "why-steps" }, ...why.steps.map((s) => el("li", { class: s.instances === null ? "st-unknown" : "" },
      `${s.address}: ${s.meta} = ${s.expression} → ${s.instances === null ? "not known" : s.instances} `,
      locLink(s.location), s.reason ? el("div", { class: "tag" }, s.reason) : null,
      s.sources.length ? el("ul", {}, ...s.sources.map(whySource)) : null)));
  }

  function whySource(s) {
    const what = {
      unset: `${s.ref} is not set (no default, no value in tfvars)`,
      tfvars: `${s.ref} = ${s.value} from tfvars`,
      default: `${s.ref} = ${s.value} (variable default)`,
      "--var": `${s.ref} = ${s.value} (--var)`,
    }[s.origin] || `${s.ref} = ${s.value} (${s.origin}${s.expression ? `: ${s.expression}` : ""})`;
    return el("li", { class: s.known ? "" : "st-unknown" }, what, " ", locLink(s.location),
      s.from && s.from.length ? el("ul", {}, ...s.from.map(whySource)) : null);
  }

  function showInventoryCell(row, column, cell) {
    const panel = document.getElementById("detail");
    const title = column ? `${row.label} in ${column}` : row.label;
    const parts = [el("h2", {}, `${title}: ${cell.count}`)];
    if (cell.uncertain) {
      parts.push(el("p", { class: "muted" },
        `Not counted: ${cell.uncertain} block(s) whose count is not known statically; see why below.`));
    }
    if (!cell.items.length) parts.push(el("p", { class: "muted" }, "None created."));
    for (const it of cell.items) {
      const only = row.only[it.id];
      parts.push(el("div", { class: `inv-item${it.created === null ? " count-open" : ""}` },
        el("strong", {}, it.name), " ", link(it.id, () => showDetail("node", it.id)), " ", locLink(it.location),
        it.stack || it.env ? el("div", { class: "tag" }, `stack ${it.stack || "—"} · env ${it.env || "none (variable defaults)"}`) : null,
        it.via && it.via.length ? el("div", { class: "tag" }, "via ", ...it.via.flatMap((m, i) => [i ? " → " : "", m.id, " ", locLink(m.location)])) : null,
        el("div", { class: "tag" }, it.created === null ? "count not known" : `${it.created} instance${it.created === 1 ? "" : "s"}`,
          only ? ` · only in ${only.join(", ")}` : ""),
        it.why ? el("div", {}, el("div", { class: "tag" }, "Why counted:"), whySteps(it.why)) : null));
    }
    panel.replaceChildren(...parts);
    panel.scrollTop = 0;
  }

  function inventoryGrid(envs) {
    const perEnv = envs.inventory_columns === "environments";
    // one column per environment name (dev | intg | uat | prod), never a total column
    const cols = perEnv ? envColumns(envs).map((c) => [c.id, c.inventory, c.wired !== false]) : [["", envs.current.inventory, true]];
    const head = el("tr", {}, el("th", {}, "Service"),
      ...cols.map(([id, , wired]) => el("th", { class: wired ? "num" : "num st-unknown", title: wired ? "" : "not wired" },
        id || (envs.active ? envs.active : "shown (variable defaults)"))));
    const rows = envs.inventory.map((row) => {
      const values = cols.map(([, inv]) => String(inv[row.key].count));
      const common = mostCommon(values);
      return el("tr", { class: row.differs ? "inv-row differs" : "inv-row", "data-row": row.key },
        el("td", {}, svcLabel(iconsData && iconsData.inventory[row.key], row.label),
          row.differs ? el("span", { class: "tag st-unknown" }, " differs") : null),
        ...cols.map(([id, inv], i) => {
          const cell = inv[row.key];
          const cls = ["num", "inv-cell"];
          if (row.differs && values[i] !== common) cls.push("diff");
          return el("td", { class: cls.join(" ") },
            link(values[i], () => showInventoryCell(row, id, cell)));
        }));
    });
    return el("table", { class: "env-inventory" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  function mostCommon(values) {
    const n = new Map();
    for (const v of values) n.set(v, (n.get(v) || 0) + 1);
    return [...n.entries()].sort((a, b) => b[1] - a[1])[0][0];
  }

  function renderEnvironments(envs) {
    const box = document.getElementById("environments");
    const toc = [];
    const parts = [];
    const section = (id, title, ...content) => {
      toc.push([id, title]);
      parts.push(el("h2", { id, class: "rv-section" }, title), ...content.filter(Boolean));
    };
    if (!envs.environments.length) {
      const why = (envs.stacks || []).filter((s) => s.no_env_reason).map((s) => `${s.id}: ${s.no_env_reason}`);
      parts.push(el("p", { class: "st-unknown" },
        "No environment found for any stack — the inventory below is the code with the variables' defaults. Why, per stack:"),
        el("ul", { class: "overview-lines" }, ...(why.length ? why : ["no stack (no root module) found"]).map((w) => el("li", { class: "ov-line" }, w))));
    } else if (multiStack(envs)) {
      parts.push(el("p", {}, `Shown: ${(envs.selected || []).join(", ") || "no environment (variable defaults)"}. Every stack is analyzed with its own environment: pick one per stack below. The flows, the Root view and the graph follow these selections.`));
    } else {
      const shown = envs.active ? `${envs.active}${envs.default ? " (default)" : ""}` : "no environment (variable defaults)";
      parts.push(el("p", {}, `Shown: ${shown}. The flows, the Root view (Network, IAM) and the graph follow the environment selected here or in the switcher above.`));
    }
    const counts = envs.counts;
    if ((envs.stacks || []).length > 1) {
      const library = counts.library_stacks ? ` Reusable modules nothing calls are listed but not counted (${counts.library_stacks}).` : "";
      section("env-stacks", `Stacks (${counts.stacks})`,
        el("p", { class: "muted" }, `Each root with its own state is a stack; its environments are the tfvars / backend configurations bound to it (the nearest root above them).${library}`),
        stacksTable(envs));
    }
    if ((envs.stack_links || []).length) {
      section("env-links", "Links between stacks (remote state)", stackLinksList(envs));
    }
    if (envs.environments.length) {
      if (envs.not_wired.length) {
        parts.push(el("p", { class: "st-unknown" },
          `Not wired: ${envs.not_wired.join(", ")} — tfvars only; still analyzed.`));
      }
      section("env-files", `Detected environments (${counts.environments})`,
        counts.stack_environments !== counts.environments
          ? el("p", { class: "muted" }, `${counts.stack_environments} rows: one per stack that has the environment.`) : null,
        envFilesTable(envs));
      section("env-state", "State file per environment", stateTable(envs));
    }
    section("env-inventory", "Service inventory",
      el("p", { class: "muted" }, "Confirmed instances each environment creates with its own tfvars. Click a count for the names, file:line and why it is counted so."),
      inventoryGrid(envs));
    if (envs.environments.length) {
      section("env-diffs", "Differences between environments",
        envs.differences.length
          ? el("ul", { class: "overview-lines" }, ...envs.differences.map((d) =>
            el("li", { class: d.startsWith(" ") ? "ov-line lvl-1" : "ov-line" }, d.trim())))
          : el("p", { class: "muted" }, "None: every environment creates the same services."));
    }
    toc.push(["env-unknowns", "Unknowns"]);
    parts.push(el("div", { id: "env-unknowns" }, ...envUnknowns(envs)));
    box.replaceChildren(...parts);
    document.getElementById("env-toc").replaceChildren(...tocLinks(toc));
  }

  async function selectEnv(name) {
    pickedEnv = name || null;
    const select = document.getElementById("env");
    select.disabled = true;
    try {
      await api(`api/env?name=${encodeURIComponent(name)}`, { method: "POST", headers: { "X-TFLens": "1" } });
      await loadAll();
    } catch (err) {
      document.getElementById("detail").replaceChildren(el("p", {}, `Environment not applied: ${err.message}`));
      select.disabled = false;
    }
  }

  function renderEnvSwitch(envs) {
    const box = document.getElementById("env-switch");
    const select = document.getElementById("env");
    // several stacks: each has its own selector in the Environments view
    box.classList.toggle("hidden", !envs.environments.length || multiStack(envs));
    select.replaceChildren(el("option", { value: "" }, "no environment (variable defaults)"),
      ...envs.environments.map((r) => el("option", { value: r.id },
        `${r.id}${envs.default && r.id === envs.active ? " (default)" : ""} — ${r.text}`)));
    select.value = envs.active || "";
    select.disabled = !envs.switchable;
  }

  function sumCounts(counts) {
    return Object.values(counts || {}).reduce((a, b) => a + b, 0);
  }

  // One row per stack, one column per environment: the instances that stack
  // creates there with its own tfvars (hover a number for the types).
  function stackCountsTable(envs, columns) {
    const names = columns.map((c) => c.name || c.id);
    const stacks = (envs.stacks || []).filter((s) => !s.library && s.environments.length);
    const cell = new Map(envs.environments.map((r) => [`${r.stack}\u0000${r.name}`, r]));
    const types = new Map((envs.types || []).map((t) => [t.type, t.label]));
    const head = el("tr", {}, el("th", {}, "Stack"), ...names.map((n) => el("th", { class: "num" }, n)));
    const rows = stacks.map((s) => el("tr", { "data-stack": s.id }, el("td", { class: "mono" }, s.id),
      ...names.map((n) => {
        const r = cell.get(`${s.id}\u0000${n}`);
        if (!r) return el("td", { class: "num muted", title: `${s.id} has no ${n} environment` }, "—");
        const what = Object.entries(r.counts || {}).map(([t, c]) => `${types.get(t) || t}: ${c}`).join("\n");
        return el("td", { class: "num", title: what || "nothing counted" }, String(sumCounts(r.counts)));
      })));
    return el("table", { class: "env-counts stack-counts" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  function renderOverview(ov, envs) {
    const box = document.getElementById("overview");
    const toc = [];
    const parts = [];
    // the default view: only the final, validated results
    toc.push(["ov-confirmed", "Confirmed results"]);
    parts.push(el("h2", { id: "ov-confirmed", class: "rv-section" }, "Confirmed results"));
    const cols = envs ? envColumns(envs) : [];
    if (envs && cols.length) {
      parts.push(el("p", { class: "muted" }, `${cols.length} confirmed environment(s), each analyzed with its own tfvars: only what its count / for_each / conditionals create is counted.`));
      toc.push(["ov-env-counts", "Counts per environment"]);
      parts.push(el("h3", { id: "ov-env-counts" }, "Counts per environment"), envCountsTable(envs, cols));
      if ((envs.stacks || []).filter((s) => !s.library && s.environments.length).length) {
        toc.push(["ov-stack-counts", "Counts per stack"]);
        parts.push(el("h3", { id: "ov-stack-counts" }, "Counts per stack"), stackCountsTable(envs, cols));
      }
    } else if (envs) {
      const cur = envs.current;
      parts.push(el("p", { class: "muted" }, "No confirmed environment: the counts below are the code with the variables' defaults."),
        el("table", { class: "env-counts" }, el("thead", {}, el("tr", {}, ...(envs.types || []).map((t) => el("th", { class: "num" }, t.label)), el("th", { class: "num" }, "Unknowns"))),
          el("tbody", {}, el("tr", {}, ...(envs.types || []).map((t) => el("td", { class: "num" },
            String((cur.counts || {})[t.type] || 0))), el("td", { class: "num" }, unknownCount(cur, "Unknowns"))))));
    }
    // everything else (why-chains, per-root explanations) behind one toggle
    const details = [];
    const dsec = (id, title, ...content) => {
      toc.push([id, title]);
      details.push(el("h2", { id, class: "rv-section" }, title), ...content);
    };
    dsec("ov-top", "What this repository deploys", overviewList(ov.summary));
    if (envs && envs.environments.length) details.push(...envUnknowns(envs));
    if (ov.inventory) {
      // a failed analysis: what is in the repository is the result worth showing
      parts.push(el("h3", {}, "What is in the repository"), inventoryTable(ov.inventory));
    }
    ov.roots.forEach((r, i) => {
      const id = `ov-root-${i}`;
      toc.push([id, r.title]);
      details.push(el("h2", { id, class: "ov-root" }, `Root ${r.title}`, " ", locLink(r.location)));
      if (!r.analyzed) details.push(el("p", { class: "unknown" }, `Not analyzed: ${r.status}`));
      for (const s of r.sections) {
        details.push(el("h3", {}, s.title), overviewList(s.lines));
      }
    });
    if (ov.resources && ov.resources.length) {
      toc.push(["ov-res", "All resources"]);
      details.push(allResources(ov, "all-resources-ov", "ov-res", false));
    }
    if (ov.environments.length) dsec("ov-envs", "Environments", overviewList(ov.environments));
    if (ov.aws) dsec("ov-aws", "AWS check", ...awsCheck(ov.aws));
    dsec("ov-files", "Files read", overviewList(ov.discovery));
    parts.push(el("details", { class: "ov-details", id: "ov-details" },
      el("summary", {}, "Details — what it deploys, per-root explanations, resources, files read"),
      ...details));
    box.replaceChildren(...parts);
    document.getElementById("toc").replaceChildren(...tocLinks(toc));
  }

  // The optional read-only check of a run in AWS: what it found, never a guess.
  function awsCheck(aws) {
    const parts = [el("p", { class: aws.status === "done" ? "" : "muted" }, `${aws.status}: ${aws.message}`)];
    if (aws.checks.length) {
      parts.push(el("ul", { class: "overview-lines" }, ...aws.checks.map((c) => el("li", { class: c.status === "exists" ? "ov-line" : "ov-line st-unknown" },
        link(c.id, () => showDetail("node", c.id)), ` ${c.name}: ${c.status}${c.detail ? ` (${c.detail})` : ""}`))));
    }
    return parts;
  }

  // The resource inventory, collapsed: the flows come first.
  function allResources(ov, cls, id, withSummary) {
    const res = ov.resources || [];
    const parts = [el("summary", {}, `All resources (${res.length})`)];
    if (withSummary) parts.push(el("h3", {}, "What this repository deploys"), overviewList(ov.summary));
    parts.push(el("p", { class: "muted" }, "What each resource is, what triggers it, what it can access and what accesses it."),
      ...res.map(drilldown));
    return el("details", { class: cls, id }, ...parts);
  }

  // ------------------------------------------------------------ flow (the landing view)
  function flowStep(s) {
    const parts = [s.summary, " ("];
    s.evidence.forEach((ev, i) => {
      if (i) parts.push(" + ");
      parts.push(ev.what, " ", locLink(ev.location) || "?");
    });
    if (s.note) parts.push(`; ${s.note}`);
    parts.push(")");
    return el("li", { value: s.n, class: s.status ? `st-${s.status}` : "", "data-step": s.n }, ...parts);
  }

  // The flow content (start here, metrics, one diagram + numbered steps per chain).
  // `pre` keeps element ids unique when a root's own Flow view is shown next to the
  // repository's; `diagramQuery` selects that root's chains (`&root=<dir>`).
  function flowParts(fl, pre, diagramQuery) {
    const m = fl.metrics;
    const toc = [[`${pre}-start`, "Start here"]];
    const parts = [
      el("div", { class: "start-here", id: `${pre}-start` },
        el("h2", {}, "Start here"), overviewList(fl.start_here)),
      el("div", { class: "flow-metrics", id: pre === "fl" ? "flow-metrics" : `${pre}-metrics` },
        el("span", {}, `${m.flows} flows found`),
        el("span", {}, `${m.steps} steps`),
        el("span", { class: m.unverified ? "warn" : "" }, `${m.unverified} unverified links`),
        el("span", {}, `${m.iam_only} IAM-only (can write / can read)`),
        el("span", { class: m.compute_covered < m.compute_total ? "warn" : "" },
          `${m.compute_covered}/${m.compute_total} compute covered`)),
    ];
    if (!fl.chains.length) {
      parts.push(el("p", { class: "muted" },
        "No flows found: no trigger, subscription, notification, rule, API or load balancer is declared for the code here."));
    }
    // one heading per group (Lambda / ECS / EC2 / Network / Other), in the server's order
    const byId = new Map(fl.chains.map((c) => [c.id, c]));
    for (const g of fl.groups) {
      const gid = `${pre}-group-${g.group.toLowerCase()}`;
      toc.push([gid, `${g.group} (${g.flows.length})`]);
      parts.push(el("h2", { id: gid, class: "flow-group" }, `${g.group} (${g.flows.length})`));
      for (const c of g.flows.map((i) => byId.get(i))) parts.push(...flowChain(c, pre, diagramQuery, toc));
    }
    return { parts, toc };
  }

  // What a compute runs in / as (cluster, task definition, containers, roles, ...).
  function flowContext(c) {
    const ctx = c.nodes.filter((n) => n.role === "context");
    if (!ctx.length) return [];
    const label = new Map(c.nodes.map((n) => [n.id, n.label]));
    return [el("h3", {}, "Runs in / as"),
      el("ul", { class: "flow-context" }, ...ctx.map((n) =>
        el("li", {}, `${label.get(n.parent) || n.parent} ${n.relation}: ${n.label} `, locLink(n.code_ref))))];
  }

  // The download links of a flow / Network / IAM diagram: the SVG, and the same diagram
  // as an editable draw.io file (full names, same colours and legend).
  function diagramLinks(name, query) {
    const href = (fmt) => `api/diagram?name=${name}&format=${fmt}${query || ""}&download=1`;
    return [el("a", { class: "link", href: href("svg"), download: `${name}.svg` }, "SVG"), " · ",
      el("a", { class: "link", href: href("drawio"), download: `${name}.drawio`,
        title: "The same diagram as an editable draw.io (diagrams.net) file" }, "Download .drawio")];
  }

  function flowChain(c, pre, diagramQuery, toc) {
    const id = `${pre}-${c.id}`;
    toc.push([id, `Flow ${c.n}: ${c.entry_label}`]);
    const entry = c.no_trigger
      ? el("p", { class: "muted" }, `No trigger: ${c.entry_label} (${c.entry_kind}). `)
      : el("p", { class: "muted" }, `Entry point: ${c.entry_label} (${c.entry_kind}). `);
    entry.append(...diagramLinks(c.id, diagramQuery));
    const parts = [el("h2", { id, class: "flow-title" }, `Flow ${c.n}: ${c.title}`), entry,
      diagramView(c.id, `Flow ${c.n}`, diagramQuery),
      c.steps.length ? el("ol", { class: "flow-steps" }, ...c.steps.map(flowStep))
        : el("p", { class: "muted" }, "No step: nothing it sends to or reads from is declared."),
      ...flowContext(c)];
    if (c.side.length) {
      parts.push(el("h3", {}, "Side branches (dead-letter queues, alarms, logs)"),
        el("ul", { class: "flow-side" }, ...c.side.map((s) => el("li", {}, s.text, " ", locLink(s.location)))));
    }
    return parts;
  }

  function tocLinks(toc) {
    return toc.map(([tid, label]) =>
      el("a", { class: "file link", onclick: () => {
        const target = document.getElementById(tid);
        // a section inside a closed details toggle: open it first
        for (let d = target.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
        target.scrollIntoView();
      } }, label));
  }

  function renderFlow(fl, ov) {
    const box = document.getElementById("flow");
    const { parts, toc } = flowParts(fl, "fl", "");
    toc.push(["fl-all", "All resources"]);
    parts.push(allResources(ov, "all-resources", "fl-all", true));
    box.replaceChildren(...parts);
    document.getElementById("flow-toc").replaceChildren(...tocLinks(toc));
  }

  // ------------------------------------------------------------ roots (the landing view)
  // Each root is classified by shape (app / platform / network / module library) and
  // shown in its primary view; the switchers pick another root or another view.
  let shapesData = null;

  function renderRoots(shapes) {
    shapesData = shapes;
    const list = document.getElementById("root-list");
    if (!shapes.roots.length) {
      list.replaceChildren(el("p", { class: "muted" }, "No root found."));
      document.getElementById("roots").replaceChildren(
        el("p", { class: "muted" }, "No Terraform or Terragrunt root found: see the Overview."));
      return;
    }
    // the root switcher, grouped by shape
    list.replaceChildren(...shapes.groups.flatMap((g) => [
      el("div", { class: "tag shape-group" }, g.label),
      ...g.roots.map((r) => el("a", { class: "file link root-link", "data-root": r.dir,
        title: r.summary, onclick: () => showRootView(r.dir, "") }, r.title)),
    ]));
    // after an environment switch, the same root in the same view (Network, IAM, ...),
    // now with that environment's analysis
    const kept = shownRoot && shapes.roots.some((r) => r.dir === shownRoot.root);
    showRootView(kept ? shownRoot.root : shapes.default_root, kept ? shownRoot.view : "");
  }

  let shownRoot = null; // {root, view} last shown in the Root view
  let cardsData = []; // api/stack-cards: one card per stack, above the root shown

  // ------------------------------------------------------------ stack cards
  function chip(text, cls, attrs) {
    return el("span", { class: `chip ${cls || ""}`.trim(), ...(attrs || {}) }, text);
  }

  function linkLine(lk, inbound) {
    const other = inbound ? lk.from_stack || "?" : lk.to_stack || `${lk.backend || "?"} state outside this repository`;
    return el("li", { class: lk.status === "unresolved" ? "st-unknown" : "" },
      inbound ? "← " : "→ ", el("span", { class: "mono" }, other), lk.env ? ` (${lk.env})` : "", " via ",
      link(lk.data_source.split("::").pop(), () => showDetail("node", lk.data_source)), " ", locLink(lk.location));
  }

  function stackCard(c) {
    const envs = c.environments.length
      ? c.environments.map((e) => chip(e.name, "env"))
      : [el("span", { class: "muted" }, "none (variable defaults)")];
    const perEnv = Object.entries(c.backend.per_env || {});
    const backend = perEnv.length
      ? el("ul", { class: "card-list" }, ...perEnv.map(([env, cell]) => el("li", {},
        el("button", { type: "button", class: "link-btn", title: "Open the backend file",
          onclick: () => showBackendCell({ stack: c.id }, env, cell) }, `${env}:`), " ",
        el("span", { class: "mono wrap" }, [cell.bucket, cell.key].filter(Boolean).join(" / ") || cell.type))))
      : el("span", { class: "mono wrap" }, c.backend.text || "—", " ", locLink(c.backend.location));
    const resources = c.resources.length
      ? el("div", { class: "chips" }, ...c.resources.map((r) => {
        const ch = chip(`${r.label} ×${r.count}`, "res", { title: r.type });
        const st = typeStyle(r.type);
        if (st) { ch.prepend(svcIcon(st)); ch.style.borderColor = st.colour; }
        return ch;
      }))
      : el("span", { class: "muted" }, "none declared");
    const modules = c.modules.length
      ? el("ul", { class: "card-list" }, ...c.modules.map((m) => el("li", {},
        link(m.address, () => showDetail("node", m.node)), " ",
        el("span", { class: "tag mono" }, `${m.source}${m.version ? ` @ ${m.version}` : ""}${m.analyzed ? "" : " (not analyzed)"}`))))
      : el("span", { class: "muted" }, "none");
    const links = c.outbound.length || c.inbound.length
      ? el("ul", { class: "card-list" }, ...c.outbound.map((l) => linkLine(l, false)), ...c.inbound.map((l) => linkLine(l, true)))
      : el("span", { class: "muted" }, "none");
    return el("article", { class: "stack-card", "data-stack": c.id },
      el("div", { class: "card-head" },
        el("h3", {}, link(c.id, () => c.node && showDetail("node", c.node))),
        c.layout ? el("span", { class: "tag" }, c.layout) : null),
      el("dl", { class: "card-facts" },
        el("dt", {}, "Environments"), el("dd", { class: "chips" }, ...envs),
        el("dt", {}, "Backend"), el("dd", {}, backend),
        el("dt", {}, `Resources (${c.resource_total})`), el("dd", {}, resources),
        el("dt", {}, `Modules (${c.modules.length})`), el("dd", {}, modules),
        el("dt", {}, "Remote state"), el("dd", {}, links)),
      el("div", { class: "card-foot" }, el("button", { type: "button", class: "btn",
        onclick: () => showRootView(c.roots[0], "").then(() => {
          const head = document.getElementById("rv-root");
          if (head) head.scrollIntoView();
        }) }, "Open in Root view")));
  }

  function stackCards() {
    if (!cardsData.length) return [];
    return [el("h2", { id: "rv-stacks", class: "rv-section first" }, `Stacks (${cardsData.length})`),
      el("p", { class: "muted" }, "Resources are the blocks each stack declares, by type."),
      el("div", { class: "stack-cards" }, ...cardsData.map(stackCard))];
  }

  function shapesSummary(shapes) {
    const head = shapes.mixed
      ? `Mixed repository: ${shapes.roots.length} roots of ${shapes.groups.length} shapes, each shown in its own view.`
      : `${shapes.roots.length} root${shapes.roots.length === 1 ? "" : "s"}, all ${shapes.groups[0].label}.`;
    return el("div", { class: "start-here", id: "rv-shapes" },
      el("h2", {}, "Roots by shape"), el("p", {}, head), overviewList(shapes.lines));
  }

  async function showRootView(root, view) {
    const box = document.getElementById("roots");
    for (const a of document.querySelectorAll("#root-list .root-link")) {
      a.classList.toggle("active", a.dataset.root === root);
    }
    let d;
    try {
      d = await api(`api/root-view?root=${encodeURIComponent(root)}&view=${encodeURIComponent(view)}`);
    } catch (err) {
      box.replaceChildren(el("p", { class: "muted" }, `${root}: ${err.message}`));
      return;
    }
    shownRoot = { root, view: view || "" };
    const s = d.shape;
    const toc = [...(cardsData.length ? [["rv-stacks", "Stacks"]] : []),
      ["rv-shapes", "Roots by shape"], ["rv-root", `Root ${s.title}`]];
    const parts = [...stackCards(), shapesSummary(shapesData),
      el("h2", { id: "rv-root", class: "rv-root" }, `Root ${s.title} `,
        el("span", { class: `shape-badge shape-${s.shape}` }, s.shape_label), " ", locLink(s.location)),
      el("p", { class: "rv-summary" }, s.summary),
      el("p", { class: "muted rv-reason" }, `Why: ${s.reason}`),
      el("div", { class: "view-switcher" }, el("span", { class: "muted" }, "View: "),
        ...s.views.map((v) => el("button", {
          type: "button", "data-rview": v.view,
          class: v.view === d.view ? "active" : "",
          onclick: () => showRootView(root, v.view),
        }, v.primary ? `${v.label} (primary)` : v.label)))];
    if (d.view === "flow") {
      const fp = flowParts(d.flow, "rv", `&root=${encodeURIComponent(root)}`);
      parts.push(...fp.parts);
      toc.push(...fp.toc);
    } else {
      for (const sec of d.sections) {
        const id = `rv-${sec.id}`;
        toc.push([id, sec.title]);
        parts.push(el("h2", { id, class: "rv-section" }, sec.title, " ", locLink(sec.location)),
          overviewList(sec.lines));
        if (sec.code) {
          parts.push(el("h3", {}, sec.code_title || "Code"), el("pre", { class: "code" }, sec.code));
        }
      }
      // the Network / IAM views: one diagram of the root, drawn like the flows
      if (d.diagram) {
        const q = `&root=${encodeURIComponent(root)}`;
        const id = `rv-diagram-${d.diagram}`;
        toc.push([id, `${d.view_label} diagram`]);
        parts.push(el("h2", { id, class: "rv-section" }, `${d.view_label} diagram `,
          ...diagramLinks(d.diagram, q)),
          canvasReady() ? diagramCanvas(d.diagram, `${d.view_label} diagram`, q)
            : diagramPanel(d.diagram, `${d.view_label} diagram`, q));
      }
    }
    box.replaceChildren(...parts);
    document.getElementById("roots-toc").replaceChildren(...tocLinks(toc));
  }

  function inventoryTable(inv) {
    return el("table", { class: "inventory" },
      el("tr", {}, el("th", {}, "Directory"), el("th", {}, "Files"), el("th", {}, "By extension")),
      ...inv.entries.map((e) => el("tr", {},
        el("td", { class: "mono" }, e.path === "." ? "./" : `${e.path}/`),
        el("td", {}, e.skipped ? "—" : e.files),
        el("td", {}, e.skipped ? `skipped: ${e.skipped}` :
          Object.entries(e.by_extension).map(([k, v]) => `${k} ${v}`).join(", ") || "no files"))));
  }

  function renderFailure(summary, ov) {
    const box = document.getElementById("failure");
    const unreadable = (summary.skipped || []).filter((s) => s.kind === "unreadable");
    if (summary.status !== "failed" && !unreadable.length) {
      box.classList.add("hidden");
      return;
    }
    const parts = [];
    if (summary.status === "failed") {
      parts.push(el("strong", {}, "Analysis failed: "), summary.reason, ". ",
        "The Repository tab lists every file found; the Overview shows what is in the repository.");
    }
    if (unreadable.length) {
      parts.push(el("div", {}, el("strong", {}, `${unreadable.length} directory(ies) could not be read: `),
        unreadable.map((s) => `${s.path}/ (${s.reason})`).join("; ")));
    }
    box.replaceChildren(...parts);
    box.classList.remove("hidden");
    if (ov && summary.status === "failed") setView("overview");
  }

  // ------------------------------------------------------------ detail panel
  // A resource's tags (each with where it comes from: the provider's default_tags or its
  // own tags), the stack a terraform_remote_state reads, the instance a node shows.
  function nodeExtras(p) {
    const out = [];
    if (p.instance_shown) out.push(el("p", { class: "muted" }, `Several instances: ${p.instance_shown}.`));
    const tags = Object.entries(p.tags || {});
    if (tags.length || (p.tags_unknown || []).length) {
      const src = p.tag_sources || {};
      const where = p.tag_locations || {};
      out.push(el("h3", {}, `Tags (${tags.length})`),
        el("table", { class: "tags" }, el("tbody", {}, ...tags.map(([k, v]) => {
          const origin = src[k] || "resource";
          const via = origin === "default_tags" ? `default_tags (provider ${p.tag_provider || "?"})` : "tags";
          return el("tr", { class: v === "UNKNOWN" ? "st-unknown" : "" },
            el("td", { class: "mono" }, k), el("td", { class: "mono" }, v),
            el("td", { class: "muted" }, via, " ", where[origin] ? locLink(where[origin]) : ""));
        }))),
        ...(p.tags_unknown || []).filter((u) => u.key === "*").map((u) =>
          el("p", { class: "st-unknown" }, `${u.source} tags not known statically: ${u.reason}`)));
    }
    const rs = p.remote_state;
    if (rs) {
      const target = rs.stack ? `stack ${rs.stack}${rs.env ? ` (${rs.env})` : ""}` : `${rs.backend} state outside this repository`;
      out.push(el("h3", {}, "Remote state"),
        el("p", { class: rs.status === "unresolved" ? "st-unknown" : "" }, `Reads ${target} — ${rs.reason}`),
        el("dl", {}, ...Object.entries(rs.config || {}).flatMap(([k, v]) =>
          [el("dt", {}, k), el("dd", { class: "mono" }, v === null ? "UNKNOWN" : String(v))])));
    }
    return out;
  }

  async function showDetail(type, id) {
    const panel = document.getElementById("detail");
    let d;
    try {
      d = await api(`api/${type}?id=${encodeURIComponent(id)}`);
    } catch (err) {
      panel.replaceChildren(el("p", { class: "muted" }, `${type} ${id}: ${err.message}`));
      return;
    }
    if (cy) {
      const ele = cy.getElementById(id);
      focus(ele.length ? ele : cy.getElementById(`group:${id}`));
    }
    const s = d.summary;
    // the label the views draw (module call · type), the full address right under it
    const parts = type === "node"
      ? [el("h2", {}, s.label || d.title), el("div", { class: "address mono", title: "Full address" }, d.title)]
      : [el("h2", {}, d.title)];
    if (type === "node") {
      parts.push(el("dl", {},
        el("dt", {}, "Name"), el("dd", {}, s.short_name),
        el("dt", {}, "Address"), el("dd", { class: "mono" }, d.id),
        el("dt", {}, "What"), el("dd", {}, s.description),
        el("dt", {}, "Type"), el("dd", {}, s.type),
        el("dt", {}, "Service"), el("dd", {}, s.service || "—"),
        el("dt", {}, "Module"), el("dd", {}, s.module),
        s.kind === "root" ? null : el("dt", {}, "Edges"),
        s.kind === "root" ? null : el("dd", {}, `${s.outgoing} out, ${s.incoming} in`)));
      parts.push(...nodeExtras(d.node.properties || {}));
      if (d.modules && d.modules.length) {
        parts.push(el("h3", {}, `Modules called (${d.modules.length})`),
          el("ul", { class: "links" }, ...d.modules.map((m) =>
            el("li", { class: "mono" }, link(m.address, () => showDetail("node", m.node))))));
      }
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
    else parts.push(unknownsPanel(groupUnknowns(d.unknowns)));
    panel.replaceChildren(...parts);
    panel.scrollTop = 0;
  }

  // ------------------------------------------------------------ graph
  const PRESETS = [
    ["Access", ["ACCESS"]],
    ["Network", ["NETWORK"]],
    ["All", null], // every kind
  ];

  function checkboxList(container, values, colorOf, line, isOn) {
    container.replaceChildren(...values.map((v) => {
      const attrs = { type: "checkbox", value: v, onchange: applyFilters };
      if (!isOn || isOn(v)) attrs.checked = "";
      return el("label", {},
        el("input", attrs),
        colorOf ? el("span", { class: line ? "swatch line" : "swatch", style: `background:${colorOf(v)}` }) : null,
        v);
    }));
  }

  function checked(id) {
    return new Set([...document.querySelectorAll(`#${id} input:checked`)].map((i) => i.value));
  }

  function setKinds(kinds) {
    for (const input of document.querySelectorAll("#filter-kinds input")) {
      input.checked = kinds === null || kinds.includes(input.value);
    }
    applyFilters();
  }

  // Graph controls that index.html does not declare are added here (once).
  function ensureGraphControls() {
    if (document.getElementById("group-modules")) return;
    const isolated = document.getElementById("hide-isolated").closest("label");
    isolated.after(
      el("label", { class: "inline" },
        el("input", { type: "checkbox", id: "group-modules", onchange: () => graphData && buildGraph(graphData, true) }),
        " Group by module"),
      el("label", { class: "inline", title: "aws_caller_identity, aws_region, aws_partition, aws_iam_policy_document" },
        el("input", { type: "checkbox", id: "show-meta", onchange: applyFilters }),
        " Show meta data sources"));
    document.getElementById("filter-kinds").before(
      el("div", { class: "presets" }, ...PRESETS.map(([name, kinds]) =>
        el("button", { type: "button", onclick: () => setKinds(kinds) }, name))));
    document.getElementById("search").after(
      el("div", { class: "presets" },
        el("button", { type: "button", id: "graph-fit", title: "Show the whole graph (labels may be too small to read)", onclick: () => cy && fitGraph(true) }, "Fit all"),
        el("button", { type: "button", id: "graph-readable", title: "Zoom to a readable size from the top-left corner", onclick: () => cy && fitGraph(false) }, "Readable size")));
  }

  function visible(ele) {
    return ele.style("display") !== "none";
  }

  function applyFilters() {
    if (!cy) return;
    const kinds = checked("filter-kinds");
    const services = checked("filter-services");
    const modules = checked("filter-modules");
    const showMeta = document.getElementById("show-meta").checked;
    const hideIsolated = document.getElementById("hide-isolated").checked;
    const q = document.getElementById("search").value.trim().toLowerCase();
    const members = cy.nodes().not(".group").not(".structure");
    const structure = cy.nodes(".structure");
    const groups = cy.nodes(".group");
    const contains = cy.edges('[kind = "CONTAINS"]');
    const flows = cy.edges().not(contains);
    cy.batch(() => {
      members.forEach((n) => {
        const service = n.data("service");
        const ok = (service === "meta" ? showMeta : services.has(service)) && modules.has(n.data("module"));
        n.style("display", ok ? "element" : "none");
      });
      flows.forEach((e) => {
        const ok = kinds.has(e.data("kind")) && visible(e.source()) && visible(e.target());
        e.style("display", ok ? "element" : "none");
      });
      if (hideIsolated) {
        // containment (resource -> module -> root) does not count as a connection
        members.forEach((n) => {
          if (visible(n) && !n.connectedEdges().not(contains).filter(visible).length) n.style("display", "none");
        });
      }
      // ungrouped: a module call / root node stays while anything it contains is shown
      // (nested calls first, roots last)
      structure.sort((a, b) => structDepth(b) - structDepth(a)).forEach((s) => {
        const shown = s.outgoers('edge[kind = "CONTAINS"]').targets().filter(visible).length;
        s.style("display", shown ? "element" : "none");
      });
      contains.forEach((e) => e.style("display", visible(e.source()) && visible(e.target()) ? "element" : "none"));
      // a module group stays visible while any of its members is (deepest groups first)
      groups.sort((a, b) => b.ancestors().length - a.ancestors().length).forEach((g) => {
        g.style("display", g.children().filter(visible).length ? "element" : "none");
      });
      cy.elements().removeClass("faded match");
      if (q) {
        const hits = members.filter((n) =>
          visible(n) && [n.id(), n.data("name"), n.data("display")].some((v) => String(v).toLowerCase().includes(q)));
        cy.elements().addClass("faded");
        hits.removeClass("faded").addClass("match");
        hits.ancestors().removeClass("faded");
        hits.connectedEdges().removeClass("faded");
      }
    });
    if (graphCanvas) graphCanvas.refreshLegend();
    graphEmptyNote();
  }

  function structDepth(n) {
    return n.data("kind") === "root" ? -1 : String(n.id()).split(".module.").length;
  }

  // Filters that leave nothing to draw say so instead of an empty canvas.
  function graphEmptyNote() {
    let note = document.getElementById("graph-note");
    if (!note) {
      note = el("div", { id: "graph-note", class: "graph-note hidden" });
      document.getElementById("center").append(note);
    }
    const shown = cy ? cy.nodes(":visible").length : 0;
    note.textContent = shown ? "" : "Nothing to draw with these filters: tick more edge kinds, services or modules, or untick “Hide unconnected nodes”.";
    note.classList.toggle("hidden", Boolean(shown) || document.getElementById("graph").classList.contains("hidden"));
  }

  // Highlight an element's neighbourhood and dim everything else (Esc resets).
  function focus(ele) {
    clearFocus();
    if (!cy || !ele || !ele.length) return;
    let keep;
    if (ele.isEdge()) keep = ele.union(ele.connectedNodes());
    else if (ele.hasClass("group")) keep = ele.union(ele.descendants()).union(ele.descendants().connectedEdges());
    else keep = ele.closedNeighborhood();
    keep = keep.union(keep.ancestors());
    cy.batch(() => {
      cy.elements().not(keep).addClass("dimmed");
      keep.addClass("focused");
    });
    ele.select();
  }

  function clearFocus() {
    if (!cy) return;
    cy.batch(() => cy.elements().removeClass("dimmed focused").unselect());
  }

  // The Graph's hover text: short (a title and one or two lines); a click opens the rest.
  function graphTip(t) {
    if (t.isEdge()) return [`${t.data("kind")} ${t.data("short_label") || t.data("label")}`, `${t.source().data("display") || t.source().id()} → ${t.target().data("display") || t.target().id()}`];
    if (t.hasClass("group")) {
      return [t.data("label"), `${t.descendants().not(".group").length} nodes · click for its code`];
    }
    return [t.data("display") || t.id(), t.data("type") ? `${t.data("type")} · ${t.data("service")}` : t.id()];
  }

  // A diagram canvas' hover text.
  function canvasTip(t) {
    if (t.isEdge()) {
      return [`${t.data("kind")}${t.data("label") ? ` · ${t.data("label")}` : ""}`,
        `${t.source().data("label") || t.source().id()} → ${t.target().data("label") || t.target().id()}`];
    }
    return [t.data("label") || t.id(), String(t.data("sub") || "").split("\n")[0]];
  }

  // A click on a diagram canvas: the node's code, evidence and unknowns (or the code it
  // stands for), an edge's evidence.
  function canvasTap(t) {
    const d = t.data();
    if (t.isEdge()) {
      if (d.edge) showDetail("edge", d.edge);
      else if (d.evidence) showStep(d, t);
      return;
    }
    if (d.detail) showDetail("node", d.detail);
    else if (d.code_ref) showSource(d.code_ref);
  }

  // A Flow / Network / IAM step: what it is, its evidence (file:line + snippet) and what
  // is not known about it.
  async function showStep(d, t) {
    const panel = document.getElementById("detail");
    const name = (id) => (t.cy().getElementById(id).data("label") || id);
    const parts = [el("h2", {}, d.summary || d.label || d.id),
      el("dl", {},
        el("dt", {}, "Kind"), el("dd", {}, kindBadge(d.kind)),
        el("dt", {}, "Step"), el("dd", {}, d.label || "—"),
        el("dt", {}, "From"), el("dd", {}, link(name(d.source), () => showDetail("node", d.source))),
        el("dt", {}, "To"), el("dd", {}, link(name(d.target), () => showDetail("node", d.target))),
        el("dt", {}, "Status"), el("dd", {}, d.status || "verified"))];
    parts.push(el("h3", {}, `Evidence (${d.evidence.length})`));
    if (!d.evidence.length) parts.push(el("p", { class: "muted" }, "No evidence recorded."));
    for (const ev of d.evidence) {
      const loc = ev.location;
      if (!loc || !loc.start_line) {
        parts.push(el("p", { class: "muted" }, `${ev.what}: no code reference`));
        continue;
      }
      try {
        const src = await api(`api/source?file=${encodeURIComponent(loc.file)}&start=${loc.start_line}&end=${loc.end_line}`);
        parts.push(evidenceBlock(`${ev.what} — ${locText(loc)}`, src.lines));
      } catch (err) {
        parts.push(el("p", { class: "muted" }, `${ev.what} — ${locText(loc)}: ${err.message}`));
      }
    }
    const unknown = { unverified: "Not verified: a reference without the matching IAM grant, or the reverse.",
      iam_only: "IAM only: the role may do this, but no code refers to the target." }[d.status];
    parts.push(el("h3", {}, `Unknowns (${unknown ? 1 : 0})`),
      unknown ? el("div", { class: "unknown" }, unknown) : el("p", { class: "muted" }, "None."));
    panel.replaceChildren(...parts);
    panel.scrollTop = 0;
  }

  // Grouped: root ⊃ module call ⊃ resource as compound nodes (containment). Ungrouped:
  // one node per root and module call, with a CONTAINS edge to what each contains.
  function elements(data, grouped) {
    // fresh copies: the graph is rebuilt from the same data when grouping is toggled
    const pos = data.positions || {};
    const at = (d) => (pos[d.id] ? { x: pos[d.id].x, y: pos[d.id].y } : undefined);
    const nodes = data.nodes.map((n) => ({
      data: { ...n.data, parent: grouped ? n.data.group || n.data.root_group || null : null },
      position: at(n.data),
    }));
    const edges = data.edges.map((e) => ({ data: { ...e.data } }));
    const st = data.structure || { nodes: [], edges: [] };
    if (!grouped) {
      return {
        nodes: nodes.concat(st.nodes.map((s) => ({ data: { ...s.data }, classes: s.classes, position: at(s.data) }))),
        edges: edges.concat(st.edges.map((e) => ({ data: { ...e.data } }))),
      };
    }
    const roots = (data.root_groups || []).map((g) => ({ data: { ...g.data }, classes: g.classes }));
    const groups = data.groups.map((g) => ({
      data: { ...g.data, parent: g.data.parent || g.data.root_group || null }, classes: g.classes,
    }));
    return { nodes: roots.concat(groups, nodes), edges };
  }

  // Colours the canvas cannot take from CSS: read from the theme's custom properties.
  function themeColor(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function graphStyle() {
    const text = themeColor("--fg", "#1f2328");
    const bg = themeColor("--bg-panel", "#ffffff");
    const muted = themeColor("--fg-muted", "#424a53");
    const accent = themeColor("--accent", "#0969da");
    const selBg = themeColor("--sel-bg", "#ddeeff");
    const icon = (n) => (window.TFDiagram ? window.TFDiagram.iconUrl(n.data("icon")) : "none");
    return [
      // each node: its service icon framed in its service colour (as in the diagrams)
      { selector: "node", style: {
        shape: "round-rectangle", "background-color": "data(color)", "background-image": icon,
        "background-fit": "cover", "background-clip": "node",
        label: "data(display)", "font-size": 12, color: text,
        "font-family": themeColor("--sans", "system-ui, sans-serif"),
        "text-valign": "bottom", "text-margin-y": 4, width: 26, height: 26,
        "border-width": 2, "border-color": "data(color)",
        "text-wrap": "ellipsis", "text-max-width": 160,
        "text-background-color": bg, "text-background-opacity": 0.8, "text-background-padding": 2,
        "min-zoomed-font-size": 6 } },
      { selector: 'node[kind = "external"]', style: { shape: "diamond" } },
      { selector: 'node[kind = "data"]', style: { shape: "round-rectangle" } },
      { selector: 'node[category = "relationship"], node[category = "policy"]', style: { width: 18, height: 18 } },
      { selector: "node.structure", style: {
        shape: "round-rectangle", width: 34, height: 22, "font-weight": 600, "text-valign": "top", "text-margin-y": -4 } },
      { selector: "node.group", style: {
        shape: "round-rectangle", "background-color": themeColor("--group-bg", "#eef1f4"), "background-opacity": 0.55,
        "background-image": "none",
        "border-width": 1.5, "border-color": themeColor("--group-border", "#6e7781"), "border-style": "dashed",
        label: "data(label)", "font-size": 13, "font-weight": 600, color: muted,
        "text-valign": "top", "text-halign": "center", "text-max-width": 600, "text-margin-y": -2,
        "text-background-opacity": 0, padding: 18, "min-zoomed-font-size": 5 } },
      { selector: "node.root-group", style: {
        "border-style": "solid", "border-width": 2, "border-color": themeColor("--tf", "#7b42bc"),
        "background-opacity": 0.18, "font-size": 15, color: themeColor("--tf", "#7b42bc") } },
      // orthogonal edges, re-routed while a node or a frame is moved
      { selector: "edge", style: {
        "line-color": "data(color)", "target-arrow-color": "data(color)",
        "target-arrow-shape": "triangle", "curve-style": "taxi", "taxi-direction": "auto",
        "taxi-turn": "50%", "taxi-turn-min-distance": 10,
        width: 1.8, "arrow-scale": 1, "font-size": 11, color: text,
        "text-background-color": bg, "text-background-opacity": 0.9,
        "text-background-padding": 2, "text-rotation": "autorotate" } },
      { selector: 'edge[kind = "CONFIG"]', style: { "line-style": "dashed", width: 1.2 } },
      { selector: 'edge[kind = "RUNS_ON"]', style: { "line-style": "dotted" } },
      { selector: 'edge[kind = "CONTAINS"]', style: {
        "line-style": "dashed", width: 1, "target-arrow-shape": "none", "line-color": themeColor("--group-border", "#6e7781"), opacity: 0.6 } },
      { selector: "edge.hover", style: { label: "data(short_label)", width: 3, "z-index": 10 } },
      // selected: the theme's accent and selection colours
      { selector: "node:selected", style: {
        "border-width": 4, "border-color": accent, "overlay-color": accent, "overlay-opacity": 0.18,
        "overlay-padding": 4, "text-background-color": selBg, "text-background-opacity": 1,
        color: themeColor("--sel-fg", text) } },
      { selector: "node.group:selected", style: { "background-color": selBg, "background-opacity": 0.4, "overlay-opacity": 0 } },
      { selector: "edge:selected", style: {
        "line-color": accent, "target-arrow-color": accent, width: 3.5, label: "data(short_label)" } },
      { selector: ".kind-off", style: { display: "none" } },
      { selector: ".faded", style: { opacity: 0.15 } },
      { selector: ".match", style: { "border-width": 4, "border-color": themeColor("--warn", "#bf8700") } },
      { selector: ".dimmed", style: { opacity: 0.12 } },
    ];
  }

  function graphError(container, err) {
    container.replaceChildren(el("div", { class: "banner st-failed" },
      `The graph could not be drawn: ${err && err.message ? err.message : err}. `,
      "The other views and the detail pane still work."));
  }

  function buildGraph(data, keepFilters) {
    graphData = data;
    ensureGraphControls();
    if (!keepFilters) {
      const kinds = new Set(data.defaults.kinds);
      checkboxList(document.getElementById("filter-kinds"), data.filters.kinds,
        (k) => data.colors.kinds[k], true, (k) => kinds.has(k));
      checkboxList(document.getElementById("filter-services"),
        data.filters.services.filter((s) => s !== "meta"), (s) => data.colors.services[s]);
      checkboxList(document.getElementById("filter-modules"), data.filters.modules, null);
      document.getElementById("show-meta").checked = data.defaults.show_meta;
      document.getElementById("group-modules").checked = data.defaults.group_by_module;
    }
    const container = document.getElementById("graph");
    if (typeof window.cytoscape !== "function" || !window.TFDiagram) {
      container.replaceChildren(el("div", { class: "banner" },
        "Graph library not found: copy cytoscape.min.js into src/tflens/static/vendor/ (and keep diagram.js). ",
        "The repository view and detail panel still work."));
      return;
    }
    if (graphCanvas) graphCanvas.destroy();
    else if (cy) cy.destroy();
    cy = null;
    const big = data.nodes.length > 250;
    // the shared diagram component: IPLens's toolbar (fit, zoom, full screen, exports),
    // draw.io-style editing, the layout saved per run, the edge-type legend
    graphCanvas = window.TFDiagram.create(container, {
      key: "graph",
      title: "Graph",
      fileName: "graph",
      positions: data.positions,
      manualFit: true, // fitted when the tab is first shown (layoutGraph)
      style: graphStyle,
      kinds: graphKinds(data),
      // the legend and the sidebar's "Edge kinds" are the same filter
      kindFilter: {
        isOn: (k) => checked("filter-kinds").has(k),
        set: (k, on) => {
          const box = document.querySelector(`#filter-kinds input[value="${k}"]`);
          if (box) box.checked = on;
          applyFilters();
        },
      },
      onTap: (t) => {
        if (t.hasClass("group")) showDetail("node", t.data("node") || t.data("module"));
        else if (t.isEdge() && t.data("kind") === "CONTAINS") showDetail("node", t.target().id());
        else showDetail(t.isEdge() ? "edge" : "node", t.id());
      },
      onBackground: clearFocus,
      onReset: () => cy.layout({ name: "preset", fit: false, positions: (n) => data.positions[n.id()] || n.position() }).run(),
      tooltip: graphTip,
    });
    try {
      // positions come with the data (a packed, readable layout computed by the server):
      // no force-directed layout over hundreds of nodes on the page's thread
      cy = window.cytoscape({
        container: graphCanvas.canvas,
        elements: elements(data, document.getElementById("group-modules").checked),
        wheelSensitivity: 0.3,
        minZoom: 0.05,
        maxZoom: 3,
        textureOnViewport: big,
        hideEdgesOnViewport: big,
        style: graphStyle(),
        layout: { name: "preset", fit: false },
      });
    } catch (err) {
      graphError(container, err);
      throw err; // reported by loadAll as well: never swallowed
    }
    graphCanvas.attach(cy);
    applyFilters();
    graphFitted = !document.getElementById("graph").classList.contains("hidden");
    if (graphFitted) layoutGraph();
  }

  // Below this zoom node labels (12px) are unreadable and nodes shrink to dots: a large
  // graph is shown from its top-left corner at a readable size instead of fitted whole.
  const MIN_READABLE_ZOOM = 0.55;

  // The legend's edge types: each kind drawn, its colour and count.
  function graphKinds(data) {
    const counts = {};
    for (const e of data.edges) counts[e.data.kind] = (counts[e.data.kind] || 0) + 1;
    return data.filters.kinds.map((k) => ({ kind: k, colour: data.colors.kinds[k], count: counts[k] || 0 }));
  }

  function layoutGraph() {
    if (!cy) return;
    // the server's layout, with the positions saved for this run on top
    const pos = graphCanvas ? graphCanvas.positions() : (graphData && graphData.positions) || {};
    cy.layout({ name: "preset", fit: false, positions: (n) => pos[n.id()] || n.position() }).run();
    fitGraph(false);
  }

  function fitGraph(whole) {
    const eles = cy.elements(":visible");
    graphEmptyNote();
    if (!eles.length) return;
    cy.fit(eles, 30);
    if (!whole && cy.zoom() < MIN_READABLE_ZOOM) {
      const bb = eles.boundingBox();
      cy.zoom(MIN_READABLE_ZOOM);
      cy.pan({ x: 30 - bb.x1 * MIN_READABLE_ZOOM, y: 30 - bb.y1 * MIN_READABLE_ZOOM });
    }
  }

  // ------------------------------------------------------------ repository
  function treeNode(node) {
    if (node.type === "file") {
      // a doc is listed, never opened: the views point at real files only
      if (DOC_FILE.test(node.path)) return el("span", { class: "file other" }, node.name);
      // analyzed files are links with their resource count; every other file is listed too
      const analyzed = node.analyzed !== false;
      return el("a", {
        class: analyzed ? "file link analyzed" : "file link other",
        "data-path": node.path,
        title: analyzed ? "analyzed" : "not analyzed (not a Terraform / Terragrunt file)",
        onclick: () => showFile(node.path),
      }, node.name, analyzed ? el("span", { class: "tag" }, ` (${node.resources})`) : null);
    }
    const label = [node.name + "/"];
    if (node.root) label.push(" ", el("span", { class: "badge-root" }, "root"));
    if (node.modules.length) label.push(" ", el("span", { class: "tag" }, node.modules.join(", ")));
    if (node.skipped) {
      label.push(" ", el("span", { class: node.skipped_kind === "unreadable" ? "tag st-failed" : "tag" },
        `skipped: ${node.skipped}`));
      return el("div", { class: "dir skipped" }, ...label);
    }
    return el("details", { class: "dir", open: "" },
      el("summary", {}, ...label),
      el("div", {}, ...node.children.map(treeNode)));
  }

  async function showFile(path) {
    for (const a of document.querySelectorAll("#tree .file")) {
      a.classList.toggle("active", a.dataset.path === path);
    }
    const box = document.getElementById("file");
    const d = await api(`api/file?path=${encodeURIComponent(path)}`);
    const parts = [el("h2", {}, d.path)];
    if (d.analyzed === false) {
      parts.push(el("p", { class: "muted" }, d.note));
      if (d.readable) {
        parts.push(el("p", {}, link("Show the source", () => showSource({ file: d.path, start_line: 1, end_line: 1000000 }))));
      }
      box.replaceChildren(...parts);
      return;
    }
    parts.push(el("p", {}, link("Show the source", () => showSource({ file: d.path, start_line: 1, end_line: 1000000 }))));
    if (d.modules.length) parts.push(el("p", { class: "muted" }, `Module instances: ${d.modules.join(", ")}`));
    if (!d.resources.length) {
      parts.push(el("p", { class: "muted" }, "No resources, data sources or module calls defined here."));
    } else {
      parts.push(el("table", {},
        el("tr", {}, el("th", {}, "Lines"), el("th", {}, "Kind"), el("th", {}, "Type"), el("th", {}, "Id")),
        ...d.resources.map((r) => el("tr", {},
          el("td", {}, `${r.location.start_line}-${r.location.end_line}`),
          el("td", {}, r.kind), el("td", { class: "mono" }, svcLabel(typeStyle(r.type), r.type)),
          el("td", {}, link(r.id, () => showDetail("node", r.id)))))));
    }
    if (d.unknowns.length) {
      parts.push(el("h3", {}, `Unknowns in this file (${d.unknowns.length})`), unknownsPanel(groupUnknowns(d.unknowns)));
    }
    box.replaceChildren(...parts);
  }

  // ------------------------------------------------------------ diagrams
  const DIAGRAMS = [
    ["architecture", "Architecture (flows)", "Services grouped by root and module; TRIGGER / ACCESS / NETWORK flows only. Click a box for its code, evidence and unknowns."],
    ["module-tree", "Module tree", "Each root and the modules it calls, with source and version. Click a root or a module call for its code."],
    ["env-matrix", "Environment matrix", "Stacks × environments: the backend (bucket / key) each stack uses in each environment. Click a cell for its backend file; filter and sort."],
  ];
  // drawn inline (clickable nodes) instead of as an image
  const INTERACTIVE = new Set(["architecture", "module-tree"]);

  function renderDiagrams() {
    const box = document.getElementById("diagrams");
    const parts = [];
    for (const [name, title, note] of DIAGRAMS) {
      let body;
      if (name === "env-matrix") body = envMatrixBox();
      // an editable canvas; without the graph library, the clickable inline SVG
      else if (INTERACTIVE.has(name)) body = canvasReady() ? diagramCanvas(name, title) : diagramPanel(name, title, "", true);
      else body = diagramPanel(name, title);
      parts.push(el("h2", { id: `dg-${name}`, class: "rv-section" }, title),
        el("p", { class: "muted" }, note, " ",
          el("a", { class: "link", href: `api/diagram?name=${name}&format=mmd&download=1`, download: `${name}.mmd` }, "Mermaid (.mmd)"),
          " · ",
          el("a", { class: "link", href: `api/diagram?name=${name}&format=svg&download=1`, download: `${name}.svg` }, "SVG")),
        body);
    }
    box.replaceChildren(...parts);
  }

  function canvasReady() {
    return typeof window.cytoscape === "function" && Boolean(window.TFDiagram);
  }

  // The layout key of a diagram (its saved positions): name, and root when it is a root's.
  function canvasKey(name, query) {
    const root = /[?&]root=([^&]*)/.exec(`&${query || ""}`);
    const key = root ? `${name}@${decodeURIComponent(root[1])}` : name;
    return key.replace(/[^\w.:@/-]+/g, "_").slice(0, 200);
  }

  // A diagram as an editable canvas (the shared diagram component), loaded when it is
  // first shown: drag, select, move frames, snap, export, layout saved per run.
  function diagramCanvas(name, title, query) {
    const host = el("div", { class: "diagram-canvas", "data-diagram": name },
      el("p", { class: "muted" }, `Loading ${title}…`));
    window.TFDiagram.whenNear(host, () => {
      api(`api/canvas?name=${name}${query || ""}`)
        .then((data) => {
          if (!data.nodes.length) {
            host.replaceChildren(el("p", { class: "muted" }, data.empty || "Nothing to draw."));
            return;
          }
          const key = canvasKey(name, query);
          window.TFDiagram.create(host, {
            key, title: data.title || title, fileName: key,
            rankdir: name === "module-tree" ? "TB" : "LR",
            onTap: canvasTap, tooltip: canvasTip,
            onBackground: () => {},
          }).draw(data);
        })
        .catch((err) => host.replaceChildren(el("p", { class: "st-failed" }, `${title}: ${err.message}`)));
    });
    return host;
  }

  // The canvas of a diagram, or (no graph library) its SVG in a scrolling panel.
  function diagramView(name, title, query) {
    return canvasReady() ? diagramCanvas(name, title, query) : diagramPanel(name, title, query);
  }

  // A node of an inline diagram: select it and open its code, evidence and unknowns.
  function selectDiagramNode(g) {
    for (const s of document.querySelectorAll(".diagram-svg .node.selected")) s.classList.remove("selected");
    g.classList.add("selected");
    showDetail("node", g.dataset.node);
  }

  // ------------------------------------------------------------ environment matrix
  // An HTML table (not a picture): one row per stack, one column per environment, each
  // cell the backend bucket / key; a cell opens its backend file in the detail pane.
  function envMatrixBox() {
    const box = el("div", { class: "env-matrix", id: "env-matrix" }, el("p", { class: "muted" }, "Loading the environment matrix…"));
    api("api/env-matrix").then((m) => renderEnvMatrix(box, m)).catch((err) =>
      box.replaceChildren(el("p", { class: "st-failed" }, `Environment matrix: ${err.message}`)));
    return box;
  }

  function backendSummary(cell) {
    if (!cell) return "";
    return [cell.type, cell.bucket, cell.key, cell.region].filter(Boolean).join(" ");
  }

  function renderEnvMatrix(box, m) {
    const state = { q: "", sort: "stack", dir: 1, hidden: new Set() };
    const filter = el("input", { type: "search", class: "matrix-filter", placeholder: "Filter stacks, buckets, keys…",
      "aria-label": "Filter the environment matrix" });
    const envToggles = el("div", { class: "chips" }, ...m.columns.map((c) => el("label", { class: "chip" },
      el("input", { type: "checkbox", checked: "", value: c.name, onchange: (e) => {
        if (e.target.checked) state.hidden.delete(c.name); else state.hidden.add(c.name);
        draw();
      } }), ` ${c.name}`)));
    const table = el("table", { class: "matrix" });
    const count = el("span", { class: "muted small" });
    filter.addEventListener("input", () => { state.q = filter.value.trim().toLowerCase(); draw(); });

    function sortBy(key) {
      state.dir = state.sort === key ? -state.dir : 1;
      state.sort = key;
      draw();
    }

    function header(label, key, title) {
      const arrow = state.sort === key ? (state.dir > 0 ? " ▲" : " ▼") : "";
      return el("th", { scope: "col", title: title || `Sort by ${label}` },
        el("button", { type: "button", class: "th-sort", "data-sort": key, onclick: () => sortBy(key) }, label + arrow));
    }

    function draw() {
      const cols = m.columns.filter((c) => !state.hidden.has(c.name));
      const text = (r) => [r.stack, backendSummary(r.default), ...cols.map((c) => backendSummary(r.cells[c.name]))].join(" ").toLowerCase();
      const rows = m.rows.filter((r) => !state.q || text(r).includes(state.q));
      const keyOf = (r) => (state.sort === "stack" ? r.stack : backendSummary(r.cells[state.sort]));
      // stacks without that environment always last, whatever the direction
      rows.sort((a, b) => (!keyOf(a) - !keyOf(b)) || state.dir * keyOf(a).localeCompare(keyOf(b)));
      const head = el("tr", {}, header("Stack", "stack"),
        ...cols.map((c) => header(c.name, c.name, `${c.name} — sort by its backend`)));
      if (!cols.length) head.append(el("th", {}, "Backend (no environment)"));
      const body = rows.map((r) => el("tr", { "data-stack": r.stack },
        el("th", { scope: "row", class: "matrix-stack" },
          link(r.stack, () => r.node && showDetail("node", r.node)),
          r.roots.length > 1 ? el("div", { class: "tag" }, r.roots.join(", ")) : null),
        ...(cols.length ? cols.map((c) => matrixCell(r, c.name, r.cells[c.name]))
          : [matrixCell(r, "", r.default)])));
      table.replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
      count.textContent = `${rows.length} of ${m.rows.length} stack(s) · ${cols.length} environment(s)`;
      if (!rows.length) table.append(el("caption", { class: "muted" }, "No stack matches the filter."));
    }

    if (!m.rows.length) {
      box.replaceChildren(el("p", { class: "muted" }, "No stack detected."));
      return;
    }
    box.replaceChildren(el("div", { class: "matrix-toolbar" }, filter, envToggles, count),
      el("div", { class: "matrix-scroll" }, table));
    draw();
  }

  function matrixCell(row, env, cell) {
    if (!cell) return el("td", { class: "matrix-cell empty", title: `${row.stack} has no ${env} environment` }, "—");
    const unknown = (cell.settings || []).some((s) => !s.known);
    return el("td", { class: `matrix-cell${cell.wired === false ? " not-wired" : ""}` },
      el("button", { type: "button", class: "cell-btn", "data-env": env, title: "Open the backend file",
        onclick: (e) => {
          for (const c of document.querySelectorAll(".matrix .cell-btn.selected")) c.classList.remove("selected");
          e.currentTarget.classList.add("selected");
          showBackendCell(row, env, cell);
        } },
        el("span", { class: "bk-type" }, cell.type),
        cell.bucket ? el("span", { class: "bk-bucket" }, cell.bucket) : null,
        cell.key ? el("span", { class: "bk-key" }, cell.key) : null,
        !cell.bucket && !cell.key ? el("span", { class: "muted" }, cell.origin || "no bucket / key") : null,
        unknown ? el("span", { class: "tag st-unknown" }, "some settings set at init time") : null));
  }

  async function showBackendCell(row, env, cell) {
    const panel = document.getElementById("detail");
    const parts = [el("h2", {}, env ? `${row.stack} · ${env}` : `${row.stack} · backend`),
      el("div", { class: "address mono" }, `${cell.type} backend`),
      el("dl", {},
        el("dt", {}, "Bucket"), el("dd", { class: "mono" }, cell.bucket || "—"),
        el("dt", {}, "Key"), el("dd", { class: "mono" }, cell.key || "—"),
        el("dt", {}, "Region"), el("dd", { class: "mono" }, cell.region || "—"),
        el("dt", {}, "From"), el("dd", {}, cell.origin || "—"))];
    if ((cell.settings || []).length) {
      parts.push(el("h3", {}, "Settings"), el("table", { class: "tags" }, el("tbody", {}, ...cell.settings.map((s) =>
        el("tr", { class: s.known ? "" : "st-unknown" }, el("td", { class: "mono" }, s.key),
          el("td", { class: "mono" }, s.known ? String(s.value) : "UNKNOWN"),
          el("td", {}, s.location ? locLink(s.location) : el("span", { class: "tag" }, s.reason || "")))))));
    }
    if ((cell.files || []).length || (cell.var_files || []).length) {
      parts.push(el("h3", {}, "Files"), el("p", {}, ...fileLinks([...(cell.files || []), ...(cell.var_files || [])])));
    }
    panel.replaceChildren(...parts);
    panel.scrollTop = 0;
    const loc = cell.location;
    if (!loc) return;
    try {
      const d = await api(`api/source?file=${encodeURIComponent(loc.file)}&start=${loc.start_line}&end=${loc.end_line}`);
      panel.append(el("h3", {}, "Backend file"), evidenceBlock(locText(d), d.lines));
    } catch (err) {
      panel.append(el("p", { class: "muted" }, `${locText(loc)}: ${err.message}`));
    }
  }

  // A diagram at its natural size (never below 100%) in a scrolling viewport:
  // wide diagrams scroll; only one narrower than the panel is fitted to its width.
  // Zoom is a CSS transform on .diagram-zoom; .diagram-sizer carries the scaled
  // size so the viewport's scrollbars match what is drawn.
  const ZOOM_STEP = 1.25, ZOOM_MAX = 4;

  // `inline`: the SVG is put in the page (not an <img>) so its nodes (data-node) are
  // clickable: each opens its code, evidence and unknowns in the detail pane.
  function diagramPanel(name, title, query, inline) {
    const url = `api/diagram?name=${name}&format=svg${query || ""}`;
    const img = el("img", { alt: title, title: "Open the full-size SVG in a new tab" });
    if (!inline) img.src = url;
    const zoom = el("div", { class: "diagram-zoom" },
      el("a", { href: url, target: "_blank", rel: "noopener" }, img));
    const sizer = el("div", { class: "diagram-sizer" }, zoom);
    const view = el("div", { class: "diagram", "data-diagram": name }, sizer);
    const label = el("span", { class: "diagram-scale muted" }, "100%");
    let scale = 1, pendingFit = false;
    let natural = () => [img.naturalWidth, img.naturalHeight];

    function apply(s) {
      const [w, h] = natural();
      if (!w || !h) return;
      scale = Math.min(ZOOM_MAX, Math.max(1, s));
      zoom.style.transform = `scale(${scale})`;
      sizer.style.width = `${w * scale}px`;
      sizer.style.height = `${h * scale}px`;
      label.textContent = `${Math.round(scale * 100)}%`;
    }
    // Never refit on resize: a fit changes the content size, which can toggle the
    // viewport's scrollbars and resize it again (jitter/ghosting). The viewport has
    // a fixed CSS height; fit once on load, and if the tab was hidden (zero width)
    // then, fit exactly once when it first becomes visible and stop observing.
    const shown = new ResizeObserver(() => {
      if (pendingFit && view.clientWidth) fit();
    });
    function fit() {
      const w = natural()[0], room = view.clientWidth;
      apply(w && room && w <= room ? room / w : 1);
      pendingFit = !room;
      if (room && w) shown.disconnect();
    }
    function zoomBy(f) { pendingFit = false; apply(scale * f); }

    if (inline) {
      zoom.replaceChildren(el("p", { class: "muted" }, `Loading ${title}…`));
      fetch(url)
        .then((r) => (r.ok ? r.text() : Promise.reject(new Error(r.statusText))))
        .then((text) => {
          const doc = new DOMParser().parseFromString(text, "image/svg+xml");
          if (doc.documentElement.nodeName !== "svg") throw new Error("not an SVG");
          const svgEl = document.importNode(doc.documentElement, true);
          const w = parseFloat(svgEl.getAttribute("width")), h = parseFloat(svgEl.getAttribute("height"));
          natural = () => [w, h];
          svgEl.removeAttribute("style"); // natural size: the viewport scrolls, it never shrinks
          svgEl.classList.add("diagram-svg");
          for (const g of svgEl.querySelectorAll("[data-node]")) {
            g.setAttribute("tabindex", "0");
            g.setAttribute("role", "button");
            g.addEventListener("click", () => selectDiagramNode(g));
            g.addEventListener("keydown", (e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectDiagramNode(g); }
            });
          }
          zoom.replaceChildren(svgEl);
          fit();
        })
        .catch((err) => zoom.replaceChildren(el("p", { class: "st-failed" }, `${title}: ${err.message}`)));
    } else {
      img.addEventListener("load", fit);
    }
    shown.observe(view);

    const bar = el("div", { class: "diagram-toolbar" },
      el("button", { type: "button", "data-zoom": "in", title: "Zoom in", onclick: () => zoomBy(ZOOM_STEP) }, "+"),
      el("button", { type: "button", "data-zoom": "out", title: "Zoom out (not below 100%)", onclick: () => zoomBy(1 / ZOOM_STEP) }, "−"),
      el("button", { type: "button", "data-zoom": "fit", title: "Reset: natural size, or fit the width if it is narrower than the panel", onclick: fit }, "Fit"),
      label,
      el("a", { class: "link", href: url, target: "_blank", rel: "noopener" }, "Open full size ↗"));
    return el("div", { class: "diagram-panel" }, bar, view);
  }

  // ------------------------------------------------------------ boot
  async function loadAll() {
    const [summary, graph, tree, ov, fl, shapes, envs, cards, icons] = await Promise.all(
      [api("api/summary"), api("api/graph"), api("api/tree"), api("api/overview"), api("api/flow"),
        api("api/shapes"), api("api/environments"),
        api("api/stack-cards").catch(() => []), api("api/icons").catch(() => null)]);
    // one set of service icons and colours for the Graph, every diagram and the tables
    iconsData = icons;
    if (window.TFDiagram && icons) window.TFDiagram.setIcons(icons.icons);
    renderEnvSwitch(envs);
    renderEnvironments(envs);
    renderOverview(ov, envs);
    renderFlow(fl, ov);
    cardsData = cards;
    renderRoots(shapes);
    renderFailure(summary, ov);
    document.getElementById("title").textContent = summary.title;
    // a recorded run (managed mode) is read-only: no Discover
    document.getElementById("discover-form").classList.toggle("hidden", Boolean(summary.readonly));
    renderDiagrams();
    renderSummaryBar(summary, envs);
    document.getElementById("tree").replaceChildren(treeNode(tree));
    buildGraph(graph);
  }

  // ------------------------------------------------------------ header summary
  // Four numbers in the header (environments, stacks, resources, unknowns); a click
  // expands everything else (flows, files, modules, nodes, edges, parse errors).
  function renderSummaryBar(summary, envs) {
    const s = summary.summary;
    const nodes = Object.values(s.nodes).reduce((a, b) => a + b, 0);
    const f = summary.flow;
    // the same numbers as the tables (envs.scope_counts)
    const { environments: envCount, stacks } = envs.counts;
    const resources = s.nodes.resource || 0;
    const stat = (n, label, cls) => el("span", { class: `sum-stat ${cls || ""}`.trim() }, el("b", {}, String(n)), ` ${label}`);
    const bar = document.getElementById("summary");
    const panel = document.getElementById("summary-details");
    const toggle = el("button", { type: "button", class: "summary-toggle", "aria-expanded": String(!panel.hidden),
      "aria-controls": "summary-details", title: "Show all the numbers" },
    stat(envCount, envCount === 1 ? "env" : "envs"), stat(stacks, stacks === 1 ? "stack" : "stacks"),
    stat(resources, "resources"),
    el("span", { class: "caret", "aria-hidden": "true" }, "▾"));
    toggle.addEventListener("click", () => {
      const open = panel.hidden;
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
    });
    // not a bare badge: it opens the panel that lists them, grouped by why
    const unknowns = el("button", { type: "button", class: "summary-unknowns", title: "Open the Unknowns panel: grouped by why, with file:line",
      onclick: () => {
        setView("environments");
        const target = document.getElementById("env-unknowns");
        if (target) target.scrollIntoView();
      } }, stat(s.unknowns, "unknowns", s.unknowns ? "warn" : ""));
    bar.replaceChildren(toggle, unknowns);
    const item = (label, value, cls) => [el("dt", {}, label), el("dd", { class: cls || "" }, value)];
    const edges = Object.entries(s.edges).map(([k, v]) => `${k} ${v}`).join(", ") || "none";
    panel.replaceChildren(el("dl", { class: "summary-grid" },
      ...(envs.environments.length ? [
        ...item("Environment shown", `${(envs.selected || []).join(", ") || envs.active || "none (variable defaults)"}`),
        ...item("Counts shown", envs.current.text),
        ...item("Total (all environments)", envs.total ? envs.total.text : "—"),
      ] : []),
      ...item("Flows", `${f.flows} flows · ${f.steps} steps`),
      ...item("Unverified links", String(f.unverified), f.unverified ? "st-unknown" : ""),
      ...item("Files", String(s.files)),
      ...item("Modules", String(s.modules)),
      ...item("Nodes", String(nodes)),
      ...item("Edges", edges),
      ...item("Unknowns", String(s.unknowns), s.unknowns ? "st-unknown" : ""),
      ...item("Parse errors", String(s.parse_errors), s.parse_errors ? "st-failed" : "")));
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const t of document.querySelectorAll(".tab[data-view]")) t.addEventListener("click", () => setView(t.dataset.view));
    document.getElementById("search").addEventListener("input", applyFilters);
    document.getElementById("hide-isolated").addEventListener("change", applyFilters);
    document.addEventListener("click", closeCommandPopovers);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closeCommandPopovers();
        clearFocus();
        const panel = document.getElementById("summary-details");
        if (panel && !panel.hidden) {
          panel.hidden = true;
          const t = document.querySelector(".summary-toggle");
          if (t) t.setAttribute("aria-expanded", "false");
        }
      }
    });
    const envSelect = document.getElementById("env");
    envSelect.addEventListener("change", () => selectEnv(envSelect.value));
    loadAll().then(() => {
      // a run tab linked from another page of the run (Changes): ?view=graph
      const view = new URLSearchParams(location.search).get("view");
      if (VIEWS.includes(view)) setView(view);
    }).catch((err) => {
      document.getElementById("detail").replaceChildren(el("p", {}, `Failed to load: ${err.message}`));
    });
  });
})();
