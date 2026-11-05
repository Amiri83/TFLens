"""Visual polish + interactive diagrams: one shared diagram component (IPLens's toolbar)
for the Graph and every diagram, draw.io-style editing with the layout saved per run in
SQLite, the same service colours and icons everywhere, an edge-type legend, the 15px
typography, a legible selection in dark mode, and nothing loaded from a CDN."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens.canvas import KIND_COLOURS, canvas_view, icon_svg, node_icon
from tflens.db import closing, init_db
from tflens.icons import ICONS, service_colour
from tflens.layouts import LayoutError, SqliteLayouts, clean_positions
from tflens.web import create_app
from tflens.webapi import KIND_COLORS, Api, ModelStore

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "tflens" / "static"
TOOLBAR = (
    "fit",
    "zoom-out",
    "zoom-in",
    "zoom-reset",
    "fullscreen",
    "export-png",
    "export-svg",
    "export-drawio",
    "reset-layout",
    "tidy",
    "snap",
    "legend",
)


def _get(api: Api, target: str):
    resp = api.handle("GET", target)
    assert resp.status == 200, (target, resp.body[:300])
    return json.loads(resp.body)


def _post(api: Api, target: str, body=None):
    raw = b"" if body is None else json.dumps(body).encode()
    resp = api.handle("POST", target, raw)
    return resp.status, json.loads(resp.body)


def _api(tmp_path, fixture="multi_stack_envs") -> Api:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / fixture, repo)
    return Api(ModelStore(repo, tmp_path / "out" / "model.json"))


def _check_elements(data):
    """What Cytoscape needs: unique ids, every parent and edge end exists, leaves placed."""
    ids = [n["data"]["id"] for n in data["nodes"]] + [e["data"]["id"] for e in data["edges"]]
    assert len(ids) == len(set(ids)), "duplicate element ids"
    nodes = {n["data"]["id"]: n for n in data["nodes"]}
    parents = {n["data"].get("parent") for n in data["nodes"]} - {None}
    assert parents <= set(nodes)
    for e in data["edges"]:
        assert e["data"]["source"] in nodes and e["data"]["target"] in nodes, e["data"]["id"]
        assert e["data"]["kind"] in KIND_COLOURS
        assert e["data"]["color"] == KIND_COLOURS[e["data"]["kind"]]
    for nid, n in nodes.items():
        if nid not in parents:  # a leaf: a starting position, an icon
            assert "position" in n, nid
            assert n["data"]["icon"] in ICONS
    counts = {}
    for e in data["edges"]:
        counts[e["data"]["kind"]] = counts.get(e["data"]["kind"], 0) + 1
    assert {k["kind"]: k["count"] for k in data["kinds"]} == counts


# ---------------------------------------------------------------- 1 + 7. one component, vendored


def test_page_loads_only_local_scripts_in_order(tmp_path):
    html = _api(tmp_path).handle("GET", "/").body.decode()
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts[:4] == [
        "/static/vendor/cytoscape.min.js",
        "/static/vendor/dagre.min.js",
        "/static/diagram.js",
        "/static/app.js",
    ]
    for src in scripts:
        assert src.startswith("/static/"), src
        assert (STATIC / src[len("/static/") :]).is_file(), src
    for name in ("cytoscape", "dagre"):
        assert (STATIC / "vendor" / f"{name}.LICENSE").is_file()
    # no remote script, style sheet or font anywhere in the page's assets
    templates = STATIC.parent / "templates"
    for path in [templates / "base.html", templates / "analyzer.html"] + [
        STATIC / f for f in ("app.css", "style.css", "app.js", "diagram.js", "discover.js")
    ]:
        text = path.read_text()
        assert not re.search(r"""(src|href)=["']https?://""", text), path
        assert not re.search(r"@import|url\(\s*['\"]?https?:", text), path
        assert "@font-face" not in text, path  # Inter only when installed locally
        assert not re.search(r"cdn|unpkg|jsdelivr|googleapis", text, re.I), path


def test_index_is_served_with_the_component(tmp_path):
    api = _api(tmp_path)
    html = api.handle("GET", "/").body.decode()
    assert 'src="/static/diagram.js"' in html and 'src="/static/vendor/dagre.min.js"' in html
    for asset in ("diagram.js", "vendor/dagre.min.js", "vendor/cytoscape.min.js"):
        resp = api.handle("GET", f"/static/{asset}")
        assert resp.status == 200 and "javascript" in resp.content_type


