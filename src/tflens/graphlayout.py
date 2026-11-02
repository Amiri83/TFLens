"""Deterministic, linear-time positions for the Graph view.

The Graph tab used to run a force-directed layout (Cytoscape's ``cose``) in the browser
over every visible element and then fit the result to the panel. On large repositories
(hundreds of nodes in dozens of module groups) that layout spreads the disconnected
module groups over thousands of pixels, the compound boxes are at least as wide as their
labels, and fitting it all into the panel gives a zoom where nodes and labels are below
a pixel: the canvas looks blank while hovering still hits the (invisible) group boxes.
It also costs O(n^2) per iteration on the page's main thread.

Here clusters (roots, module calls, nested module calls) are packed as boxes, each
cluster's own nodes in a grid inside it, so the drawing stays compact and readable at
any size; the UI draws it with Cytoscape's ``preset`` layout. Pure functions, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

CELL_W = 180.0  # room for a node and its label (~24 characters at 12px)
CELL_H = 84.0
PAD = 24.0  # inside a cluster box
HEADER = 34.0  # a cluster's label above its content
GAP = 36.0  # between sibling boxes


@dataclass
class Cluster:
    key: str
    leaves: list[str] = field(default_factory=list)
    children: list[Cluster] = field(default_factory=list)
    # computed by _measure / _place
    w: float = 0.0
    h: float = 0.0
    x: float = 0.0
    y: float = 0.0
    shelves: list[list[Cluster]] = field(default_factory=list)
    leaf_cols: int = 0
    leaf_h: float = 0.0


def _grid_cols(n: int) -> int:
    return max(1, math.ceil(math.sqrt(n * 1.8))) if n else 0


def _shelves(boxes: list[Cluster], width: float) -> list[list[Cluster]]:
    rows: list[list[Cluster]] = [[]]
    used = 0.0
    for b in boxes:
        if rows[-1] and used + b.w > width:
            rows.append([])
            used = 0.0
        rows[-1].append(b)
        used += b.w + GAP
    return [r for r in rows if r]


def _measure(c: Cluster, depth: int = 0) -> None:
    top = depth == 0
    for ch in c.children:
        _measure(ch, depth + 1)
    cols = _grid_cols(len(c.leaves))
    leaf_w = cols * CELL_W
    leaf_h = math.ceil(len(c.leaves) / cols) * CELL_H if c.leaves else 0.0
    area = sum(ch.w * ch.h for ch in c.children) + leaf_w * leaf_h
    # screens are wider than tall: the outer levels (the roots) pack wider than modules
    wide = 2.0 if depth <= 1 else 1.4
    target = max([leaf_w, math.sqrt(area) * wide] + [ch.w for ch in c.children])
    shelves = _shelves(c.children, target)
    shelf_w = max([sum(b.w for b in r) + GAP * (len(r) - 1) for r in shelves] or [0.0])
    shelf_h = sum(max(b.h for b in r) for r in shelves) + GAP * max(len(shelves) - 1, 0)
    inner_w = max(leaf_w, shelf_w)
    inner_h = leaf_h + (GAP if leaf_h and shelf_h else 0.0) + shelf_h
    pad, header = (0.0, 0.0) if top else (PAD, HEADER)
    c.w, c.h = inner_w + 2 * pad, inner_h + header + 2 * pad
    c.shelves, c.leaf_cols, c.leaf_h = shelves, cols, leaf_h


def _place(c: Cluster, x: float, y: float, out: dict[str, dict[str, float]], top: bool) -> None:
    c.x, c.y = x, y
    pad, header = (0.0, 0.0) if top else (PAD, HEADER)
    cols, leaf_h = c.leaf_cols, c.leaf_h
    for i, leaf in enumerate(c.leaves):
        r, k = divmod(i, cols)
        out[leaf] = {
            "x": round(x + pad + k * CELL_W + CELL_W / 2, 1),
            "y": round(y + header + pad + r * CELL_H + CELL_H / 2, 1),
        }
    cy = y + header + pad + leaf_h + (GAP if leaf_h and c.children else 0.0)
    for row in c.shelves:
        cx = x + pad
        for ch in row:
            _place(ch, cx, cy, out, False)
            cx += ch.w + GAP
        cy += max(b.h for b in row) + GAP


def pack(top: Cluster) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """Positions of every leaf (node centre) and the box of every cluster (x, y, w, h)."""
    _measure(top)
    positions: dict[str, dict[str, float]] = {}
    _place(top, 0.0, 0.0, positions, True)
    boxes: dict[str, dict[str, float]] = {}

    def walk(c: Cluster) -> None:
        boxes[c.key] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
        for ch in c.children:
            walk(ch)

    walk(top)
    return positions, boxes


def layout(
    leaves: list[tuple[str, list[str], tuple[Any, ...]]],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """``leaves``: (node id, cluster path outermost first, sort key). Returns the node
    positions and each cluster's box; clusters are keyed by their path's last element."""
    top = Cluster("")
    index: dict[tuple[str, ...], Cluster] = {(): top}
    for leaf, path, _key in sorted(leaves, key=lambda t: (t[1], t[2], t[0])):
        parent = top
        for depth in range(len(path)):
            sub = tuple(path[: depth + 1])
            if sub not in index:
                index[sub] = Cluster(path[depth])
                parent.children.append(index[sub])
            parent = index[sub]
        parent.leaves.append(leaf)
    return pack(top)
