"""Diagrams of a model, as Mermaid (``.mmd``) and standalone SVG, deterministic and offline.

* ``architecture``: the services, grouped by root and module, and only the flows between
  them (TRIGGER, ACCESS, NETWORK), laid out trigger -> compute -> data, with AWS-style
  icons (:mod:`tflens.icons`).
* ``module-tree``: each root and the modules it calls, with source kind and version.
* ``env-matrix``: root x environment, with each root's backend.

The SVG layout is computed here (no browser, no renderer): the same model always gives
byte-identical files. A PNG is written only when an offline SVG renderer is installed
(``rsvg-convert``, ``cairosvg`` or ``inkscape``); otherwise the reason is reported.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .icons import colour, icon_for_type, symbol
from .overview import FRIENDLY, SUPPORTING

DIAGRAMS = ("architecture", "module-tree", "env-matrix")
TITLES = {
    "architecture": "Architecture (flows)",
    "module-tree": "Module tree",
    "env-matrix": "Environment matrix",
}
FLOW_KINDS = ("TRIGGER", "ACCESS", "NETWORK")
KIND_COLOURS = {"TRIGGER": "#ff7f0e", "ACCESS": "#d62728", "NETWORK": "#1f77b4"}
TIERS = ("trigger", "compute", "data")
TIER_TITLES = {"trigger": "Triggers / entry points", "compute": "Compute", "data": "Data / targets"}
COMPUTE_TYPES = frozenset(
    {
        "aws_lambda_function",
        "aws_instance",
        "aws_autoscaling_group",
        "aws_ecs_service",
        "aws_ecs_cluster",
        "aws_eks_cluster",
        "aws_sfn_state_machine",
    }
)
FONT = "Helvetica, Arial, sans-serif"
CHAR_W = 6.6  # average glyph width at 11px, for layout only
MAX_LABEL = 30
LABEL_H = 12  # line height of a 9px edge label: stacked labels never overlap

_MERMAID_UNSAFE = re.compile(r'["\[\]{}()<>|#;`]')


# ------------------------------------------------------------------ shared helpers


def _cut(text: str, n: int = MAX_LABEL) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _text_w(text: str, size: int = 11) -> float:
    """Estimated rendered width of a text, for layout only."""
    return len(text) * CHAR_W * size / 11


def _mlabel(text: str) -> str:
    """Text safe inside a quoted Mermaid label / edge label."""
    return _MERMAID_UNSAFE.sub(" ", text).replace("  ", " ").strip() or "-"


def _x(text: str) -> str:
    return escape(text, {'"': "&quot;"})


def _label(n: dict[str, Any]) -> str:
    props = n["properties"]
    for key in ("function_name", "bucket", "name"):
        if isinstance(props.get(key), str) and props[key]:
            return props[key]
    return n["name"]


def _root_of(node_id: str, roots: list[str]) -> str:
    if "::" in node_id:
        return node_id.split("::", 1)[0]
    return roots[0] if len(roots) == 1 else "."


def _module_path(n: dict[str, Any]) -> str:
    """Module address inside its root ('' for the root module itself)."""
    mod = n["module"] or ""
    return mod.split("::", 1)[1] if "::" in mod else mod


def _svg(width: float, height: float, title: str, body: list[str], icons: set[str]) -> str:
    defs = "".join(symbol(i) for i in sorted(icons))
    markers = "".join(
        f'<marker id="arrow-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        f'fill="{c}"/></marker>'
        for k, c in sorted(KIND_COLOURS.items())
    )
    w, h = round(width), round(height)
    # the viewBox lets the drawing scale down to its container instead of being clipped;
    # width/height keep the natural size for standalone viewers and PNG renderers
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" preserveAspectRatio="xMinYMin meet" '
        f'style="max-width:100%;height:auto" font-family="{FONT}" font-size="11">\n'
        f"<title>{_x(title)}</title>\n<defs>{defs}{markers}</defs>\n"
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>\n'
        + "\n".join(body)
        + "\n</svg>\n"
    )


def _text(
    x: float,
    y: float,
    text: str,
    size: int = 11,
    weight: str = "",
    fill: str = "#24292f",
    anchor: str = "",
) -> str:
    attrs = f'x="{x:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}"'
    if weight:
        attrs += f' font-weight="{weight}"'
    if anchor:
        attrs += f' text-anchor="{anchor}"'
    return f"<text {attrs}>{_x(text)}</text>"


def _icon(name: str, x: float, y: float, size: int = 32) -> str:
    return (
        f'<use xlink:href="#icon-{name}" x="{x:.0f}" y="{y:.0f}" width="{size}" height="{size}"/>'
    )


# ------------------------------------------------------------------ architecture


def architecture_graph(model: dict[str, Any]) -> dict[str, Any]:
    """Nodes (recognized services, not supporting resources) and the flows between them."""
    roots = list(model.get("roots") or [])
    nodes = {
        n["id"]: n
        for n in model["nodes"]
        if n["kind"] == "resource" and n["type"] in FRIENDLY and n["type"] not in SUPPORTING
    }
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in model["edges"]:
        if e["kind"] not in FLOW_KINDS or e["source"] not in nodes or e["target"] not in nodes:
            continue
        key = (e["kind"], e["source"], e["target"])
        if key in edges:
            continue
        actions = e["attributes"].get("actions")
        label = (
            f"{len(actions)} action{'s' if len(actions) != 1 else ''}"
            if isinstance(actions, list)
            else e["label"]
        )
        edges[key] = {
            "kind": e["kind"],
            "source": e["source"],
            "target": e["target"],
            "label": _cut(label, 28),
        }
    triggers = {e["source"] for e in edges.values() if e["kind"] == "TRIGGER"}
    out_nodes = []
    for nid in sorted(nodes):
        n = nodes[nid]
        tier = (
            "compute" if n["type"] in COMPUTE_TYPES else ("trigger" if nid in triggers else "data")
        )
        out_nodes.append(
            {
                "id": nid,
                "label": _label(n),
                "type": n["type"],
                "kind_label": FRIENDLY[n["type"]][0],
                "icon": icon_for_type(n["type"]),
                "root": _root_of(nid, roots),
                "module": _module_path(n),
                "tier": tier,
            }
        )
    return {"nodes": out_nodes, "edges": [edges[k] for k in sorted(edges)]}


def _groups(nodes: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """root -> module path -> nodes (sorted)."""
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for n in nodes:
        out.setdefault(n["root"], {}).setdefault(n["module"], []).append(n)
    return {r: {m: out[r][m] for m in sorted(out[r])} for r in sorted(out)}


def architecture_mermaid(model: dict[str, Any]) -> str:
    g = architecture_graph(model)
    ids = {n["id"]: f"N{i}" for i, n in enumerate(g["nodes"])}
    lines = ["flowchart LR"]
    for tier, fill in (("trigger", "#fde2ef"), ("compute", "#fdebd3"), ("data", "#eef5e1")):
        lines.append(f"  classDef {tier} fill:{fill},stroke:#545b64,color:#24292f")
    if not g["nodes"]:
        lines.append('  empty["No recognized services in the analyzed code"]')
    for ri, (root, mods) in enumerate(_groups(g["nodes"]).items()):
        lines.append(f'  subgraph R{ri}["{_mlabel(root + "/" if root != "." else "./ root")}"]')
        lines.append("    direction LR")
        open_mods: list[str] = []
        sub = 0
        for mod, nodes in mods.items():
            parts = mod.split(".") if mod else []
            # module.a.module.b -> ["module.a", "module.a.module.b"]
            paths = [".".join(parts[: i + 2]) for i in range(0, len(parts), 2)]
            while open_mods and (
                len(open_mods) > len(paths) or open_mods[-1] != paths[len(open_mods) - 1]
            ):
                open_mods.pop()
                lines.append("    " + "  " * len(open_mods) + "end")
            for p in paths[len(open_mods) :]:
                label = ".".join(p.split(".")[-2:])
                indent = "    " + "  " * len(open_mods)
                lines.append(f'{indent}subgraph R{ri}M{sub}["{_mlabel(label)}"]')
                sub += 1
                open_mods.append(p)
            for n in nodes:
                text = f"{_mlabel(_cut(n['label']))}<br/>{_mlabel(n['kind_label'])}"
                lines.append(
                    "    " + "  " * len(open_mods) + f'{ids[n["id"]]}["{text}"]:::{n["tier"]}'
                )
        while open_mods:
            open_mods.pop()
            lines.append("    " + "  " * len(open_mods) + "end")
        lines.append("  end")
    for e in g["edges"]:
        arrow = "-->" if e["kind"] == "TRIGGER" else ("-.->" if e["kind"] == "ACCESS" else "==>")
        lines.append(
            f'  {ids[e["source"]]} {arrow}|"{e["kind"]}: {_mlabel(e["label"])}"| {ids[e["target"]]}'
        )
    for i, e in enumerate(g["edges"]):
        lines.append(f"  linkStyle {i} stroke:{KIND_COLOURS[e['kind']]}")
    return "\n".join(lines) + "\n"


def architecture_svg(model: dict[str, Any], title: str = "") -> str:
    g = architecture_graph(model)
    pad, lane_w, col_w, node_w, node_h, gap = 16, 170, 250, 210, 50, 12
    width = pad * 2 + lane_w + col_w * len(TIERS)
    body: list[str] = []
    icons: set[str] = set()
    pos: dict[str, tuple[float, float]] = {}
    y = pad
    body.append(_text(pad, y + 14, title or "Architecture (flows)", 15, "700"))
    y += 30
    for i, tier in enumerate(TIERS):
        body.append(
            _text(
                pad + lane_w + i * col_w + node_w / 2,
                y + 12,
                TIER_TITLES[tier],
                12,
                "700",
                "#57606a",
                "middle",
            )
        )
    y += 24
    if not g["nodes"]:
        body.append(
            _text(pad, y + 16, "No recognized services in the analyzed code.", 12, fill="#57606a")
        )
        y += 30
    node_svg: list[str] = []
    for root, mods in _groups(g["nodes"]).items():
        top = y
        panel_at = len(body)
        y += 26
        for mod, nodes in mods.items():
            cols = {t: [n for n in nodes if n["tier"] == t] for t in TIERS}
            rows = max(len(v) for v in cols.values())
            lane_h = rows * (node_h + gap) + gap
            body.append(
                f'<rect x="{pad + 8}" y="{y}" width="{width - 2 * pad - 16}" height="{lane_h}" rx="6" '
                f'fill="#f6f8fa" stroke="#d0d7de" stroke-dasharray="4 3"/>'
            )
            body.append(
                _text(pad + 16, y + 18, _cut(mod or "(root module)", 24), 11, "700", "#57606a")
            )
            for ti, tier in enumerate(TIERS):
                for ri, n in enumerate(cols[tier]):
                    nx = pad + lane_w + ti * col_w
                    ny = y + gap + ri * (node_h + gap)
                    pos[n["id"]] = (nx, ny)
                    icons.add(n["icon"])
                    node_svg.append(
                        f'<g><rect x="{nx}" y="{ny}" width="{node_w}" height="{node_h}" rx="6" '
                        f'fill="#ffffff" stroke="{colour(n["icon"])}" stroke-width="1.5"/>'
                        + _icon(n["icon"], nx + 8, ny + 9)
                        + _text(nx + 48, ny + 21, _cut(n["label"], 24), 12, "700")
                        + _text(nx + 48, ny + 37, _cut(n["kind_label"], 28), 10, fill="#57606a")
                        + "</g>"
                    )
            y += lane_h + 8
        label = f"{root}/" if root != "." else "./ (root)"
        body.insert(
            panel_at,
            f'<rect x="{pad}" y="{top}" width="{width - 2 * pad}" height="{y - top}" rx="8" '
            f'fill="none" stroke="{colour("terraform")}" stroke-width="1.5"/>',
        )
        body.append(_text(pad + 10, top + 18, f"root {label}", 12, "700", colour("terraform")))
        y += 12
    edge_svg, label_svg = [], []
    # parallel edges (same pair of nodes, either direction) would draw the same curve and
    # put their labels on the same spot: spread their ends and stack their labels
    pairs: dict[frozenset[str], list[int]] = {}
    for i, e in enumerate(g["edges"]):
        pairs.setdefault(frozenset((e["source"], e["target"])), []).append(i)
    lane: dict[int, float] = {}  # offset of the edge's ends from the node middles
    stack: dict[int, float] = {}  # offset of its label from the shared label spot
    for members in pairs.values():
        step = min(12.0, (node_h - 14) / max(len(members) - 1, 1))
        for j, i in enumerate(members):
            lane[i] = (j - (len(members) - 1) / 2) * step
            stack[i] = (j - (len(members) - 1) / 2) * LABEL_H
    right = float(width)
    for i, e in enumerate(g["edges"]):
        (sx, sy), (tx, ty) = pos[e["source"]], pos[e["target"]]
        c = KIND_COLOURS[e["kind"]]
        y1, y2 = sy + node_h / 2 + lane[i], ty + node_h / 2 + lane[i]
        if tx > sx:
            x1, x2 = sx + node_w, tx
            d = f"M{x1:.0f},{y1:.0f} C{x1 + 50:.0f},{y1:.0f} {x2 - 50:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
        elif tx < sx:
            x1, x2 = sx, tx + node_w
            d = f"M{x1:.0f},{y1:.0f} C{x1 - 50:.0f},{y1:.0f} {x2 + 50:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
        else:
            x1 = x2 = sx + node_w
            d = f"M{x1:.0f},{y1:.0f} C{x1 + 45:.0f},{y1:.0f} {x2 + 45:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
        dash = ' stroke-dasharray="5 3"' if e["kind"] == "ACCESS" else ""
        edge_svg.append(
            f'<path d="{d}" fill="none" stroke="{c}" stroke-width="1.6"{dash} marker-end="url(#arrow-{e["kind"]})"/>'
        )
        text = f"{e['kind']}: {e['label']}"
        mx = (x1 + x2) / 2 + (40 if x1 == x2 else 0)
        my = (sy + ty + node_h) / 2 - 4 + stack[i]
        label_svg.append(_text(mx, my, text, 9, fill=c, anchor="middle"))
        right = max(right, mx + _text_w(text, 9) / 2 + pad)
    width = right  # a label beside the last column must not be cut off
    legend_y = y + 6
    for i, k in enumerate(FLOW_KINDS):
        lx = pad + i * 130
        dash = ' stroke-dasharray="5 3"' if k == "ACCESS" else ""
        body.append(
            f'<line x1="{lx}" y1="{legend_y}" x2="{lx + 30}" y2="{legend_y}" stroke="{KIND_COLOURS[k]}" stroke-width="2"{dash}/>'
        )
        body.append(_text(lx + 36, legend_y + 4, k, 10, fill=KIND_COLOURS[k]))
    height = legend_y + 20
    return _svg(
        width, height, title or "Architecture", body + edge_svg + node_svg + label_svg, icons
    )


# ------------------------------------------------------------------ module tree


def module_tree(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of the module tree: depth, label, details, icon, location, parent row."""
    rows: list[dict[str, Any]] = []
    for r in (model.get("structure") or {}).get("roots", []):
        role = r.get("role", "deployable")
        rows.append(
            {
                "key": f"root:{r['dir']}",
                "parent": None,
                "depth": 0,
                "label": f"{r['dir']}/" if r["dir"] != "." else "./",
                "detail": f"{r['kind']} root, {r['backend']['type']} backend"
                + (", reusable module" if role == "library" else ""),
                "icon": "terraform",
            }
        )
        index = {"": rows[-1]["key"]}
        for m in sorted(r["modules"], key=lambda m: m["address"]):
            parent_addr = (
                m["address"].rpartition(".module.")[0] if ".module." in m["address"] else ""
            )
            kind = m["kind"]
            icon = f"module-{kind}" if kind in ("local", "registry", "git") else "module-remote"
            detail = f"{kind} {m['source'] or '(no literal source)'}"
            if m["version"]:
                detail += f" @ {m['version']}"
            if not m["analyzed"]:
                detail += " (not analyzed)"
            key = f"{r['dir']}:{m['address']}"
            parent = index.get(parent_addr, index[""])
            depth = m["address"].count("module.") if m["address"] != "terraform.source" else 1
            rows.append(
                {
                    "key": key,
                    "parent": parent,
                    "depth": depth,
                    "label": m["address"],
                    "detail": detail,
                    "icon": icon,
                    "kind": kind,
                    "version": m["version"] or "",
                    "source": m["source"] or "(no literal source)",
                    "analyzed": m["analyzed"],
                }
            )
            index[m["address"]] = key
    return rows


