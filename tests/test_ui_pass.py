"""The run pages' UI/UX pass: the Graph renders on large repositories, every node of the
Graph and of the Diagrams opens its code, labels never show a bare ``this``, the
environment matrix is an interactive table, the Overview shows confirmed results only, the
Root view has one card per stack, and the header is a compact summary bar."""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest
from conftest import FIXTURES
from large_repo import write_large_repo

from tflens.cards import stack_cards
from tflens.flow import build_flows
from tflens.graphlayout import CELL_H, CELL_W
from tflens.icons import SERVICE_COLOURS, service_colour
from tflens.labels import GENERIC_NAMES, node_label
from tflens.output import run_analysis
from tflens.visuals import architecture_graph, env_matrix_table, module_tree, svg
from tflens.webapi import Api, ModelStore

STATIC = Path(__file__).resolve().parent.parent / "src" / "tflens" / "static"
NODE_SIZE = 26  # the Graph's node diameter (app.js graphStyle)
MIN_READABLE_ZOOM = 0.55  # app.js: below it labels are unreadable


def _get(api: Api, target: str):
    resp = api.handle("GET", target)
    assert resp.status == 200, (target, resp.body[:300])
    return json.loads(resp.body)


@pytest.fixture(scope="module")
def large(tmp_path_factory):
    root = write_large_repo(tmp_path_factory.mktemp("large") / "repo", modules=40)
    api = Api(ModelStore(root, root.parent / "model.json"))
    return api, _get(api, "/api/graph")


# ---------------------------------------------------------------- 1. large graph renders


def test_large_graph_has_every_element_cytoscape_needs(large):
    """A 300+ node graph: every edge end, every compound parent exists, ids are unique
    (Cytoscape refuses / drops anything else, leaving holes or a blank canvas)."""
    _, g = large
    assert len(g["nodes"]) >= 300
    assert g["dropped_edges"] == []
    ids = [n["data"]["id"] for n in g["nodes"]]
    groups = [x["data"]["id"] for x in g["groups"] + g["root_groups"]]
    structure = [s["data"]["id"] for s in g["structure"]["nodes"]]
    edges = [e["data"]["id"] for e in g["edges"] + g["structure"]["edges"]]
    every = ids + groups + edges
    assert len(every) == len(set(every)), "duplicate element ids"
    assert not set(structure) & set(ids + groups)
    nodes = set(ids)
    for e in g["edges"]:
        assert e["data"]["source"] in nodes and e["data"]["target"] in nodes
    # grouped: node -> module group -> root group; every parent exists
    parents = set(groups)
    for n in g["nodes"]:
        d = n["data"]
        assert d["group"] is None or d["group"] in parents
        assert d["root_group"] in parents
    for grp in g["groups"]:
        assert grp["data"]["parent"] is None or grp["data"]["parent"] in parents
        assert grp["data"]["root_group"] in parents
    # ungrouped: every CONTAINS edge joins two drawn elements
    drawn = nodes | set(structure)
    for e in g["structure"]["edges"]:
        assert e["data"]["source"] in drawn and e["data"]["target"] in drawn


