"""draw.io (diagrams.net) files of the Flow, Network and IAM diagrams.

The same diagram as the SVG: the boxes, their positions, colours and dashes, the steps
and their labels, the frames (VPCs, subnets) and the legend all come from
:func:`tflens.flow.flow_layout`. The file is editable: boxes are vertices (a frame is a
container holding its boxes, so moving it moves them), steps are edges attached to their
boxes (draw.io re-routes them when a box moves), the legend is sample edges with their
text. Names are in full: no shared prefix taken off, no "…" (draw.io wraps them in the
box); a box's tooltip has its id, its ``file:lines`` and its full name, as in the SVG.

Deterministic: the same model always gives the same file.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from typing import Any

from .flow import (
    FLOW_LEGEND,
    FONT,
    FRAME_FILL,
    HUNG,
    MUTED,
    PAD,
    REF_COLOUR,
    ROLE_STROKE,
    SIDE_STROKE,
    chain_title,
    find_chain,
    flow_layout,
    node_tooltip,
)
from .snippets import location_label
from .tabdiagrams import tab_diagram

MIME = "application/vnd.jgraph.mxfile"
_FONT = f"fontFamily={FONT.split(',')[0]};fontColor=#24292f;"
_BOX = (
    "rounded=1;arcSize=6;whiteSpace=wrap;html=1;align=left;verticalAlign=top;"
    "spacingLeft=10;spacingTop=6;fillColor=#ffffff;strokeWidth=2;fontSize=14;" + _FONT
)
_FRAME = (
    "rounded=1;arcSize=3;whiteSpace=wrap;html=1;align=left;verticalAlign=top;"
    "spacingLeft=8;spacingTop=4;strokeWidth=1.5;container=1;collapsible=0;"
    "recursiveResize=0;fontSize=14;" + _FONT
)
_EDGE = "html=1;endArrow=block;endFill=1;fontSize=14;labelBackgroundColor=#ffffff;" + _FONT
_TEXT = "text;html=1;align=left;verticalAlign=middle;whiteSpace=nowrap;" + _FONT


def _fmt(v: float) -> str:
    return f"{round(v, 1):g}"


def _dash(dash: str) -> str:
    return f"dashed=1;dashPattern={dash};" if dash else ""


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _ref_text(item: dict[str, Any]) -> str:
    ref = item.get("code_ref", item.get("location"))
    return location_label(ref) if ref else "no code reference"


def node_style(n: dict[str, Any]) -> str:
    """A box's style: white, rounded, its role's colour (dashed when hung under another)."""
    return (
        _BOX
        + f"strokeColor={ROLE_STROKE[n['role']]};"
        + (_dash("5 3") if n["role"] in HUNG else "")
    )


def node_value(n: dict[str, Any]) -> str:
    """A box's text: the full name (bold), what it is, its summary line, its code ref."""
    size = n.get("name_size", 16)
    lines = [
        f'<b><span style="font-size:{size}px">{_esc(n["label"])}</span></b>',
        f'<font color="{MUTED}">{_esc(n["kind_label"])}</font>',
    ]
    if n.get("meta"):
        lines.append(_esc(n["meta"]))
    lines.append(f'<font color="{REF_COLOUR}">{_esc(_ref_text(n))}</font>')
    return "<br>".join(lines)


def edge_style(e: dict[str, Any], src: tuple[float, ...], dst: tuple[float, ...]) -> str:
    """A step's style: its colour and dash, its arrow (a read points back at the
    reader), and where it leaves / enters its boxes (the SVG's ends, its lane)."""
    s = e["step"]
    x1, y1, x2, y2 = e["ends"]
    style = _EDGE + f"strokeColor={e['colour']};fontColor={e['colour']};" + _dash(e["dash"])
    if s.get("quiet"):
        style += "edgeStyle=orthogonalEdgeStyle;rounded=1;strokeWidth=1.5;"
    else:
        style += "curved=1;strokeWidth=2.2;fontStyle=1;"
    if s["kind"] == "read":
        style += "startArrow=block;startFill=1;endArrow=none;"
    for end, (x, y), (bx, by, bw, bh) in (("exit", (x1, y1), src), ("entry", (x2, y2), dst)):
        style += f"{end}X={_fmt((x - bx) / bw)};{end}Y={_fmt((y - by) / bh)};{end}Perimeter=0;"
    return style