def module_tree_mermaid(model: dict[str, Any]) -> str:
    rows = module_tree(model)
    ids = {r["key"]: f"T{i}" for i, r in enumerate(rows)}
    lines = ["flowchart TD"]
    lines.append("  classDef root fill:#efe6fb,stroke:#7b42bc,color:#24292f")
    lines.append("  classDef module fill:#f6f8fa,stroke:#545b64,color:#24292f")
    if not rows:
        lines.append('  empty["No roots detected"]')
    for r in rows:
        if r["parent"] is None:
            text = f"{_mlabel(r['label'])}<br/>{_mlabel(r['detail'])}"
        else:  # kind and version on their own line: a long source cannot hide the version
            head = r["kind"] + (f" @ {r['version']}" if r["version"] else "")
            head += "" if r["analyzed"] else ", not analyzed"
            text = f"{_mlabel(r['label'])}<br/>{_mlabel(head)}<br/>{_mlabel(_cut(r['source'], 60))}"
        cls = "root" if r["parent"] is None else "module"
        lines.append(f'  {ids[r["key"]]}["{text}"]:::{cls}')
    for r in rows:
        if r["parent"] is not None:
            lines.append(f"  {ids[r['parent']]} --> {ids[r['key']]}")
    return "\n".join(lines) + "\n"


