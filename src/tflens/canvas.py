"""The editable canvases of the web UI: every diagram as Cytoscape elements.

The Graph and each diagram (Architecture, Module tree, a Flow chain, a root's Network
or IAM diagram) are drawn by one shared front-end component (``static/diagram.js``).
This module turns a diagram into the elements it draws, with a deterministic starting
layout; the user may then move nodes and frames, and the layout they leave is saved per
run (:mod:`tflens.layouts`).

Every node carries the same service colour and icon as in the Graph and the tables
(:func:`node_icon`, :func:`tflens.icons.service_colour`); every edge an edge type
(TRIGGER / ACCESS / NETWORK / CONFIG, or CONTAINS for structure) that the legend can
show or hide.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from .analyze import service_of
from .envs import INVENTORY, OPTIONAL_INVENTORY
from .flow import (
    FLOW_LEGEND,
    ROLE_STROKE,
    chain_title,
    find_chain,
    flow_layout,
    node_tooltip,
)
from .icons import ICONS, TYPE_ICONS, service_colour, symbol
from .labels import call_name
from .snippets import location_label
from .tabdiagrams import tab_diagram
from .visuals import (
    TIERS,
    TITLES,
    architecture_graph,
    module_tree,
    root_node_id,
)

# Edge type colours: the Graph's (webapi.KIND_COLORS) and the diagrams' are these.
KIND_COLOURS = {
    "TRIGGER": "#ff7f0e",
    "ACCESS": "#d62728",
    "NETWORK": "#1f77b4",
    "CONFIG": "#7f7f7f",
    "RUNS_ON": "#9467bd",
    "CONTAINS": "#8c959f",
}
LEGEND_KINDS = ("TRIGGER", "ACCESS", "NETWORK", "CONFIG")
# A Flow / Network / IAM step kind -> its edge type.
STEP_KINDS = {
    "trigger": "TRIGGER",
    "send": "ACCESS",
    "read": "ACCESS",
    "access": "ACCESS",
    "deny": "ACCESS",
    "rule": "NETWORK",
    "contains": "CONTAINS",
}
STATUS_DASH = {"unverified": "dashed", "iam_only": "dotted"}
CANVASES = ("architecture", "module-tree")  # plus flow-<n>, network, iam

# starting layout (model units, i.e. pixels at 100%)
COL_W, ROW_H = 300.0, 120.0
FRAME_PAD, FRAME_HEAD = 26.0, 34.0
TREE_COL_W, TREE_ROW_H = 250.0, 130.0

_SYMBOL = re.compile(r'<symbol id="[^"]*" viewBox="([^"]+)">(.*)</symbol>', re.S)


# ------------------------------------------------------------------ icons and colours


def icon_svg(name: str) -> str:
    """A standalone SVG of an icon (the same drawing the exported SVGs embed)."""
    m = _SYMBOL.fullmatch(symbol(name))
    view_box, body = (m.group(1), m.group(2)) if m else ("0 0 64 64", "")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" '
        f'viewBox="{view_box}">{body}</svg>'
    )


def icon_uri(name: str) -> str:
    """An icon as a ``data:`` URI (allowed by the page's ``img-src 'self' data:``)."""
    return "data:image/svg+xml;base64," + base64.b64encode(icon_svg(name).encode()).decode()


def node_icon(rtype: str, service: str | None = None) -> str:
    """The icon of a resource type (else of its service, else the generic one)."""
    if rtype in TYPE_ICONS:
        return TYPE_ICONS[rtype]
    if service and service in ICONS:
        return service
    return "generic"


def type_style(rtype: str, service: str | None = None) -> dict[str, str]:
    """How a resource type is shown everywhere: its service, colour and icon."""
    svc = service or (service_of(rtype) if rtype else "")
    return {"service": svc, "colour": service_colour(svc), "icon": node_icon(rtype, svc)}


def icons_view(model: dict[str, Any]) -> dict[str, Any]:
    """Icons (data URIs), the style of every type in the model and of each inventory
    row, and the edge type colours: the tables use these to look like the canvases."""
    types: dict[str, dict[str, str]] = {}
    for n in model["nodes"]:
        if n["kind"] in ("resource", "data") and n["type"] not in types:
            types[n["type"]] = type_style(n["type"], n["service"] or None)
    inventory = {}
    for key, _label, rtypes in INVENTORY + OPTIONAL_INVENTORY:
        inventory[key] = types.get(rtypes[0]) or type_style(rtypes[0])
    return {
        "icons": {name: icon_uri(name) for name in sorted(ICONS)},
        "types": dict(sorted(types.items())),
        "inventory": inventory,
        "kinds": dict(KIND_COLOURS),
    }


def _kinds(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The legend: each edge type drawn, with its colour and count (types first)."""
    counts: dict[str, int] = {}
    for e in edges:
        counts[e["data"]["kind"]] = counts.get(e["data"]["kind"], 0) + 1
    order = [*LEGEND_KINDS, *sorted(k for k in counts if k not in LEGEND_KINDS)]
    return [
        {"kind": k, "colour": KIND_COLOURS.get(k, "#57606a"), "count": counts[k]}
        for k in order
        if k in counts
    ]


def _result(name: str, title: str, nodes: list, edges: list, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "nodes": nodes,
        "edges": edges,
        "kinds": _kinds(edges),
        **extra,
    }


# ------------------------------------------------------------------ architecture


def _parent_module(module: str) -> str:
    head, sep, _ = module.rpartition(".module.")
    return head if sep else ""


def _module_chain(module: str) -> list[str]:
    """``R::module.a.module.b`` -> ``["R::module.a", "R::module.a.module.b"]``."""
    out = []
    while module:
        out.append(module)
        module = _parent_module(module)
    return out[::-1]


def architecture_canvas(model: dict[str, Any]) -> dict[str, Any]:
    """The Architecture diagram: services in root and module frames (moveable groups),
    trigger -> compute -> data columns, the TRIGGER / ACCESS / NETWORK flows."""
    g = architecture_graph(model)
    by_id = {n["id"]: n for n in model["nodes"]}
    frames: dict[str, dict[str, Any]] = {}
    nodes: list[dict[str, Any]] = []

    def root_frame(root: str) -> str:
        fid = f"frame:root:{root}"
        if fid not in frames:
            label = f"root {root}/" if root != "." else "root ./"
            frames[fid] = {
                "data": {"id": fid, "label": label, "detail": root_node_id(root), "kind": "root"},
                "classes": "frame root-frame",
            }
        return fid

    def module_frame(module: str, root: str) -> str | None:
        parent = root_frame(root)
        for m in _module_chain(module):
            fid = f"frame:module:{m}"
            if fid not in frames:
                frames[fid] = {
                    "data": {
                        "id": fid,
                        "label": f"{call_name(m) or m} · module",
                        "detail": m,
                        "kind": "module",
                        "parent": parent,
                    },
                    "classes": "frame module-frame",
                }
            parent = fid
        return parent

    # bands: one per (root, module), in order; columns by tier inside a band
    bands: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for n in g["nodes"]:
        full = by_id[n["id"]]["module"] or ""
        bands.setdefault((n["root"], full), []).append(n)
    y = 0.0
    prev: list[str] = []
    prev_root = None
    for (root, module), members in sorted(bands.items()):
        chain = _module_chain(module)
        if root != prev_root:
            if prev_root is not None:
                y += FRAME_PAD * (len(prev) + 1) + 60
            y += FRAME_HEAD + FRAME_PAD
            prev = []
            prev_root = root
        common = 0
        while common < min(len(prev), len(chain)) and prev[common] == chain[common]:
            common += 1
        y += FRAME_PAD * (len(prev) - common) + (FRAME_HEAD + FRAME_PAD) * (len(chain) - common)
        parent = module_frame(module, root) if module else root_frame(root)
        cols = {t: [n for n in members if n["tier"] == t] for t in TIERS}
        for ti, tier in enumerate(TIERS):
            for ri, n in enumerate(cols[tier]):
                style = type_style(n["type"], n["service"])
                nodes.append(
                    {
                        "data": {
                            "id": n["id"],
                            "label": n["label"],
                            "sub": n["sub_label"],
                            "detail": n["id"],
                            "type": n["type"],
                            "service": style["service"],
                            "color": style["colour"],
                            "icon": style["icon"],
                            "tier": tier,
                            "parent": parent,
                        },
                        "classes": "icon-node",
                        "position": {"x": ti * COL_W, "y": y + ri * ROW_H},
                    }
                )
        y += max(len(v) for v in cols.values()) * ROW_H
        prev = chain
    edges = []
    model_edges: dict[tuple[str, str, str], str] = {}
    for e in model["edges"]:
        model_edges.setdefault((e["kind"], e["source"], e["target"]), e["id"])
    for e in g["edges"]:
        key = (e["kind"], e["source"], e["target"])
        edges.append(
            {
                "data": {
                    "id": f"{e['kind']}:{e['source']}->{e['target']}",
                    "source": e["source"],
                    "target": e["target"],
                    "kind": e["kind"],
                    "label": e["label"],
                    "color": KIND_COLOURS[e["kind"]],
                    "edge": model_edges.get(key),
                }
            }
        )
    return _result(
        "architecture",
        TITLES["architecture"],
        list(frames.values()) + nodes,
        edges,
        empty="No recognized services in the analyzed code." if not nodes else "",
    )


# ------------------------------------------------------------------ module tree


def module_tree_canvas(model: dict[str, Any]) -> dict[str, Any]:
    """The Module tree: each root and the module calls under it, a top-down tree."""
    rows = module_tree(model)
    kids: dict[str | None, list[dict[str, Any]]] = {}
    for r in rows:
        kids.setdefault(r["parent"], []).append(r)
    pos: dict[str, tuple[float, float]] = {}
    slot = [0.0]

    def place(r: dict[str, Any], depth: int) -> float:
        children = kids.get(r["key"], [])
        if children:
            xs = [place(c, depth + 1) for c in children]
            x = (xs[0] + xs[-1]) / 2
        else:
            x = slot[0]
            slot[0] += TREE_COL_W
        pos[r["key"]] = (x, depth * TREE_ROW_H)
        return x

    for r in kids.get(None, []):
        place(r, 0)
        slot[0] += TREE_COL_W / 2  # a gap between two roots' trees
    nodes, edges = [], []
    for r in rows:
        head = r["detail"]
        if r["parent"] is not None:
            head = r["kind"] + (f" @ {r['version']}" if r["version"] else "")
            head += "" if r["analyzed"] else ", not analyzed"
        x, y = pos[r["key"]]
        colour = service_colour("terraform") if r["parent"] is None else ICONS[r["icon"]][0]
        nodes.append(
            {
                "data": {
                    "id": f"tree:{r['key']}",
                    "label": r["label"],
                    "sub": head,
                    "detail": r["node"],
                    "service": "terraform",
                    "color": colour,
                    "icon": r["icon"],
                    "tip": r["detail"],
                },
                "classes": "icon-node" + (" tree-root" if r["parent"] is None else ""),
                "position": {"x": x, "y": y},
            }
        )
        if r["parent"] is not None:
            edges.append(
                {
                    "data": {
                        "id": f"calls:{r['parent']}->{r['key']}",
                        "source": f"tree:{r['parent']}",
                        "target": f"tree:{r['key']}",
                        "kind": "CONTAINS",
                        "label": "",
                        "color": KIND_COLOURS["CONTAINS"],
                        "direction": "downward",
                    }
                }
            )
    return _result(
        "module-tree",
        TITLES["module-tree"],
        nodes,
        edges,
        empty="No roots detected." if not nodes else "",
    )


# ------------------------------------------------------------------ flows, Network, IAM


def _loc(ref: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ref:
        return None
    return {k: ref[k] for k in ("file", "start_line", "end_line") if k in ref}


def boxes_canvas(
    model: dict[str, Any],
    chain: dict[str, Any],
    name: str,
    title: str,
    legend: tuple[tuple[str, str, str], ...] = FLOW_LEGEND,
) -> dict[str, Any]:
    """A diagram drawn as boxes (a Flow chain, the Network or IAM diagram): the same
    boxes, frames and steps as its SVG / draw.io file, as moveable canvas elements."""
    lay = flow_layout(chain, title, legend)
    known = {n["id"] for n in model["nodes"]}
    pos = lay["pos"]
    nodes: list[dict[str, Any]] = []
    frame_ids = set()
    for f, _level, *_geo in lay["frames"]:
        frame_ids.add(f["id"])
    for f, _level, *_geo in lay["frames"]:
        parent = f.get("parent") if f.get("parent") in frame_ids else None
        ref = f.get("code_ref", f.get("location"))
        nodes.append(
            {
                "data": {
                    "id": f"frame:{f['id']}",
                    "label": f"{f['label']} · {f['kind_label']}",
                    "detail": f["id"] if f["id"] in known else None,
                    "code_ref": _loc(ref),
                    "kind": "frame",
                    "parent": f"frame:{parent}" if parent else None,
                    "tip": f"{f['id']} ({location_label(ref) if ref else 'no code reference'})",
                },
                "classes": "frame net-frame",
            }
        )
    by_id = {n["id"]: n for n in model["nodes"]}
    for n in lay["nodes"]:
        x, y, w, h = pos[n["id"]]
        frame = n.get("frame") if n.get("frame") in frame_ids else None
        src = by_id.get(n["id"])
        style = (
            type_style(src["type"], src["service"] or None)
            if src and src["kind"] in ("resource", "data")
            else {"service": "external", "colour": service_colour("external"), "icon": "generic"}
        )
        nodes.append(
            {
                "data": {
                    "id": n["id"],
                    "label": n["label"],
                    "sub": n["kind_label"] + (f"\n{n['meta']}" if n.get("meta") else ""),
                    "detail": n["id"] if n["id"] in known else None,
                    "code_ref": _loc(n.get("code_ref", n.get("location"))),
                    "service": style["service"],
                    "color": style["colour"],
                    "stroke": ROLE_STROKE.get(n["role"], style["colour"]),
                    "icon": style["icon"],
                    "role": n["role"],
                    "w": w,
                    "h": h,
                    "parent": f"frame:{frame}" if frame else None,
                    "tip": node_tooltip(n),
                },
                "classes": "box-node" + (" hung" if n["role"] in ("side", "context") else ""),
                "position": {"x": x + w / 2, "y": y + h / 2},
            }
        )
    edges: list[dict[str, Any]] = []
    for parent, child, _d in lay["connectors"]:
        edges.append(
            {
                "data": {
                    "id": f"hung:{parent}->{child}",
                    "source": parent,
                    "target": child,
                    "kind": "CONTAINS",
                    "label": "",
                    "color": KIND_COLOURS["CONTAINS"],
                    "dash": "dashed",
                }
            }
        )
    for e in lay["edges"]:
        s = e["step"]
        kind = STEP_KINDS.get(s["kind"], "CONFIG")
        edges.append(
            {
                "data": {
                    "id": f"step:{s['n']}:{s['source']}->{s['target']}",
                    "source": s["source"],
                    "target": s["target"],
                    "kind": kind,
                    "label": "" if s.get("quiet") else f"{s['n']}. {s['verb'].split(' (')[0]}",
                    "color": KIND_COLOURS[kind],
                    "dash": STATUS_DASH.get(s["status"], "solid"),
                    "status": s["status"],
                    "reverse": s["kind"] == "read",
                    "summary": s["summary"],
                    "evidence": [
                        {"what": ev["what"], "location": _loc(ev["location"])}
                        for ev in s["evidence"]
                    ],
                }
            }
        )
    return _result(name, lay["title"], nodes, edges, empty="")


def canvas_view(model: dict[str, Any], name: str, title: str = "") -> dict[str, Any] | None:
    """The canvas of a diagram by name (None: unknown, or nothing to draw)."""
    if name == "architecture":
        return architecture_canvas(model)
    if name == "module-tree":
        return module_tree_canvas(model)
    if name.startswith("flow-"):
        chain = find_chain(model, name)
        if chain is None:
            return None
        return boxes_canvas(model, chain, name, chain_title(chain, title))
    found = tab_diagram(model, name, title)
    if found is None:
        return None
    dg, full, legend = found
    return boxes_canvas(model, dg, name, full, legend)


__all__ = [
    "CANVASES",
    "KIND_COLOURS",
    "canvas_view",
    "icon_uri",
    "icons_view",
    "node_icon",
    "type_style",
]