class _File:
    """An mxfile being written: cells in drawing order (containers before what is in them)."""

    def __init__(self, name: str) -> None:
        self.mxfile = ET.Element("mxfile", {"host": "TFLens", "type": "device"})
        diagram = ET.SubElement(self.mxfile, "diagram", {"id": "tflens", "name": name[:80]})
        model = ET.SubElement(
            diagram,
            "mxGraphModel",
            {
                "grid": "1",
                "gridSize": "10",
                "guides": "1",
                "tooltips": "1",
                "connect": "1",
                "arrows": "1",
                "fold": "1",
                "page": "0",
                "pageScale": "1",
                "math": "0",
                "shadow": "0",
            },
        )
        self.root = ET.SubElement(model, "root")
        ET.SubElement(self.root, "mxCell", {"id": "0"})
        ET.SubElement(self.root, "mxCell", {"id": "1", "parent": "0"})
        self.count = 0

    def _id(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}{self.count}"

    def vertex(
        self,
        value: str,
        style: str,
        geometry: tuple[float, float, float, float],
        parent: str = "1",
        tooltip: str = "",
    ) -> str:
        cid = self._id("n")
        attrs = {"style": style, "vertex": "1", "parent": parent}
        if tooltip:  # a UserObject carries the tooltip; its mxCell, the rest
            obj = ET.SubElement(
                self.root, "UserObject", {"id": cid, "label": value, "tooltip": tooltip}
            )
            cell = ET.SubElement(obj, "mxCell", attrs)
        else:
            cell = ET.SubElement(self.root, "mxCell", {"id": cid, "value": value, **attrs})
        x, y, w, h = geometry
        ET.SubElement(
            cell,
            "mxGeometry",
            {"x": _fmt(x), "y": _fmt(y), "width": _fmt(w), "height": _fmt(h), "as": "geometry"},
        )
        return cid

    def edge(
        self,
        value: str,
        style: str,
        source: str | None = None,
        target: str | None = None,
        points: tuple[tuple[float, float], tuple[float, float]] | None = None,
    ) -> str:
        cid = self._id("e")
        attrs = {"id": cid, "value": value, "style": style, "edge": "1", "parent": "1"}
        if source and target:
            attrs.update(source=source, target=target)
        cell = ET.SubElement(self.root, "mxCell", attrs)
        geo = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        if points is not None:  # a free-standing edge (the legend's samples)
            (sx, sy), (tx, ty) = points
            ET.SubElement(geo, "mxPoint", {"x": _fmt(sx), "y": _fmt(sy), "as": "sourcePoint"})
            ET.SubElement(geo, "mxPoint", {"x": _fmt(tx), "y": _fmt(ty), "as": "targetPoint"})
        return cid

    def text(self) -> str:
        ET.indent(self.mxfile)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
            self.mxfile, encoding="unicode"
        )


def flow_drawio(
    chain: dict[str, Any],
    title: str = "",
    legend: tuple[tuple[str, str, str], ...] = FLOW_LEGEND,
) -> str:
    """The draw.io file of a diagram :func:`tflens.flow.flow_svg` draws (a flow, the
    Network or the IAM diagram)."""
    lay = flow_layout(chain, title, legend)
    pos = lay["pos"]
    out = _File(lay["title"])
    out.vertex(
        f"<b>{_esc(lay['title'])}</b>",
        _TEXT + "fontSize=18;",
        (PAD, PAD, max(lay["width"] - 2 * PAD, 200), 28),
    )
    # frames first (outer before inner): each is a container, geometry relative to its own
    cells: dict[str, str] = {}
    origin: dict[str, tuple[float, float]] = {}
    for f, level, x, y, w, h in lay["frames"]:
        parent = f.get("parent") if f.get("parent") in cells else None
        px, py = origin.get(parent, (0.0, 0.0)) if parent else (0.0, 0.0)
        value = (
            f"<b>{_esc(f['label'])}</b><br>"
            f'<font color="{MUTED}">{_esc(f["kind_label"])} · {_esc(_ref_text(f))}</font>'
        )
        style = (
            _FRAME
            + f"fillColor={FRAME_FILL[min(level, 1)]};strokeColor={ROLE_STROKE['network']};"
            + (_dash("6 4") if level else "")
        )
        tip = f"{f['id']} ({_ref_text(f)})\n{f['label']}"
        cells[f["id"]] = out.vertex(
            value, style, (x - px, y - py, w, h), cells[parent] if parent else "1", tip
        )
        origin[f["id"]] = (x, y)
    for n in lay["nodes"]:
        x, y, w, h = pos[n["id"]]
        frame = n.get("frame") if n.get("frame") in cells else None
        fx, fy = origin[frame] if frame else (0.0, 0.0)
        cells[n["id"]] = out.vertex(
            node_value(n),
            node_style(n),
            (x - fx, y - fy, w, h),
            cells[frame] if frame else "1",
            node_tooltip(n),
        )
    for parent, child, _d in lay["connectors"]:  # a hung box: a dashed line from its owner
        bx, _by, bw, _bh = pos[parent]
        out.edge(
            "",
            "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=none;strokeWidth=1.5;"
            f"strokeColor={SIDE_STROKE};{_dash('4 3')}"
            f"exitX={_fmt(12 / bw)};exitY=1;exitPerimeter=0;entryX=0;entryY=0.5;entryPerimeter=0;",
            cells[parent],
            cells[child],
        )
    for e in lay["edges"]:
        s = e["step"]
        value = "" if e["label"] is None else "<br>".join(_esc(t) for t in e["lines"] if t)
        out.edge(
            value,
            edge_style(e, pos[s["source"]], pos[s["target"]]),
            cells[s["source"]],
            cells[s["target"]],
        )
    ly = lay["legend_y"]
    for lx, text, colour, dash in lay["legend"]:
        out.edge(
            "",
            f"html=1;endArrow=none;strokeWidth=2.2;strokeColor={colour};" + _dash(dash),
            points=((lx, ly), (lx + 34, ly)),
        )
        out.vertex(
            _esc(text),
            _TEXT + f"fontSize=14;fontColor={colour};",
            (lx + 40, ly - 10, len(text) * 8.4 + 8, 20),
        )
    return out.text()


def chain_drawio(model: dict[str, Any], name: str, title: str = "") -> str | None:
    """The draw.io file of ``flow-<n>`` (None when there is no such chain)."""
    chain = find_chain(model, name)
    return None if chain is None else flow_drawio(chain, chain_title(chain, title))


def tab_drawio(model: dict[str, Any], name: str, title: str = "") -> str | None:
    """The draw.io file of the ``network`` or ``iam`` diagram (None: unknown name, or
    nothing to draw)."""
    found = tab_diagram(model, name, title)
    return None if found is None else flow_drawio(*found)