def test_large_graph_layout_is_compact_and_readable(large):
    """The positions the page draws (preset layout): one finite, distinct position per
    node, nodes never overlap, and each module group fits a screen at a readable zoom
    (the cause of the blank canvas: a force layout fitted whole made it sub-pixel)."""
    _, g = large
    pos = g["positions"]
    ids = [n["data"]["id"] for n in g["nodes"]] + [s["data"]["id"] for s in g["structure"]["nodes"]]
    for i in ids:
        assert i in pos, i
        assert math.isfinite(pos[i]["x"]) and math.isfinite(pos[i]["y"])
    points = [(pos[n["data"]["id"]]["x"], pos[n["data"]["id"]]["y"]) for n in g["nodes"]]
    assert len(set(points)) == len(points)
    closest = min(math.dist(a, b) for i, a in enumerate(points) for b in points[i + 1 :])
    assert closest >= NODE_SIZE * 2  # never on top of each other, room for the label
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    assert width > 0 and height > 0
    # packed: no more than ~3 grid cells of area per node (no sprawling force layout)
    assert width * height / len(points) <= 3 * CELL_W * CELL_H
    # a module group (8 nodes) is visible whole on a 1280x800 screen at a readable zoom
    by_group: dict[str, list[tuple[float, float]]] = {}
    for n in g["nodes"]:
        if n["data"]["group"]:
            by_group.setdefault(n["data"]["group"], []).append(
                (pos[n["data"]["id"]]["x"], pos[n["data"]["id"]]["y"])
            )
    assert len(by_group) == 40
    for members in by_group.values():
        w = max(p[0] for p in members) - min(p[0] for p in members) + CELL_W
        h = max(p[1] for p in members) - min(p[1] for p in members) + CELL_H
        assert w * MIN_READABLE_ZOOM <= 1280 and h * MIN_READABLE_ZOOM <= 800


def test_large_graph_is_deterministic_and_served(large):
    api, g = large
    assert _get(api, "/api/graph") == g


def test_graph_page_draws_server_positions_and_reports_errors():
    """The page draws the server's positions (no force-directed layout on the page's
    thread), keeps a readable zoom, and a failure to draw is shown and re-thrown."""
    js = (STATIC / "app.js").read_text()
    assert 'name: "cose"' not in js
    assert 'layout: { name: "preset", fit: false }' in js
    assert "graphData.positions" in js and "pos[n.id()]" in js
    assert "const MIN_READABLE_ZOOM = 0.55;" in js
    assert "cy.zoom(MIN_READABLE_ZOOM)" in js
    assert "graphError(container, err);\n      throw err;" in js
    assert 'label: "data(display)"' in js
    assert "Nothing to draw with these filters" in js


# ---------------------------------------------------------------- 3. labels


def _labels_ok(labels):
    for label in labels:
        assert label, "empty label"
        assert label.lower() not in GENERIC_NAMES, label
        assert not re.fullmatch(r"(this|these|self|it)(\b.*)?", label), label


def test_labels_are_module_call_and_type(large):
    _, g = large
    _labels_ok(n["data"]["display"] for n in g["nodes"])
    fn = next(
        n["data"] for n in g["nodes"] if n["data"]["id"] == "module.svc_07.aws_lambda_function.this"
    )
    assert fn["display"] == "svc_07 · Lambda"
    # module groups: the call's name; the full address stays in the pane
    grp = next(x["data"] for x in g["groups"] if x["data"]["module"] == "module.svc_07")
    assert grp["label"] == "svc_07 · module" and grp["node"] == "module.svc_07"
    assert (
        node_label(
            {"kind": "resource", "type": "aws_instance", "name": "this", "module": "module.app"}
        )
        == "app · EC2"
    )
    assert (
        node_label(
            {"kind": "resource", "type": "aws_instance", "name": "this", "module": ""}, "stacks/web"
        )
        == "web · EC2"
    )


def test_detail_pane_has_label_and_full_address(large):
    api, _ = large
    d = _get(api, "/api/node?id=" + quote("module.svc_07.aws_sqs_queue.this"))
    assert d["title"] == "module.svc_07.aws_sqs_queue.this"  # full address
    assert d["summary"]["label"] == "svc_07 · SQS"
    assert d["defined_by"]["file"] == "modules/svc/main.tf" and d["defined_by"]["lines"]


@pytest.mark.parametrize("fixture", ["multi_module", "multi_root_envs", "app_flows", "mixed"])
def test_no_bare_this_in_graph_architecture_or_flows(fixture):
    m = run_analysis(FIXTURES / fixture, git=False).model
    api = Api(ModelStore(FIXTURES / fixture, Path("/nonexistent/model.json")))
    api.store._set(m, None, generated=True)
    g = _get(api, "/api/graph")
    _labels_ok(n["data"]["display"] for n in g["nodes"])
    _labels_ok(x["data"]["label"] for x in g["groups"] + g["root_groups"])
    arch = architecture_graph(m)
    _labels_ok(n["label"] for n in arch["nodes"])
    _labels_ok(n["sub_label"] for n in arch["nodes"])
    _labels_ok(n["label"] for c in build_flows(m)["chains"] for n in c["nodes"])