def test_component_has_the_iplens_toolbar():
    js = (STATIC / "diagram.js").read_text()
    for name in TOOLBAR:
        assert f'"{name}"' in js, name
    assert "data-zoom-label" in js and "Math.round(state.cy.zoom() * 100)}%" in js  # live zoom %
    assert "requestFullscreen" in js and "fullscreenchange" in js
    assert "cy.png(" in js and "toSvg(currentView())" in js and "toDrawio(currentView())" in js
    assert "iplens/static/visual.js" in js  # whose look and behaviour it follows


def test_graph_and_every_diagram_use_the_one_component():
    js = (STATIC / "app.js").read_text()
    assert "graphCanvas = window.TFDiagram.create(container, {" in js
    assert "graphCanvas.attach(cy);" in js and "container: graphCanvas.canvas" in js
    body = js.split("function diagramCanvas(name, title, query) {", 1)[1].split("\n  }\n", 1)[0]
    assert "window.TFDiagram.create(host, {" in body and ".draw(data)" in body
    assert "api/canvas?name=${name}" in body
    # the Diagrams tab, each Flow and a root's Network / IAM diagram
    assert "canvasReady() ? diagramCanvas(name, title)" in js
    assert "diagramView(c.id, `Flow ${c.n}`, diagramQuery)" in js
    assert "canvasReady() ? diagramCanvas(d.diagram," in js
    # no second toolbar or canvas implementation: Cytoscape is only created in two places
    assert js.count("window.cytoscape({") == 1
    assert (STATIC / "diagram.js").read_text().count("root.cytoscape({") == 1


# ---------------------------------------------------------------- 2. editable canvas


def test_canvas_is_editable_draw_io_style():
    js = (STATIC / "diagram.js").read_text()
    assert "cy.boxSelectionEnabled(true)" in js  # Shift + drag: a selection box
    assert "oe.shiftKey || oe.ctrlKey || oe.metaKey" in js  # adding to the selection
    assert '"curve-style": "taxi"' in js  # orthogonal edges, re-routed while moving
    assert 'cy.on("dragfree", "node"' in js and "snap(p.x)" in js  # snap to the grid
    assert "const GRID = 20;" in js and "tfd-grid" in js
    assert "evt.target.descendants()" in js  # a moved frame snaps what it holds
    assert "ArrowLeft" in js  # nudging the selection
    assert "root.dagre.layout(g)" in js and "compound: true" in js  # Tidy
    app = (STATIC / "app.js").read_text()
    assert '"curve-style": "taxi"' in app  # the Graph's edges too


def test_layout_is_saved_restored_and_reset_through_the_api():
    js = (STATIC / "diagram.js").read_text()
    assert "sendJson(viewUrl(o.layoutUrl), { positions: out })" in js
    assert "sendJson(viewUrl(`${o.layoutUrl}/reset`))" in js
    assert '"X-TFLens": "1"' in js  # the JSON API's CSRF guard
    assert "fetch(viewUrl(o.layoutUrl)" in js and "applyPositions(state.saved)" in js
    app = (STATIC / "app.js").read_text()
    # the Graph starts from the server's layout with the saved positions on top
    assert "graphCanvas ? graphCanvas.positions() : (graphData && graphData.positions)" in app


def test_canvas_payloads_for_every_diagram(tmp_path):
    api = _api(tmp_path, "app_flows")
    for name in ("architecture", "module-tree"):
        data = _get(api, f"/api/canvas?name={name}")
        assert data["name"] == name and data["nodes"]
        _check_elements(data)
    arch = _get(api, "/api/canvas?name=architecture")
    frames = [n for n in arch["nodes"] if "frame" in n.get("classes", "")]
    assert any("root-frame" in f["classes"] for f in frames)  # moveable root groups
    assert {e["data"]["kind"] for e in arch["edges"]} <= {"TRIGGER", "ACCESS", "NETWORK"}
    assert all(e["data"]["edge"] for e in arch["edges"])  # a click opens its evidence
    flows = _get(api, "/api/flow")["chains"]
    assert flows
    for c in flows:
        data = _get(api, f"/api/canvas?name={c['id']}")
        _check_elements(data)
        steps = [e for e in data["edges"] if e["data"]["id"].startswith("step:")]
        assert len(steps) == len(c["steps"])
        for e in steps:
            assert e["data"]["summary"] and isinstance(e["data"]["evidence"], list)
    assert api.handle("GET", "/api/canvas?name=flow-999").status == 404
    assert api.handle("GET", "/api/canvas?name=nope").status == 404
    assert api.handle("GET", "/api/canvas?name=architecture&root=nope").status == 404