def module_tree_svg(model: dict[str, Any], title: str = "") -> str:
    rows = module_tree(model)
    pad, row_h, indent = 16, 34, 30
    body = [_text(pad, pad + 14, title or "Module tree", 15, "700")]
    icons: set[str] = set()
    y0 = pad + 34
    ys: dict[str, float] = {}
    depths = {r["key"]: r["depth"] for r in rows}
    width = 420.0
    for i, r in enumerate(rows):
        x = pad + r["depth"] * indent
        y = y0 + i * row_h
        ys[r["key"]] = y
        if r["parent"] is not None:
            px = pad + depths[r["parent"]] * indent + 11
            py = ys[r["parent"]] + 22
            body.append(
                f'<path d="M{px},{py:.0f} V{y + 11:.0f} H{x}" fill="none" stroke="#8c959f" stroke-width="1"/>'
            )
        icons.add(r["icon"])
        body.append(_icon(r["icon"], x, y, 22))
        body.append(_text(x + 30, y + 15, r["label"], 12, "700"))
        dx = x + 30 + len(r["label"]) * 7.4 + 12
        body.append(_text(dx, y + 15, _cut(r["detail"], 90), 11, fill="#57606a"))
        width = max(width, dx + len(_cut(r["detail"], 90)) * CHAR_W + pad)
    if not rows:
        body.append(_text(pad, y0 + 14, "No roots detected.", 12, fill="#57606a"))
    height = y0 + max(len(rows), 1) * row_h + pad
    return _svg(width, height, title or "Module tree", body, icons)