def test_large_repo_flows_and_architecture_never_say_this(large):
    api, _ = large
    m = api.store.model()
    _labels_ok(n["label"] for c in build_flows(m)["chains"] for n in c["nodes"])
    arch = architecture_graph(m)
    _labels_ok(n["label"] for n in arch["nodes"])
    assert any(n["sub_label"].startswith("svc_01 · ") for n in arch["nodes"])


def test_containment_resource_to_module_to_root(large):
    _, g = large
    contains = [(e["data"]["source"], e["data"]["target"]) for e in g["structure"]["edges"]]
    assert all(e["data"]["kind"] == "CONTAINS" for e in g["structure"]["edges"])
    into: dict[str, list[str]] = {}
    for src, dst in contains:
        into.setdefault(dst, []).append(src)
    for n in g["nodes"]:
        d = n["data"]
        if d["module"] != "(root)":
            assert into[d["id"]] == [d["module"]], d["id"]
    for i in range(1, 41):
        assert into[f"module.svc_{i:02d}"] == ["root:."]
    kinds = {s["data"]["kind"] for s in g["structure"]["nodes"]}
    assert kinds == {"root", "module"}
    # grouped: the module groups nest in the root's compound
    assert [x["data"]["id"] for x in g["root_groups"]] == ["group:root:."]
    assert {x["data"]["root_group"] for x in g["groups"]} == {"group:root:."}


# ---------------------------------------------------------------- 2. every node clickable


def _store(fixture: str) -> tuple[Api, dict]:
    m = run_analysis(FIXTURES / fixture, git=False).model
    api = Api(ModelStore(FIXTURES / fixture, Path("/nonexistent/model.json")))
    api.store._set(m, None, generated=True)
    return api, m


def _opens(api: Api, node_id: str) -> dict:
    d = _get(api, "/api/node?id=" + quote(node_id, safe=""))
    assert {"defined_by", "evidence", "unknowns", "title"} <= set(d)
    return d


@pytest.mark.parametrize("fixture", ["multi_module", "multi_root_envs", "app_flows"])
def test_every_graph_node_group_and_root_opens_the_pane(fixture):
    api, _ = _store(fixture)
    g = _get(api, "/api/graph")
    for n in g["nodes"]:
        _opens(api, n["data"]["id"])
    for grp in g["groups"] + g["root_groups"]:
        d = _opens(api, grp["data"]["node"])
        assert d["defined_by"] is None or d["defined_by"]["lines"]
    for s in g["structure"]["nodes"]:
        _opens(api, s["data"]["id"])


@pytest.mark.parametrize(
    "fixture", ["multi_module", "multi_root_envs", "app_flows", "multi_stack_envs"]
)
def test_every_diagram_node_opens_code_evidence_and_unknowns(fixture):
    """Architecture boxes and module-tree rows (roots and module calls) carry data-node;
    each opens file:line with its snippet, its evidence and unknowns."""
    api, m = _store(fixture)
    for name in ("architecture", "module-tree"):
        text = api.handle("GET", f"/api/diagram?name={name}&format=svg").body.decode()
        assert text == svg(m, name, api.store.title)
        ids = re.findall(r'<g class="node" data-node="([^"]+)">', text)
        if name == "module-tree":
            assert len(ids) == len(module_tree(m))
        else:
            assert len(ids) == len(architecture_graph(m)["nodes"])
        for nid in ids:
            d = _opens(api, nid.replace("&amp;", "&"))
            if d["defined_by"]:
                assert d["defined_by"]["file"] and d["defined_by"]["start_line"]
                assert d["defined_by"]["lines"]
    rows = module_tree(m)
    assert any(r["node"].startswith("root:") for r in rows)
    if any(r["parent"] for r in rows):
        assert any(not r["node"].startswith("root:") for r in rows)