@pytest.mark.parametrize(("fixture", "name"), [("security_groups", "network"), ("iam", "iam")])
def test_network_and_iam_canvases_of_a_root(tmp_path, fixture, name):
    api = _api(tmp_path, fixture)
    roots = [r["dir"] for r in _get(api, "/api/shapes")["roots"]]
    drawn = [
        r
        for r in roots
        if api.handle("GET", f"/api/diagram?name={name}&format=svg&root={r}").status == 200
    ]
    assert drawn  # the same roots get a canvas as get an SVG
    for r in roots:
        status = api.handle("GET", f"/api/canvas?name={name}&root={r}").status
        assert status == (200 if r in drawn else 404)
    for r in drawn:
        data = _get(api, f"/api/canvas?name={name}&root={r}")
        _check_elements(data)
        assert any("box-node" in n["classes"] for n in data["nodes"])
        assert all(n["data"]["tip"] for n in data["nodes"])  # hover text


def test_module_tree_is_a_top_down_tree(tmp_path):
    data = _get(_api(tmp_path), "/api/canvas?name=module-tree")
    nodes = {n["data"]["id"]: n for n in data["nodes"]}
    for e in data["edges"]:
        src, dst = nodes[e["data"]["source"]], nodes[e["data"]["target"]]
        assert dst["position"]["y"] > src["position"]["y"]
        assert e["data"]["direction"] == "downward"
    roots = [n for n in data["nodes"] if "tree-root" in n["classes"]]
    assert roots and all(n["data"]["detail"].startswith("root:") for n in roots)


def test_layout_api_round_trip_standalone(tmp_path):
    api = _api(tmp_path)
    assert _get(api, "/api/layout?view=graph") == {"view": "graph", "positions": None}
    status, out = _post(api, "/api/layout?view=graph", {"positions": {"a.b": {"x": 1.26, "y": -2}}})
    assert status == 200 and out == {"ok": True, "view": "graph", "count": 1}
    assert _get(api, "/api/layout?view=graph")["positions"] == {"a.b": {"x": 1.3, "y": -2.0}}
    # per view, and kept in SQLite next to model.json: a new process finds it
    db = tmp_path / "out" / "layouts.sqlite"
    assert db.is_file()
    again = Api(ModelStore(tmp_path / "repo", tmp_path / "out" / "model.json"))
    assert _get(again, "/api/layout?view=graph")["positions"] == {"a.b": {"x": 1.3, "y": -2.0}}
    assert _get(again, "/api/layout?view=architecture")["positions"] is None
    status, out = _post(again, "/api/layout/reset?view=graph")
    assert status == 200 and out["reset"] is True
    assert _get(api, "/api/layout?view=graph")["positions"] is None
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_layouts").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("view", "body"),
    [
        ("graph", {"positions": {"a": {"x": "1", "y": 2}}}),
        ("graph", {"positions": {"a": {"x": float("inf"), "y": 2}}}),
        ("graph", {"positions": {"a": {"x": 1}}}),
        ("graph", {"positions": []}),
        ("graph", ["not", "an", "object"]),
        ("bad view", {"positions": {}}),
        ("", {"positions": {}}),
    ],
)
def test_layout_api_rejects_bad_input(tmp_path, view, body):
    api = _api(tmp_path)
    raw = json.dumps(body).replace("Infinity", "1e999").encode()
    resp = api.handle("POST", f"/api/layout?view={view.replace(' ', '%20')}", raw)
    assert resp.status == 400
    assert not (tmp_path / "out" / "layouts.sqlite").exists()


def test_clean_positions_limits():
    assert clean_positions({"n": {"x": 0, "y": 0}}) == {"n": {"x": 0.0, "y": 0.0}}
    for bad in ({"": {"x": 0, "y": 0}}, {"n": {"x": True, "y": 0}}, {"n": {"x": 1e9, "y": 0}}):
        with pytest.raises(LayoutError):
            clean_positions(bad)