# ------------------------------------------------------------------ environment matrix


def env_matrix(model: dict[str, Any]) -> dict[str, Any]:
    """Rows (roots with their backend) x columns (environment names) -> cell text."""
    structure = model.get("structure") or {"roots": [], "environments": []}
    envs = structure["environments"]
    names = sorted({e["name"] for e in envs}, key=str.lower) or ["(default)"]
    rows = []
    for r in structure["roots"]:
        b = r["backend"]
        settings = {s["key"]: s["value"] for s in b["settings"]}
        where = settings.get("key") or settings.get("prefix") or settings.get("path") or ""
        store = (
            settings.get("bucket")
            or settings.get("organization")
            or settings.get("storage_account_name")
            or ""
        )
        backend = b["type"] + (f" {store}" if store else "") + (f"/{where}" if where else "")
        cells = {}
        for name in names:
            covering = [
                e
                for e in envs
                if e["name"] == name
                and (r["dir"] in e["roots"] or (not e["roots"] and e["root"] == "."))
            ]
            if name == "(default)":
                cells[name] = "✓ default"
            elif covering:
                e = covering[0]
                how = ", ".join(e["kinds"])
                files = [f.rsplit("/", 1)[-1] for f in e["var_files"]]
                cells[name] = "✓ " + how + (f" ({', '.join(files)})" if files else "")
            else:
                cells[name] = ""
        rows.append({"root": r["dir"], "backend": backend, "cells": cells})
    return {"columns": names, "rows": rows}