def test_module_and_root_nodes_show_their_code():
    api, _ = _store("multi_module")
    mod = _opens(api, "module.worker")
    assert mod["summary"]["kind"] == "module" and mod["defined_by"]["file"] == "main.tf"
    root = _opens(api, "root:.")
    assert root["summary"]["kind"] == "root" and root["title"] == "root ./"
    assert {m["address"] for m in root["modules"]} >= {"module.worker", "module.storage"}
    assert root["defined_by"]["lines"]


def test_diagrams_page_inlines_clickable_svgs():
    js = (STATIC / "app.js").read_text()
    assert 'const INTERACTIVE = new Set(["architecture", "module-tree"]);' in js
    assert 'diagramPanel(name, title, "", true)' in js
    assert 'querySelectorAll("[data-node]")' in js and 'showDetail("node", g.dataset.node)' in js


# ---------------------------------------------------------------- 4. environment matrix


def test_env_matrix_is_a_table_of_backends():
    api, m = _store("multi_stack_envs")
    mx = _get(api, "/api/env-matrix")
    assert mx == env_matrix_table(m)
    assert [c["name"] for c in mx["columns"]] == ["dev", "uat", "prod"]
    rows = {r["stack"]: r for r in mx["rows"]}
    assert "modules/legacy_fn" not in rows  # a library is not deployed: no row
    cell = rows["stacks/api"]["cells"]["dev"]
    assert cell["type"] == "s3"
    assert cell["bucket"] == "example-tfstate-dev" and cell["key"] == "api/terraform.tfstate"
    assert cell["location"]["file"] == "stacks/api/envs/backend-dev.hcl"
    assert rows["stacks/api"]["node"] == "root:stacks/api"
    # the cell's backend file opens in the pane
    loc = cell["location"]
    src = _get(
        api,
        f"/api/source?file={quote(loc['file'])}&start={loc['start_line']}&end={loc['end_line']}",
    )
    assert any("example-tfstate-dev" in ln["text"] for ln in src["lines"])


def test_env_matrix_page_is_interactive_not_an_image():
    js = (STATIC / "app.js").read_text()
    assert 'if (name === "env-matrix") body = envMatrixBox();' in js
    for hook in (
        'api("api/env-matrix")',
        'class: "matrix-filter"',
        'class: "th-sort"',
        "showBackendCell(row, env, cell)",
        "state.hidden",
    ):
        assert hook in js, hook
    css = (STATIC / "app.css").read_text()
    assert ".cell-btn .bk-key" in css and "overflow-wrap: anywhere" in css


# ---------------------------------------------------------------- 5. overview


def test_overview_default_view_is_confirmed_counts_only():
    js = (STATIC / "app.js").read_text()
    body = js.split("function renderOverview(ov, envs) {", 1)[1].split("\n  }\n", 1)[0]
    default, details = body.split("const details = [];", 1)
    assert "confirmedColumns(envs)" in default and "stackCountsTable(envs, cols)" in default
    assert "Purpose (guess)" not in default and "ov.roots" not in default
    assert 'dsec("ov-purpose", "Purpose (guess)"' in details
    assert 'el("details", { class: "ov-details", id: "ov-details" }' in details


# ---------------------------------------------------------------- 6. stack cards


def test_stack_cards_carry_envs_backend_resources_and_modules():
    api, m = _store("multi_stack_envs")
    cards = {c["id"]: c for c in _get(api, "/api/stack-cards")}
    assert set(cards) == {"stacks/api", "stacks/edge", "stacks/worker"}  # no library
    api_card = cards["stacks/api"]
    assert [e["name"] for e in api_card["environments"]] == ["dev", "prod", "uat"]
    assert api_card["backend"]["per_env"]["prod"]["bucket"] == "example-tfstate-prod"
    assert api_card["purpose"]["text"] is None  # no README of its own: nothing guessed
    assert {"address": "module.handlers"}.items() <= api_card["modules"][0].items()
    assert api_card["resource_total"] == sum(r["count"] for r in api_card["resources"])