def test_layouts_per_run_in_the_app_database(tmp_path):
    db = tmp_path / "app.db"
    init_db(db)
    with closing(db) as conn:
        for i in (1, 2):
            conn.execute(
                "INSERT INTO runs(id, ts, model_path) VALUES(?, '2026-01-01T00:00:00', 'm')", (i,)
            )
    one, two = SqliteLayouts(db, 1, managed=True), SqliteLayouts(db, 2, managed=True)
    one.save("graph", {"n": {"x": 10, "y": 20}})
    assert one.get("graph") == {"n": {"x": 10.0, "y": 20.0}}
    assert two.get("graph") is None  # per run
    two.save("graph", {"n": {"x": 1, "y": 2}})
    with closing(db) as conn:  # deleting a run deletes its layouts
        conn.execute("DELETE FROM runs WHERE id = 1")
    assert one.get("graph") is None and two.views() == ["graph"]


def test_recorded_run_saves_its_layout(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "multi_module", repo)
    app = create_app(home, testing=True)
    c = app.test_client()
    c.get("/repos")
    with c.session_transaction() as s:
        token = s["csrf"]
    resp = c.post("/repos", data={"source": "local", "path": str(repo), "csrf_token": token})
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    run_url = c.post(f"/repos/{repo_id}/discover", data={"csrf_token": token}).headers["Location"]
    run_id = int(run_url.split("/")[2])
    body = {"positions": {"module.worker.aws_lambda_function.this": {"x": 40, "y": 60}}}
    # the JSON API's CSRF guard applies
    assert c.post(run_url + "api/layout?view=graph", json=body).status_code == 403
    ok = c.post(run_url + "api/layout?view=graph", json=body, headers={"X-TFLens": "1"})
    assert ok.status_code == 200 and ok.get_json()["count"] == 1  # read-only run: still saved
    got = c.get(run_url + "api/layout?view=graph").get_json()
    assert got["positions"] == {"module.worker.aws_lambda_function.this": {"x": 40.0, "y": 60.0}}
    db = app.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        rows = conn.execute("SELECT run_id, view FROM run_layouts").fetchall()
    assert [(r["run_id"], r["view"]) for r in rows] == [(run_id, "graph")]
    reset = c.post(run_url + "api/layout/reset?view=graph", headers={"X-TFLens": "1"})
    assert reset.get_json()["reset"] is True
    assert c.get(run_url + "api/layout?view=graph").get_json()["positions"] is None
    assert not (repo / "tflens-out").exists()  # nothing written into the repository


# ---------------------------------------------------------------- 3. click and hover


def test_click_opens_the_pane_hover_is_a_short_tooltip():
    app = (STATIC / "app.js").read_text()
    tap = app.split("function canvasTap(t) {", 1)[1].split("\n  }\n", 1)[0]
    assert 'showDetail("node", d.detail)' in tap and 'showDetail("edge", d.edge)' in tap
    assert "showSource(d.code_ref)" in tap and "showStep(d, t)" in tap
    step = app.split("async function showStep(d, t) {", 1)[1].split("\n  }\n", 1)[0]
    assert "evidenceBlock(" in step and "api/source?file=" in step and "Unknowns (" in step
    # the Graph keeps today's behaviour (node / edge / group -> showDetail)
    assert 'else showDetail(t.isEdge() ? "edge" : "node", t.id());' in app
    js = (STATIC / "diagram.js").read_text()
    assert 'cy.on("mouseover", "node, edge"' in js and "showTip(evt)" in js
    for tip in ("function graphTip(t)", "function canvasTip(t)"):
        body = app.split(tip, 1)[1].split("\n  }\n", 1)[0]
        assert body.count("return [") >= 1 and "evidence" not in body  # short: title + a line


# ---------------------------------------------------------------- 4. typography