def env_matrix_mermaid(model: dict[str, Any]) -> str:
    m = env_matrix(model)
    lines = ["flowchart LR"]
    lines.append("  classDef root fill:#efe6fb,stroke:#7b42bc,color:#24292f")
    lines.append("  classDef env fill:#e6f4ea,stroke:#2da44e,color:#24292f")
    env_ids = {name: f"E{i}" for i, name in enumerate(m["columns"])}
    for name, eid in env_ids.items():
        lines.append(f'  {eid}(["{_mlabel(name)}"]):::env')
    if not m["rows"]:
        lines.append('  empty["No roots detected"]')
    for i, r in enumerate(m["rows"]):
        root = f"{r['root']}/" if r["root"] != "." else "./"
        lines.append(f'  R{i}["{_mlabel(root)}<br/>{_mlabel(_cut(r["backend"], 60))}"]:::root')
    for i, r in enumerate(m["rows"]):
        for name in m["columns"]:
            if r["cells"][name]:
                how = r["cells"][name].removeprefix("✓ ")
                lines.append(f'  R{i} ---|"{_mlabel(_cut(how, 40))}"| {env_ids[name]}')
    return "\n".join(lines) + "\n"


def env_matrix_svg(model: dict[str, Any], title: str = "") -> str:
    m = env_matrix(model)
    pad, row_h = 16, 30
    root_w = max([len(r["root"]) * CHAR_W + 40 for r in m["rows"]] + [120])
    backend_w = max([len(r["backend"]) * CHAR_W + 24 for r in m["rows"]] + [120])
    col_w = [
        max([len(r["cells"][c]) * CHAR_W + 24 for r in m["rows"]] + [len(c) * 7.5 + 24, 90])
        for c in m["columns"]
    ]
    width = pad * 2 + root_w + backend_w + sum(col_w)
    body = [_text(pad, pad + 14, title or "Environment matrix", 15, "700")]
    y = pad + 30
    xs = [pad, pad + root_w, pad + root_w + backend_w]
    for w in col_w[:-1]:
        xs.append(xs[-1] + w)
    headers = ["Root", "Backend", *m["columns"]]
    body.append(
        f'<rect x="{pad}" y="{y}" width="{width - 2 * pad:.0f}" height="{row_h}" fill="#f6f8fa" stroke="#d0d7de"/>'
    )
    for x, h in zip(xs, headers, strict=True):
        body.append(_text(x + 8, y + 19, h, 12, "700"))
    icons = {"terraform"}
    for i, r in enumerate(m["rows"]):
        ry = y + (i + 1) * row_h
        fill = "#ffffff" if i % 2 == 0 else "#fbfbfc"
        body.append(
            f'<rect x="{pad}" y="{ry}" width="{width - 2 * pad:.0f}" height="{row_h}" fill="{fill}" stroke="#d0d7de"/>'
        )
        body.append(_icon("terraform", xs[0] + 6, ry + 6, 18))
        body.append(
            _text(xs[0] + 30, ry + 19, f"{r['root']}/" if r["root"] != "." else "./", 12, "700")
        )
        body.append(_text(xs[1] + 8, ry + 19, r["backend"], 11))
        for j, c in enumerate(m["columns"]):
            cell = r["cells"][c]
            body.append(
                _text(
                    xs[2 + j] + 8, ry + 19, cell or "—", 11, fill="#1a7f37" if cell else "#8c959f"
                )
            )
    total = y + (len(m["rows"]) + 1) * row_h
    for x in xs[1:]:
        body.append(f'<line x1="{x:.0f}" y1="{y}" x2="{x:.0f}" y2="{total}" stroke="#d0d7de"/>')
    if not m["rows"]:
        body.append(_text(pad, total + 18, "No roots detected.", 12, fill="#57606a"))
        total += 24
    return _svg(width, total + pad, title or "Environment matrix", body, icons)


