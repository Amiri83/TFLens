/* TFLens web UI: root view (the landing view: each root in the view its shape calls
   for), flow, overview, graph (Cytoscape, vendored),
   diagrams, repository tree, detail panel.
   All data comes from the local api/* endpoints next to this page (/api/* for
   `tflens serve --dir`, /runs/<id>/api/* for a recorded run); no external requests. */
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
  }

  // ------------------------------------------------------------ file:line references
  function locLink(loc) {
    if (!loc) return null;
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
  // One row per detected environment (its tfvars applied) and a total row; the shown
  // environment is highlighted, its remaining unknowns listed with the reason.
  function envCountsTable(envs) {
    const types = envs.types || [];
    const head = el("tr", {}, el("th", {}, "Environment"),
      ...types.map((t) => el("th", {}, t.label)), el("th", {}, "Unknowns"), el("th", {}, "Files"));
    const rows = envs.environments.map((r) => {
      const files = [...r.backend_hcl, ...r.var_files];
      return el("tr", { class: r.id === envs.active ? "env-row active" : "env-row", "data-env": r.id },
        el("td", {}, r.id),
        ...types.map((t) => el("td", { class: r.uncertain[t.type] ? "num st-unknown" : "num",
          title: r.uncertain[t.type] ? "blocks whose count is not known statically are not counted" : "" },
          countText(r.counts[t.type] || 0, r.uncertain[t.type] || 0))),
        el("td", { class: r.unknown_count ? "num st-unknown" : "num" }, String(r.unknown_count)),
        el("td", {}, ...files.flatMap((f, i) => [i ? ", " : "",
          locLink({ file: f, start_line: 1, end_line: 1 })])));
    });
    const t = envs.total;
    const total = el("tr", { class: "env-total" }, el("td", {}, el("strong", {}, "Total")),
      ...types.map((x) => el("td", { class: "num" },
        el("strong", {}, countText(t.counts[x.type] || 0, (t.uncertain || {})[x.type] || 0)))),
      el("td", { class: "num" }, el("strong", {}, String(t.unknown_count))), el("td", {}, ""));
    return el("table", { class: "env-counts" }, el("thead", {}, head), el("tbody", {}, ...rows, total));
  }

  function envUnknowns(envs) {
    const row = envs.environments.find((r) => r.id === envs.active);
    const shown = row || envs.current;
    const title = row ? `Unknowns in ${row.id} after applying its variables` : "Unknowns (no environment applied)";
    if (!shown.unknown_count) return [el("p", { class: "muted" }, `${title}: none.`)];
    const items = shown.unknowns.map((u) => el("li", { class: "ov-line st-unknown" },
      `${u.subject}.${u.attribute} — ${u.category}: ${u.reason} `, locLink(u.location)));
    if (shown.unknown_count > shown.unknowns.length) {
      items.push(el("li", { class: "ov-line" }, `… ${shown.unknown_count - shown.unknowns.length} more`));
    }
    return [el("h3", {}, `${title} (${shown.unknown_count})`), el("ul", { class: "overview-lines" }, ...items)];
  }

  // "4", or "4 (+3 uncertain)": a block whose count is not known is never counted as 1
  function countText(known, uncertain) {
    return uncertain ? `${known} (+${uncertain} uncertain)` : String(known);
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

  function envFilesTable(envs) {
    const head = el("tr", {}, el("th", {}, "Environment"), el("th", {}, "Wired"),
      el("th", {}, "tfvars"), el("th", {}, "Backend config"), el("th", {}, "CI / script lines"));
    const rows = envs.environments.map((r) => {
      const ci = r.var_file_cli.map((u) => el("div", {}, `${u.flag}=${u.value} `, locLink(u.location)));
      const wired = r.wired
        ? el("td", { title: r.wiring.join("; ") }, "yes")
        : el("td", { class: "st-unknown", title: r.not_wired_reason || "" }, "not wired");
      return el("tr", { class: `env-row${r.id === envs.active ? " active" : ""}${r.wired ? "" : " not-wired"}`, "data-env": r.id },
        el("td", {}, envButton(envs, r.id)), wired,
        el("td", {}, ...fileLinks(r.var_files)), el("td", {}, ...fileLinks(r.backend_hcl)),
        el("td", {}, ...(ci.length ? ci : [el("span", { class: "muted" }, "—")])));
    });
    return el("table", { class: "env-files" }, el("thead", {}, head), el("tbody", {}, ...rows));
  }

  // An environment's name; a button applying it when the analysis can switch.
  function envButton(envs, id) {
    const label = id === envs.active ? `${id} (shown)` : id;
    if (!envs.switchable || id === envs.active) return el("strong", {}, label);
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
      return el("tr", { class: r.id === envs.active ? "env-row active" : "env-row" },
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
    const parts = [el("h2", {}, `${title}: ${countText(cell.count, cell.uncertain)}`)];
    if (cell.uncertain) {
      parts.push(el("p", { class: "st-unknown" },
        `${cell.uncertain} block(s) with a count not known statically: not counted (never assumed to be 1); see why below.`));
    }
    if (!cell.items.length) parts.push(el("p", { class: "muted" }, "None created."));
    for (const it of cell.items) {
      const only = row.only[it.id];
      parts.push(el("div", { class: `inv-item${it.created === null ? " uncertain" : ""}` },
        el("strong", {}, it.name), " ", link(it.id, () => showDetail("node", it.id)), " ", locLink(it.location),
        el("div", { class: "tag" }, it.created === null ? "count not known" : `${it.created} instance${it.created === 1 ? "" : "s"}`,
          only ? ` · only in ${only.join(", ")}` : ""),
        it.why ? el("div", {}, el("div", { class: "tag" }, "Why counted:"), whySteps(it.why)) : null));
    }
    panel.replaceChildren(...parts);
    panel.scrollTop = 0;
  }

  function inventoryGrid(envs) {
    const perEnv = envs.inventory_columns === "environments";
    const cols = perEnv ? envs.environments.map((r) => [r.id, r.inventory, r.wired]) : [["", envs.current.inventory, true]];
    const head = el("tr", {}, el("th", {}, "Service"),
      ...cols.map(([id, , wired]) => el("th", { class: wired ? "num" : "num st-unknown", title: wired ? "" : "not wired" },
        id || (envs.active ? envs.active : "shown (variable defaults)"))));
    const rows = envs.inventory.map((row) => {
      const values = cols.map(([, inv]) => countText(inv[row.key].count, inv[row.key].uncertain));
      const common = mostCommon(values);
      return el("tr", { class: row.differs ? "inv-row differs" : "inv-row", "data-row": row.key },
        el("td", {}, row.label, row.differs ? el("span", { class: "tag st-unknown" }, " differs") : null),
        ...cols.map(([id, inv], i) => {
          const cell = inv[row.key];
          const cls = ["num", "inv-cell"];
          if (row.differs && values[i] !== common) cls.push("diff");
          if (cell.uncertain) cls.push("st-unknown");
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
      parts.push(el("h2", { id, class: "rv-section" }, title), ...content);
    };
    if (!envs.environments.length) {
      parts.push(el("p", { class: "muted" },
        "No environment detected (no env/*.tfvars, backend .hcl per environment, env directories or CI -var-file / -backend-config lines): the inventory below is the code with the variables' defaults."));
    } else {
      const shown = envs.active ? `${envs.active}${envs.default ? " (default)" : ""}` : "no environment (variable defaults)";
      parts.push(el("p", {}, `Shown: ${shown}. The flows, the Root view (Network, IAM) and the graph follow the environment selected here or in the switcher above.`));
      if (envs.not_wired.length) {
        parts.push(el("p", { class: "st-unknown" },
          `Not wired: ${envs.not_wired.join(", ")} — tfvars only (no backend configuration, no CI / script line uses it); still analyzed.`));
      }
      section("env-files", `Detected environments (${envs.environments.length})`, envFilesTable(envs));
      section("env-state", "State file per environment", stateTable(envs));
    }
    section("env-inventory", "Service inventory",
      el("p", { class: "muted" }, "Instances each environment creates with its own tfvars. Click a count for the names, file:line and why it is counted so; \"(+N uncertain)\": blocks whose count is not known statically (not counted)."),
      inventoryGrid(envs));
    if (envs.environments.length) {
      section("env-diffs", "Differences between environments",
        envs.differences.length
          ? el("ul", { class: "overview-lines" }, ...envs.differences.map((d) =>
            el("li", { class: d.startsWith(" ") ? "ov-line lvl-1" : "ov-line" }, d.trim())))
          : el("p", { class: "muted" }, "None: every environment creates the same services."));
    }
    const shownRow = envs.environments.find((r) => r.id === envs.active) || envs.current;
    if (shownRow.uncertain_items && shownRow.uncertain_items.length) {
      section("env-uncertain", "Uncertain counts", ...shownRow.uncertain_items.map((it) =>
        el("div", { class: "inv-item uncertain" }, link(it.id, () => showDetail("node", it.id)), " ", locLink(it.location),
          it.why ? whySteps(it.why) : el("div", { class: "tag" }, "count not known"))));
    }
    toc.push(["env-unknowns", "Unknowns"]);
    parts.push(el("div", { id: "env-unknowns" }, ...envUnknowns(envs)));
    box.replaceChildren(...parts);
    document.getElementById("env-toc").replaceChildren(...tocLinks(toc));
  }

  async function selectEnv(name) {
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
    box.classList.toggle("hidden", !envs.environments.length);
    select.replaceChildren(el("option", { value: "" }, "no environment (variable defaults)"),
      ...envs.environments.map((r) => el("option", { value: r.id },
        `${r.id}${envs.default && r.id === envs.active ? " (default)" : ""} — ${r.text}`)));
    select.value = envs.active || "";
    select.disabled = !envs.switchable;
  }

  function renderOverview(ov, envs) {
    const box = document.getElementById("overview");
    const toc = [];
    const parts = [];
    if (ov.purpose && ov.purpose.length) {
      toc.push(["ov-purpose", "Purpose (guess)"]);
      parts.push(el("h2", { id: "ov-purpose" }, "Purpose (guess)"), overviewList(ov.purpose));
    }
    parts.push(el("h2", { id: "ov-top" }, "What this repository deploys"), overviewList(ov.summary));
    toc.push(["ov-top", "What it deploys"]);
    if (envs && envs.environments.length) {
      toc.push(["ov-env-counts", "Counts per environment"]);
      parts.push(el("h2", { id: "ov-env-counts" }, "Counts per environment"),
        el("p", { class: "muted" }, "Each environment analyzed with its own tfvars: only the blocks its count / for_each / conditionals create are counted."),
        envCountsTable(envs), ...envUnknowns(envs));
    }
    if (ov.inventory) {
      parts.push(el("h3", {}, "What is in the repository"), inventoryTable(ov.inventory));
    }
    ov.roots.forEach((r, i) => {
      const id = `ov-root-${i}`;
      toc.push([id, r.title]);
      parts.push(el("h2", { id, class: "ov-root" }, `Root ${r.title}`, " ", locLink(r.location)));
      if (!r.analyzed) parts.push(el("p", { class: "unknown" }, `Not analyzed: ${r.status}`));
      for (const s of r.sections) {
        parts.push(el("h3", {}, s.title), overviewList(s.lines));
      }
    });
    if (ov.resources && ov.resources.length) {
      toc.push(["ov-res", "All resources"]);
      parts.push(allResources(ov, "all-resources-ov", "ov-res", false));
    }
    if (ov.environments.length) {
      toc.push(["ov-envs", "Environments"]);
      parts.push(el("h2", { id: "ov-envs" }, "Environments"), overviewList(ov.environments));
    }
    if (ov.docs && ov.docs.length) {
      toc.push(["ov-docs", "Documentation"]);
      parts.push(el("h2", { id: "ov-docs" }, "Documentation"), overviewList(ov.docs));
    }
    if (ov.ci && ov.ci.length) {
      toc.push(["ov-ci", "CI / CD and lock files"]);
      parts.push(el("h2", { id: "ov-ci" }, "CI / CD and lock files"), overviewList(ov.ci));
    }
    if (ov.history) {
      toc.push(["ov-hist", "History"]);
      parts.push(el("h2", { id: "ov-hist" }, "History"), overviewList(ov.history.summary));
      if (ov.history.hot_files.length) {
        parts.push(el("h3", {}, "Most-changed files"), overviewList(ov.history.hot_files));
      }
      if (ov.history.commits.length) {
        parts.push(el("h3", {}, "Recent commits"), overviewList(ov.history.commits));
      }
    }
    toc.push(["ov-disc", "Discovery"]);
    parts.push(el("h2", { id: "ov-disc" }, "Discovery"), overviewList(ov.discovery));
    box.replaceChildren(...parts);
    document.getElementById("toc").replaceChildren(...toc.map(([id, label]) =>
      el("a", { class: "file link", onclick: () => document.getElementById(id).scrollIntoView() }, label)));
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
      diagramPanel(c.id, `Flow ${c.n}`, diagramQuery),
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
      el("a", { class: "file link", onclick: () => document.getElementById(tid).scrollIntoView() }, label));
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
    const toc = [["rv-shapes", "Roots by shape"], ["rv-root", `Root ${s.title}`]];
    const parts = [shapesSummary(shapesData),
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
          diagramPanel(d.diagram, `${d.view_label} diagram`, q));
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
    const parts = [el("h2", {}, d.title)];
    const s = d.summary;
    if (type === "node") {
      parts.push(el("dl", {},
        el("dt", {}, "Name"), el("dd", {}, s.short_name),
        el("dt", {}, "Id"), el("dd", {}, d.id),
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
    const members = cy.nodes().not(".group");
    const groups = cy.nodes(".group");
    cy.batch(() => {
      members.forEach((n) => {
        const service = n.data("service");
        const ok = (service === "meta" ? showMeta : services.has(service)) && modules.has(n.data("module"));
        n.style("display", ok ? "element" : "none");
      });
      cy.edges().forEach((e) => {
        const ok = kinds.has(e.data("kind")) && visible(e.source()) && visible(e.target());
        e.style("display", ok ? "element" : "none");
      });
      if (hideIsolated) {
        members.forEach((n) => {
          if (visible(n) && !n.connectedEdges().filter(visible).length) n.style("display", "none");
        });
      }
      // a module group stays visible while any of its members is (deepest groups first)
      groups.sort((a, b) => b.ancestors().length - a.ancestors().length).forEach((g) => {
        g.style("display", g.children().filter(visible).length ? "element" : "none");
      });
      cy.elements().removeClass("faded match");
      if (q) {
        const hits = members.filter((n) =>
          visible(n) && (n.id().toLowerCase().includes(q) || String(n.data("name")).toLowerCase().includes(q)));
        cy.elements().addClass("faded");
        hits.removeClass("faded").addClass("match");
        hits.ancestors().removeClass("faded");
        hits.connectedEdges().removeClass("faded");
      }
    });
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

  function tooltip() {
    let tip = document.getElementById("tooltip");
    if (!tip) {
      tip = el("div", { id: "tooltip", class: "hidden" });
      document.getElementById("center").append(tip);
    }
    return tip;
  }

  function showTooltip(evt) {
    const t = evt.target;
    const tip = tooltip();
    if (t.isEdge()) {
      tip.replaceChildren(el("strong", {}, t.data("kind")), " ", t.data("label"),
        el("div", { class: "tag" }, `${t.source().id()} → ${t.target().id()}`));
    } else if (t.hasClass("group")) {
      tip.replaceChildren(el("strong", {}, t.data("module")),
        el("div", { class: "tag" }, `${t.descendants().not(".group").length} nodes`));
    } else {
      tip.replaceChildren(el("strong", {}, t.id()),
        el("div", { class: "tag" }, `${t.data("type")} · ${t.data("service")} · module ${t.data("module")}`));
    }
    const pos = evt.renderedPosition || t.renderedPosition();
    tip.style.left = `${pos.x + 12}px`;
    tip.style.top = `${pos.y + 12}px`;
    tip.classList.remove("hidden");
  }

  function hideTooltip() {
    tooltip().classList.add("hidden");
  }

  function elements(data, grouped) {
    // fresh copies: the graph is rebuilt from the same data when grouping is toggled
    const nodes = data.nodes.map((n) => ({ data: { ...n.data, parent: grouped ? n.data.group : null } }));
    const groups = data.groups.map((g) => ({ data: { ...g.data }, classes: g.classes }));
    return { nodes: grouped ? groups.concat(nodes) : nodes, edges: data.edges.map((e) => ({ data: { ...e.data } })) };
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
    hideTooltip();
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
      elements: elements(data, document.getElementById("group-modules").checked),
      wheelSensitivity: 0.3,
      style: [
        { selector: "node", style: {
          "background-color": "data(color)", label: "data(label)", "font-size": 9,
          "text-valign": "bottom", "text-margin-y": 3, width: 18, height: 18,
          "text-wrap": "ellipsis", "text-max-width": 120 } },
        { selector: 'node[kind = "external"]', style: { shape: "diamond" } },
        { selector: 'node[kind = "data"]', style: { shape: "round-rectangle" } },
        { selector: 'node[category = "relationship"], node[category = "policy"]', style: { width: 12, height: 12 } },
        { selector: "node.group", style: {
          shape: "round-rectangle", "background-color": "#eaeef2", "background-opacity": 0.5,
          "border-width": 1, "border-color": "#8c959f", "border-style": "dashed",
          label: "data(label)", "font-size": 10, "font-weight": 600, color: "#57606a",
          "text-valign": "top", "text-halign": "center", "text-max-width": 400, padding: 14 } },
        { selector: "edge", style: {
          "line-color": "data(color)", "target-arrow-color": "data(color)",
          "target-arrow-shape": "triangle", "curve-style": "bezier", "control-point-step-size": 28,
          width: 1.5, "arrow-scale": 0.9, "font-size": 8, color: "#1f2328",
          "text-background-color": "#ffffff", "text-background-opacity": 0.85,
          "text-background-padding": 1, "text-rotation": "autorotate" } },
        { selector: 'edge[kind = "CONFIG"]', style: { "line-style": "dashed", width: 1 } },
        { selector: 'edge[kind = "RUNS_ON"]', style: { "line-style": "dotted" } },
        { selector: "edge.hover", style: { label: "data(short_label)", width: 2.5, "z-index": 10 } },
        { selector: "node:selected", style: { "border-width": 3, "border-color": "#0969da" } },
        { selector: "edge:selected", style: {
          "line-color": "#0969da", "target-arrow-color": "#0969da", width: 3, label: "data(short_label)" } },
        { selector: ".faded", style: { opacity: 0.15 } },
        { selector: ".match", style: { "border-width": 3, "border-color": "#bf8700" } },
        { selector: ".dimmed", style: { opacity: 0.12 } },
      ],
      layout: { name: "grid" },
    });
    cy.on("tap", (evt) => {
      const t = evt.target;
      if (t === cy) clearFocus();
      else if (t.hasClass("group")) showDetail("node", t.data("module"));
      else showDetail(t.isEdge() ? "edge" : "node", t.id());
    });
    cy.on("mouseover", "node, edge", (evt) => {
      if (evt.target.isEdge()) evt.target.addClass("hover");
      showTooltip(evt);
    });
    cy.on("mouseout", "node, edge", (evt) => {
      evt.target.removeClass("hover");
      hideTooltip();
    });
    cy.on("pan zoom", hideTooltip);
    applyFilters();
    graphFitted = !document.getElementById("graph").classList.contains("hidden");
    if (graphFitted) layoutGraph();
  }

  function layoutGraph() {
    if (!cy) return;
    cy.layout({ name: "cose", animate: false, randomize: false, nodeDimensionsIncludeLabels: true,
      eles: cy.elements(":visible") }).run();
    cy.fit(cy.elements(":visible"), 30);
  }

  // ------------------------------------------------------------ repository
  function treeNode(node) {
    if (node.type === "file") {
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

  // ------------------------------------------------------------ diagrams
  const DIAGRAMS = [
    ["architecture", "Architecture (flows)", "Services grouped by root and module; TRIGGER / ACCESS / NETWORK flows only."],
    ["module-tree", "Module tree", "Each root and the modules it calls, with source and version."],
    ["env-matrix", "Environment matrix", "Root × environment, with each root's backend."],
  ];

  function renderDiagrams() {
    const box = document.getElementById("diagrams");
    const parts = [el("p", {},
      el("a", { class: "link", href: "api/handoff?download=1", download: "handoff.md" }, "Download handoff.md"),
      " — the overview and these diagrams (Mermaid) in one file, for Claude Code.")];
    for (const [name, title, note] of DIAGRAMS) {
      parts.push(el("h2", { id: `dg-${name}` }, title),
        el("p", { class: "muted" }, note, " ",
          el("a", { class: "link", href: `api/diagram?name=${name}&format=mmd&download=1`, download: `${name}.mmd` }, "Mermaid (.mmd)"),
          " · ",
          el("a", { class: "link", href: `api/diagram?name=${name}&format=svg&download=1`, download: `${name}.svg` }, "SVG")),
        diagramPanel(name, title));
    }
    box.replaceChildren(...parts);
  }

  // A diagram at its natural size (never below 100%) in a scrolling viewport:
  // wide diagrams scroll; only one narrower than the panel is fitted to its width.
  // Zoom is a CSS transform on .diagram-zoom; .diagram-sizer carries the scaled
  // size so the viewport's scrollbars match what is drawn.
  const ZOOM_STEP = 1.25, ZOOM_MAX = 4;

  function diagramPanel(name, title, query) {
    const url = `api/diagram?name=${name}&format=svg${query || ""}`;
    const img = el("img", { src: url, alt: title, title: "Open the full-size SVG in a new tab" });
    const zoom = el("div", { class: "diagram-zoom" },
      el("a", { href: url, target: "_blank", rel: "noopener" }, img));
    const sizer = el("div", { class: "diagram-sizer" }, zoom);
    const view = el("div", { class: "diagram", "data-diagram": name }, sizer);
    const label = el("span", { class: "diagram-scale muted" }, "100%");
    let scale = 1, pendingFit = false;

    function apply(s) {
      const w = img.naturalWidth, h = img.naturalHeight;
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
      const w = img.naturalWidth, room = view.clientWidth;
      apply(w && room && w <= room ? room / w : 1);
      pendingFit = !room;
      if (room && w) shown.disconnect();
    }
    function zoomBy(f) { pendingFit = false; apply(scale * f); }

    img.addEventListener("load", fit);
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
    const [summary, graph, tree, ov, fl, shapes, envs] = await Promise.all(
      [api("api/summary"), api("api/graph"), api("api/tree"), api("api/overview"), api("api/flow"),
        api("api/shapes"), api("api/environments")]);
    renderEnvSwitch(envs);
    renderEnvironments(envs);
    renderOverview(ov, envs);
    renderFlow(fl, ov);
    renderRoots(shapes);
    renderFailure(summary, ov);
    document.getElementById("title").textContent = summary.title;
    // a recorded run (managed mode) is read-only: no Regenerate, a way back to the runs
    document.getElementById("regenerate").classList.toggle("hidden", Boolean(summary.readonly));
    document.getElementById("home").classList.toggle("hidden", !location.pathname.startsWith("/runs/"));
    // a recorded run: diff against the previous run (managed mode only)
    document.getElementById("changes").classList.toggle("hidden", !location.pathname.startsWith("/runs/"));
    renderDiagrams();
    const s = summary.summary;
    const nodes = Object.values(s.nodes).reduce((a, b) => a + b, 0);
    const edges = Object.entries(s.edges).map(([k, v]) => `${k} ${v}`).join(", ") || "none";
    const f = summary.flow;
    const shownEnv = envs.environments.length
      ? `${envs.active || "no environment"}: ${envs.current.text} · total ${envs.total.text} · `
      : "";
    document.getElementById("summary").textContent = shownEnv +
      `${f.flows} flows · ${f.steps} steps · ${f.unverified} unverified links · ` +
      `${s.files} files · ${s.modules} modules · ${nodes} nodes · edges: ${edges} · ` +
      `${s.unknowns} unknowns · ${s.parse_errors} parse errors`;
    document.getElementById("tree").replaceChildren(treeNode(tree));
    buildGraph(graph);
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => setView(t.dataset.view));
    document.getElementById("search").addEventListener("input", applyFilters);
    document.getElementById("hide-isolated").addEventListener("change", applyFilters);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        clearFocus();
        hideTooltip();
      }
    });
    const regen = document.getElementById("regenerate");
    regen.addEventListener("click", async () => {
      regen.disabled = true;
      try {
        await api("api/regenerate", { method: "POST", headers: { "X-TFLens": "1" } });
        await loadAll();
      } finally {
        regen.disabled = false;
      }
    });
    const envSelect = document.getElementById("env");
    envSelect.addEventListener("change", () => selectEnv(envSelect.value));
    loadAll().catch((err) => {
      document.getElementById("detail").replaceChildren(el("p", {}, `Failed to load: ${err.message}`));
    });
  });
})();