def test_typography_one_sans_15px_mono_for_code():
    css = (STATIC / "app.css").read_text()
    sans = re.search(r"--sans: ([^;]+);", css).group(1)
    assert sans.startswith("Inter, system-ui")
    assert re.search(r"body \{[^}]*font: 15px/1\.6 var\(--sans\)", css)
    assert re.search(r"code, kbd, pre, samp, \.mono \{ font-family: var\(--mono\); \}", css)
    for token in ("--fs-xl: 24px", "--fs-lg: 20px", "--fs-base: 15px", "--fs-sm: 13.5px"):
        assert token in css
    assert re.search(r"\nh1 \{ font-size: var\(--fs-xl\)", css)
    assert re.search(r"\nh2 \{ font-size: var\(--fs-lg\)", css)
    assert re.search(r"\nh3 \{ font-size: var\(--fs-sm\)", css)
    assert re.search(
        r"body \{ margin: 0; font: 15px/1\.6 Inter, system-ui", (STATIC / "style.css").read_text()
    )
    js = (STATIC / "diagram.js").read_text()
    assert 'themeVar("--sans", SANS)' in js  # the canvases use the page's font


def _hex(v: str) -> tuple[float, float, float]:
    v = v.lstrip("#")
    return tuple(int(v[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(v: str) -> float:
    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(c) for c in _hex(v))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _theme(css: str, selector: str) -> dict[str, str]:
    block = css.split(selector, 1)[1].split("}", 1)[0]
    return dict(re.findall(r"(--[\w-]+): (#[0-9a-fA-F]{6})", block))


def test_higher_contrast_text():
    theme = _theme((STATIC / "app.css").read_text(), ":root {")
    for fg in ("--fg", "--fg-muted", "--fg-faint"):
        assert _contrast(theme[fg], theme["--bg-panel"]) >= 7.0, fg  # WCAG AAA


# ---------------------------------------------------------------- 5. colours, icons, legend


def test_same_service_colour_and_icon_in_graph_diagrams_and_tables(tmp_path):
    api = _api(tmp_path, "app_flows")
    graph = {n["data"]["id"]: n["data"] for n in _get(api, "/api/graph")["nodes"]}
    icons = _get(api, "/api/icons")
    seen = 0
    for name in ("architecture", *[c["id"] for c in _get(api, "/api/flow")["chains"]]):
        for n in _get(api, f"/api/canvas?name={name}")["nodes"]:
            g = graph.get(n["data"]["id"])
            if g is None or "frame" in n.get("classes", ""):
                continue
            seen += 1
            assert n["data"]["color"] == g["color"] == service_colour(g["service"])
            assert n["data"]["icon"] == g["icon"]
            style = icons["types"][g["type"]]
            assert style == {"service": g["service"], "colour": g["color"], "icon": g["icon"]}
    assert seen
    assert set(icons["icons"]) == set(ICONS)
    assert icons["inventory"]["lambda"] == icons["types"]["aws_lambda_function"]
    assert icons["kinds"] == KIND_COLOURS
    assert all(KIND_COLORS[k] == KIND_COLOURS[k] for k in KIND_COLORS)
    app = (STATIC / "app.js").read_text()
    assert "svcLabel(iconsData && iconsData.inventory[row.key], row.label)" in app
    assert "svcLabel(typeStyle(r.type), r.type)" in app and "ch.prepend(svcIcon(st))" in app
    assert 'TFDiagram.iconUrl(n.data("icon"))' in app  # the Graph's nodes show the icon


def test_icons_are_self_contained_svgs():
    for name in ICONS:
        root = ET.fromstring(icon_svg(name))
        assert root.tag.endswith("svg") and "<script" not in icon_svg(name)
    assert node_icon("aws_lambda_function") == "lambda"
    assert node_icon("aws_iam_policy", "iam") == "iam"  # by service when the type has none
    assert node_icon("aws_unknown_thing", "nothing") == "generic"


def test_legend_toggles_edge_types():
    js = (STATIC / "diagram.js").read_text()
    for kind in ("TRIGGER", "ACCESS", "NETWORK", "CONFIG"):
        assert f"{kind}: " in js
    assert 'e.toggleClass("kind-off", state.off.has(e.data("kind")))' in js
    assert '{ selector: ".kind-off", style: { display: "none" } }' in js
    app = (STATIC / "app.js").read_text()
    # the Graph's legend and its sidebar "Edge kinds" are one filter
    assert 'isOn: (k) => checked("filter-kinds").has(k)' in app
    assert "if (graphCanvas) graphCanvas.refreshLegend();" in app


def test_canvas_legend_lists_edge_types_in_order(tmp_path):
    data = canvas_view(_api(tmp_path, "app_flows").store.model(), "architecture")
    kinds = [k["kind"] for k in data["kinds"]]
    assert kinds == [k for k in ("TRIGGER", "ACCESS", "NETWORK") if k in kinds]
    assert all(k["colour"] == KIND_COLOURS[k["kind"]] for k in data["kinds"])


# ---------------------------------------------------------------- 6. dark mode selection


def test_selected_node_is_legible_in_dark_mode():
    css = (STATIC / "app.css").read_text()
    dark = _theme(css, ":root {")
    assert _luminance(dark["--sel-bg"]) < 0.2  # a dark fill
    assert _contrast(dark["--sel-bg"], dark["--sel-fg"]) >= 4.5
    assert not re.search(
        r"fill: #[0-9a-f]{6}",
        css.split(
            "/* ---------------------------------------------------------------- diagrams */", 1
        )[1],
    )
    assert ".diagram-svg [data-node].selected > rect:first-of-type { fill: var(--sel-bg); }" in css
    assert ".diagram-svg [data-node].selected > text { fill: var(--sel-fg); }" in css
    js = (STATIC / "diagram.js").read_text()
    sel = js.split('{ selector: "node:selected", style: {', 1)[1].split("} },", 1)[0]
    assert '"background-color": selBg' in sel and "color: selFg" in sel
    app = (STATIC / "app.js").read_text()
    sel = app.split('{ selector: "node:selected", style: {', 1)[1].split("} },", 1)[0]
    assert '"text-background-color": selBg' in sel and 'themeColor("--sel-fg", text)' in sel


# ---------------------------------------------------------------- exports (diagram.js under node)

SAMPLE_VIEW = {
    "title": "Example diagram",
    "groups": [
        {
            "id": "frame:root:.",
            "parent": None,
            "x": 0,
            "y": 0,
            "w": 400,
            "h": 300,
            "label": "root ./",
            "color": "#7340b3",
            "dashed": False,
        },
        {
            "id": "frame:module:module.worker",
            "parent": "frame:root:.",
            "x": 20,
            "y": 40,
            "w": 360,
            "h": 200,
            "label": "worker · module",
            "color": "#6e7781",
            "dashed": True,
        },
    ],
    "nodes": [
        {
            "id": "aws_sqs_queue.jobs",
            "parent": "frame:module:module.worker",
            "x": 40,
            "y": 80,
            "w": 46,
            "h": 46,
            "shape": "icon",
            "label": "example-jobs",
            "sub": "worker · SQS queue",
            "color": "#d6337a",
            "stroke": "#d6337a",
            "icon": "data:image/svg+xml;base64,PHN2Zy8+",
        },
        {
            "id": "aws_lambda_function.fn",
            "parent": "frame:module:module.worker",
            "x": 240,
            "y": 80,
            "w": 220,
            "h": 70,
            "shape": "box",
            "label": "example-fn & <co>",
            "sub": "Lambda function",
            "color": "#e8710a",
            "stroke": "#8250df",
            "icon": None,
        },
    ],
    "edges": [
        {
            "id": "TRIGGER:aws_sqs_queue.jobs->aws_lambda_function.fn",
            "source": "aws_sqs_queue.jobs",
            "target": "aws_lambda_function.fn",
            "points": [[86, 103], [160, 103], [160, 115], [240, 115]],
            "color": "#ff7f0e",
            "dash": "dashed",
            "label": "event source",
            "reverse": False,
        },
    ],
}


def _node_export(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "export.js"
    script.write_text(
        "const d = require(process.argv[2]);\n"
        "const view = JSON.parse(process.argv[3]);\n"
        "process.stdout.write(JSON.stringify({svg: d.toSvg(view), drawio: d.toDrawio(view),"
        " snap: [d.snap(29), d.snap(31), d.snap(-11)]}));\n"
    )
    out = subprocess.run(
        [node, str(script), str(STATIC / "diagram.js"), json.dumps(SAMPLE_VIEW)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


def test_page_scripts_parse():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for name in ("app.js", "diagram.js", "discover.js", "forms.js", "localtime.js"):
        done = subprocess.run(
            [node, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60
        )
        assert done.returncode == 0, (name, done.stderr)


def test_canvases_load_into_cytoscape(tmp_path):
    """Every canvas payload (and the Graph) is accepted by the vendored Cytoscape.js, with
    the compound frames nested as sent and the orthogonal edge style."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    api = _api(tmp_path, "app_flows")
    payloads = [_get(api, f"/api/canvas?name={n}") for n in ("architecture", "module-tree")]
    payloads += [_get(api, f"/api/canvas?name={c['id']}") for c in _get(api, "/api/flow")["chains"]]
    g = _get(api, "/api/graph")
    for n in g["nodes"]:
        n["data"]["parent"] = n["data"]["group"] or n["data"]["root_group"]
    graph_nodes = (
        g["root_groups"]
        + [
            {**x, "data": {**x["data"], "parent": x["data"]["parent"] or x["data"]["root_group"]}}
            for x in g["groups"]
        ]
        + g["nodes"]
    )
    payloads.append({"nodes": graph_nodes, "edges": g["edges"]})
    data = tmp_path / "payloads.json"
    data.write_text(json.dumps(payloads))
    script = tmp_path / "load.js"
    script.write_text(
        "const cytoscape = require(process.argv[2]);\n"
        "const all = JSON.parse(require('fs').readFileSync(process.argv[3], 'utf8'));\n"
        "const out = all.map((p) => {\n"
        "  const cy = cytoscape({ headless: true, elements: { nodes: p.nodes, edges: p.edges },\n"
        "    style: [{ selector: 'edge', style: { 'curve-style': 'taxi', 'taxi-direction': 'auto' } }] });\n"
        "  return { nodes: cy.nodes().length, edges: cy.edges().length,\n"
        "    parents: cy.nodes().filter((n) => n.isParent()).length,\n"
        "    orphans: cy.nodes().filter((n) => n.data('parent') && !n.parent().length).length };\n"
        "});\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    done = subprocess.run(
        [node, str(script), str(STATIC / "vendor" / "cytoscape.min.js"), str(data)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    for p, got in zip(payloads, json.loads(done.stdout), strict=True):
        assert got["nodes"] == len(p["nodes"]) and got["edges"] == len(p["edges"])
        assert got["orphans"] == 0
        assert got["parents"] == len({n["data"].get("parent") for n in p["nodes"]} - {None})


def test_svg_export_of_what_is_drawn(tmp_path):
    out = _node_export(tmp_path)
    assert out["snap"] == [20, 40, -20]
    svg = ET.fromstring(out["svg"])
    ns = {"s": "http://www.w3.org/2000/svg"}
    assert svg.find("s:title", ns).text == "Example diagram"
    assert len(svg.findall(".//s:g[@class='frame']", ns)) == 2
    assert {g.get("data-id") for g in svg.findall(".//s:g[@class='node']", ns)} == {
        "aws_sqs_queue.jobs",
        "aws_lambda_function.fn",
    }
    path = svg.find(".//s:path[@class='edge']", ns)
    assert path.get("stroke") == "#ff7f0e" and path.get("stroke-dasharray") == "7 4"
    assert path.get("d").count("L") == 3  # the orthogonal bends are kept
    texts = [t.text for t in svg.iter("{http://www.w3.org/2000/svg}text")]
    assert "example-fn & <co>" in texts and "event source" in texts


def test_drawio_export_keeps_frames_icons_and_orthogonal_edges(tmp_path):
    out = _node_export(tmp_path)
    root = ET.fromstring(out["drawio"].split("\n", 1)[1])
    assert root.tag == "mxfile"
    cells = {c.get("value"): c for c in root.iter("mxCell") if c.get("value") is not None}
    outer, inner = cells["root ./"], cells["worker · module"]
    assert "container=1" in outer.get("style") and inner.get("parent") == outer.get("id")
    geo = inner.find("mxGeometry")
    assert (geo.get("x"), geo.get("y")) == ("20", "40")  # relative to its frame
    icon = next(c for c in root.iter("mxCell") if "shape=image" in (c.get("style") or ""))
    assert "image=data:image/svg+xml,PHN2Zy8+" in icon.get("style")  # no ";base64"
    assert icon.get("parent") == inner.get("id")
    edge = next(c for c in root.iter("mxCell") if c.get("edge") == "1")
    assert "edgeStyle=orthogonalEdgeStyle" in edge.get("style") and "dashed=1" in edge.get("style")
    assert edge.get("source") == icon.get("id")
    assert len(edge.findall(".//mxPoint")) == 2  # the bends between the two ends