# ------------------------------------------------------------------ export


def mermaid(model: dict[str, Any], name: str) -> str:
    return {
        "architecture": architecture_mermaid,
        "module-tree": module_tree_mermaid,
        "env-matrix": env_matrix_mermaid,
    }[name](model)


def svg(model: dict[str, Any], name: str, title: str = "") -> str:
    full = f"{TITLES[name]}: {title}" if title else TITLES[name]
    return {
        "architecture": architecture_svg,
        "module-tree": module_tree_svg,
        "env-matrix": env_matrix_svg,
    }[name](model, full)


def png_renderer() -> str | None:
    """An offline SVG -> PNG renderer, if one is installed (never downloaded)."""
    if shutil.which("rsvg-convert"):
        return "rsvg-convert"
    try:
        import cairosvg  # noqa: F401
    except ImportError:
        pass
    else:
        return "cairosvg"
    if shutil.which("inkscape"):
        return "inkscape"
    return None


def _png(svg_path: Path, png_path: Path, renderer: str) -> None:
    if renderer == "cairosvg":
        import cairosvg

        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))
        return
    argv = (
        ["rsvg-convert", "-o", str(png_path), str(svg_path)]
        if renderer == "rsvg-convert"
        else ["inkscape", str(svg_path), "--export-type=png", f"--export-filename={png_path}"]
    )
    subprocess.run(argv, check=True, capture_output=True, timeout=60)