def test_stack_card_purpose_only_from_its_own_readme(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "multi_stack_envs", repo)
    (repo / "stacks" / "worker" / "README.md").write_text(
        "# Worker\n\nProcesses the example job queue for the platform.\n"
    )
    m = run_analysis(repo, git=False).model
    cards = {c["id"]: c for c in stack_cards(m)}
    p = cards["stacks/worker"]["purpose"]
    assert p["text"] == "Processes the example job queue for the platform."
    assert p["status"] == "confirmed" and p["location"]["file"] == "stacks/worker/README.md"
    assert cards["stacks/edge"]["purpose"]["text"] is None  # the top README is not its own
    m["pipeline"] = {
        "validation": {"stacks": [{"name": "stacks/worker", "status": "hint-only"}], "envs": []}
    }
    cards = {c["id"]: c for c in stack_cards(m)}
    assert cards["stacks/worker"]["purpose"]["text"] is None  # not confirmed: not shown


def test_stack_cards_remote_state_inbound_and_outbound():
    _, m = _store("mixed")
    cards = {c["id"]: c for c in stack_cards(m)}
    out = cards["services/app"]["outbound"]
    assert [(lk["from_stack"], lk["to_stack"]) for lk in out] == [
        ("services/app", "stacks/platform")
    ]
    assert [lk["from_stack"] for lk in cards["stacks/platform"]["inbound"]] == ["services/app"]


def test_root_view_renders_the_cards():
    js = (STATIC / "app.js").read_text()
    assert 'api("api/stack-cards")' in js and "...stackCards(), shapesSummary(shapesData)" in js
    for field in (
        "c.environments",
        "c.backend.per_env",
        "c.resources",
        "c.modules",
        "c.outbound",
        "c.inbound",
        "p.text",
    ):
        assert field in js, field


# ---------------------------------------------------------------- 7. colours and themes


def test_service_colours_are_fixed_and_shared():
    other, _ = _store("multi_module")
    api, _ = _store("app_flows")
    g1 = _get(other, "/api/graph")
    g2 = _get(api, "/api/graph")
    for s in set(g1["colors"]["services"]) & set(g2["colors"]["services"]):
        assert g1["colors"]["services"][s] == g2["colors"]["services"][s]
    assert (
        g2["colors"]["services"]["lambda"] == SERVICE_COLOURS["lambda"] == service_colour("lambda")
    )
    assert f'stroke="{SERVICE_COLOURS["lambda"]}"' in svg(api.store.model(), "architecture")


def test_light_and_dark_themes():
    css = (STATIC / "app.css").read_text()
    assert ':root[data-theme="dark"]' in css and "@media (prefers-color-scheme: dark)" in css
    assert re.search(r"body \{[^}]*font: 15px", css)  # the 15px base (interactive diagrams pass)
    js = (STATIC / "app.js").read_text()
    assert 'themeColor("--fg"' in js and "cy.style(graphStyle())" in js


# ---------------------------------------------------------------- 8. header summary bar


def test_header_is_a_compact_summary_bar():
    html = (STATIC / "index.html").read_text()
    assert '<div id="summary-details" class="summary-details" hidden></div>' in html
    js = (STATIC / "app.js").read_text()
    body = js.split("function renderSummaryBar(summary, envs) {", 1)[1].split("\n  }\n", 1)[0]
    for label in ('"envs"', '"stacks"', '"resources"', '"unknowns"'):
        assert label in body, label
    assert '"aria-controls": "summary-details"' in body and "panel.hidden = !open" in body
    assert 'document.getElementById("summary").textContent' not in js  # no wall of counts