def export_diagrams(model: dict[str, Any], out_dir: Path, title: str = "") -> dict[str, Any]:
    """Write ``<name>.mmd`` and ``<name>.svg`` for every diagram (and ``.png`` when an
    offline renderer exists). Returns the files written and the PNG status."""
    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = png_renderer()
    written: list[str] = []
    png_note = (
        f"PNG rendered with {renderer}"
        if renderer
        else "PNG not produced: no offline SVG renderer (rsvg-convert, cairosvg or inkscape) "
        "is installed; the SVG files are complete"
    )
    for name in DIAGRAMS:
        (out_dir / f"{name}.mmd").write_text(mermaid(model, name), encoding="utf-8")
        svg_path = out_dir / f"{name}.svg"
        svg_path.write_text(svg(model, name, title), encoding="utf-8")
        written += [f"{name}.mmd", f"{name}.svg"]
        if renderer:
            try:
                _png(svg_path, out_dir / f"{name}.png", renderer)
                written.append(f"{name}.png")
            except Exception as exc:  # best effort only
                png_note = f"PNG rendering with {renderer} failed ({type(exc).__name__})"
    return {"files": written, "png": png_note}


def validate_mermaid(text: str) -> list[str]:
    """Structural problems of a Mermaid flowchart produced here ([] when valid)."""
    problems = []
    lines = text.splitlines()
    if not lines or not re.fullmatch(r"flowchart (LR|TD|TB|RL|BT)", lines[0]):
        problems.append("first line must be 'flowchart <direction>'")
    depth = 0
    defined: set[str] = set()
    node_re = re.compile(r'^\s*([A-Za-z][\w]*)(\["[^"]*"\]|\(\["[^"]*"\]\))(:::\w+)?$')
    edge_re = re.compile(r'^\s*(\w+) (-->|-\.->|==>|---)(\|"[^"|]*"\|)? (\w+)$')
    refs: list[str] = []
    for n, line in enumerate(lines[1:], 2):
        s = line.strip()
        if not s:
            continue
        if s.startswith("subgraph "):
            if not re.fullmatch(r'subgraph \w+\["[^"]*"\]', s):
                problems.append(f"line {n}: bad subgraph")
            depth += 1
        elif s == "end":
            depth -= 1
            if depth < 0:
                problems.append(f"line {n}: 'end' without subgraph")
        elif s.startswith(("classDef ", "linkStyle ", "direction ")):
            continue
        elif m := node_re.match(line):
            defined.add(m.group(1))
        elif m := edge_re.match(line):
            refs += [m.group(1), m.group(4)]
        else:
            problems.append(f"line {n}: not a node, edge or subgraph: {s[:60]}")
    if depth != 0:
        problems.append("unbalanced subgraph / end")
    problems += [f"edge references undefined node {r}" for r in sorted(set(refs) - defined)]
    return problems
