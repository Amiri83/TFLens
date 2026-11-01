"""Flow / diagram follow-ups. Fixture: flow_followups (lambda-a fed by an EventBridge rule
and by queue-a, which the no-trigger lambda-b writes to; lambda-a reads and writes
table-a; a VPC, subnet and group with two rules from one CIDR).

* a compute is expanded in exactly one flow; every other flow that reaches it ends in a
  "continues in Flow N" link;
* steps between the same two nodes are drawn in lanes of their own, each labelled, and
  every step's label sits on its own edge;
* the Network diagram is three columns (outside | security groups | resources), its only
  lines the security group rules, none crossing a box; VPCs and subnets are frames, and
  each resource's box lists its subnets and groups.
"""

from __future__ import annotations

import re

from conftest import FIXTURES, _model

from tflens.flow import (
    COMPUTE_TYPES,
    build_flows,
    chain_svg,
    flow_svg,
    parallel_lanes,
    render_flows_markdown,
)
from tflens.tabdiagrams import EXTERNAL, GROUP, PLACED, SG_TYPES, network_diagram, tab_svg

FIXTURE = "flow_followups"
LAMBDA_A, LAMBDA_B = "aws_lambda_function.lambda-a", "aws_lambda_function.lambda-b"


def _flows(fixture=FIXTURE):
    return build_flows(_model(fixture))


def _chain(entry, fixture=FIXTURE):
    return next(c for c in _flows(fixture)["chains"] if c["entry"] == entry)


def _expanded(chains):
    """compute -> the flows it is expanded in (a step source, the start, or reached)."""
    out: dict[str, list[str]] = {}
    for c in chains:
        for n in c["nodes"]:
            if n["role"] in ("entry", "no_trigger", "compute") and _is_compute(n["id"]):
                out.setdefault(n["id"], []).append(c["id"])
    return out


def _is_compute(nid):
    return nid.split(".", 1)[0] in COMPUTE_TYPES


# -- 1. a compute is expanded once -----------------------------------------------------------


def test_compute_expanded_in_one_flow_others_link_to_it():
    chains = _flows()["chains"]
    assert [(c["title"], c["no_trigger"]) for c in chains] == [
        ("queue-a → lambda-a → table-a", False),
        ("lambda-b → queue-a → lambda-a (no trigger)", True),
        ("rule-a → lambda-a", False),
    ]
    assert _expanded(chains) == {LAMBDA_A: ["flow-1"], LAMBDA_B: ["flow-2"]}
    # lambda-a's own steps (sends to / reads from table-a) are in flow 1 only
    from_a = [c["id"] for c in chains for s in c["steps"] if s["source"] == LAMBDA_A]
    assert from_a == ["flow-1", "flow-1"]
    for c in chains[1:]:
        link = next(n for n in c["nodes"] if n["id"] == LAMBDA_A)
        assert link["role"] == "link" and link["label"] == "continues in Flow 1"
        assert link["continues"] == {"flow": "flow-1", "n": 1, "compute": "lambda-a"}
        assert link["code_ref"] == {"file": "main.tf", "start_line": 106, "end_line": 123}
        # the chain stops there: nothing after the link, no context of lambda-a
        assert not [s for s in c["steps"] if s["source"] == LAMBDA_A]
        assert "aws_iam_role.role-a" not in {n["id"] for n in c["nodes"]}
    assert chains[1]["steps"][-1]["text"] == (
        "2. queue-a triggers lambda-a (event source mapping main.tf:135-138)"
    )
    m = _flows()["metrics"]
    assert (m["compute_covered"], m["compute_total"]) == (2, 2)


def test_link_is_drawn_and_listed():
    md = render_flows_markdown(_flows())
    assert "- lambda-a: continues in Flow 1 (`main.tf:106-123`)" in md
    svg = chain_svg(_model(FIXTURE), "flow-2")
    assert ">continues in Flow 1</text>" in svg and ">main.tf:106-123</text>" in svg


def test_every_compute_expanded_exactly_once_in_the_other_fixtures():
    for fixture in ("flows_per_compute", "kafka_flow", "app_flows", "app_pipeline"):
        chains = _flows(fixture)["chains"]
        for nid, flows in _expanded(chains).items():
            assert len(flows) == 1, (fixture, nid, flows)
        for c in chains:
            for n in c["nodes"]:
                if n["role"] == "link":
                    assert n["code_ref"], (fixture, n)
                    assert n["id"] in _expanded(chains), (fixture, n)


# -- 2. parallel steps -----------------------------------------------------------------------


def _path(svg, n):
    m = re.search(rf'<path data-step="{n}"( data-lane="(\d+)")?[^>]* d="([^"]+)"', svg)
    assert m, n
    return m.group(2), [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", m.group(3))]


def _label_centre(svg, n):
    m = re.search(
        rf'<g data-step="{n}"[^>]*><title>[^<]*</title><rect x="(\d+)" y="(\d+)" '
        rf'width="(\d+)" height="(\d+)"',
        svg,
    )
    assert m, n
    x, y, w, h = (int(v) for v in m.groups())
    return x + w / 2, y + h / 2


def _mid(p):
    """The middle of a cubic ``M x1,y1 C c1 c2 x2,y2``."""
    (x1, y1, ax, ay, bx, by, x2, y2) = p
    return (x1 + 3 * ax + 3 * bx + x2) / 8, (y1 + 3 * ay + 3 * by + y2) / 8


def test_read_and_write_of_one_table_get_their_own_lanes():
    c = _chain("aws_sqs_queue.queue-a")
    pair = [
        s
        for s in c["steps"]
        if (s["source"], s["target"]) == (LAMBDA_A, "aws_dynamodb_table.table-a")
    ]
    assert [(s["n"], s["verb"]) for s in pair] == [(2, "sends to"), (3, "reads from")]
    assert parallel_lanes(c["steps"]) == {2: (0, 2), 3: (1, 2)}
    svg = chain_svg(_model(FIXTURE), c["id"])
    (lane2, p2), (lane3, p3) = _path(svg, 2), _path(svg, 3)
    assert (lane2, lane3) == ("0", "1")
    assert p2[1] != p3[1] and p2[-1] != p3[-1]  # apart where they leave and arrive
    (_, m2), (_, m3) = _mid(p2), _mid(p3)
    assert abs(m2 - m3) >= 40  # further apart in the middle
    # both labels, each on its own edge (closer to its own lane than to the other)
    assert ">2. sends to</text>" in svg and ">3. reads from</text>" in svg
    (_, l2), (_, l3) = _label_centre(svg, 2), _label_centre(svg, 3)
    assert abs(l2 - m2) < abs(l2 - m3) and abs(l3 - m3) < abs(l3 - m2)


def test_more_than_two_parallel_steps_are_each_distinct():
    steps = [{"n": i, "source": "a", "target": "b"} for i in (1, 2, 3, 4)]
    steps.append({"n": 5, "source": "b", "target": "a"})  # the other direction: alone
    assert parallel_lanes(steps) == {1: (0, 4), 2: (1, 4), 3: (2, 4), 4: (3, 4)}
    chain = {
        "n": 1,
        "title": "t",
        "nodes": [
            {"id": "a", "label": "a", "kind_label": "k", "depth": 0, "role": "entry"},
            {"id": "b", "label": "b", "kind_label": "k", "depth": 1, "role": "target"},
        ],
        "steps": [
            {
                "n": i,
                "source": "a",
                "target": "b",
                "kind": "trigger",
                "status": "",
                "verb": "triggers",
                "text": f"{i}. a triggers b",
            }
            for i in (1, 2, 3)
        ],
    }
    svg = flow_svg(chain)
    starts = [_path(svg, i)[1][1] for i in (1, 2, 3)]
    mids = [_mid(_path(svg, i)[1])[1] for i in (1, 2, 3)]
    assert len(set(starts)) == 3 and starts == sorted(starts)
    labels = [_label_centre(svg, i)[1] for i in (1, 2, 3)]
    assert len(set(labels)) == 3
    for i, ly in enumerate(labels):  # each label nearest its own edge
        assert min(range(3), key=lambda j: abs(mids[j] - ly)) == i


def test_two_rules_from_one_cidr_get_their_own_lanes():
    svg = tab_svg(_model(FIXTURE), "network")
    (l1, p1), (l2, p2) = _path(svg, 1), _path(svg, 2)
    assert (l1, l2) == ("0", "1") and p1[1] != p2[1]
    assert ">1. ingress tcp 443</text>" in svg and ">2. ingress tcp 80</text>" in svg


# -- 3. every label on its own edge ----------------------------------------------------------


def _label_rect(svg, n):
    m = re.search(
        rf'<g data-step="{n}"[^>]*><title>[^<]*</title><rect x="(-?\d+)" y="(-?\d+)" '
        rf'width="(\d+)" height="(\d+)"',
        svg,
    )
    assert m, n
    return tuple(int(v) for v in m.groups())


def _curve_points(p, k=200):
    """Points along a cubic ``M x1,y1 C c1 c2 x2,y2``."""
    x1, y1, ax, ay, bx, by, x2, y2 = p
    out = []
    for i in range(k + 1):
        t = i / k
        u = 1 - t
        out.append(
            (
                u**3 * x1 + 3 * u * u * t * ax + 3 * u * t * t * bx + t**3 * x2,
                u**3 * y1 + 3 * u * u * t * ay + 3 * u * t * t * by + t**3 * y2,
            )
        )
    return out


def _touches(rect, points, slack=2):
    x, y, w, h = rect
    return any(
        x - slack <= px <= x + w + slack and y - slack <= py <= y + h + slack for px, py in points
    )


def _check_labels_on_their_edges(svg):
    steps = sorted({int(n) for n in re.findall(r'<g data-step="(\d+)"', svg)})
    for n in steps:
        _, p = _path(svg, n)
        assert len(p) == 8, (n, p)  # a cubic
        assert _touches(_label_rect(svg, n), _curve_points(p)), n
    return steps


def test_parallel_labels_touch_their_own_lane_only():
    c = _chain("aws_sqs_queue.queue-a")
    svg = chain_svg(_model(FIXTURE), c["id"])
    (_, p2), (_, p3) = _path(svg, 2), _path(svg, 3)
    r2, r3 = _label_rect(svg, 2), _label_rect(svg, 3)
    # "3. reads from" on the reads-from lane, "2. sends to" on the sends-to lane
    assert _touches(r3, _curve_points(p3)) and not _touches(r3, _curve_points(p2), slack=0)
    assert _touches(r2, _curve_points(p2)) and not _touches(r2, _curve_points(p3), slack=0)
    # and within the span of the edge, between the two boxes
    for r, p in ((r2, p2), (r3, p3)):
        assert p[0] <= r[0] and r[0] + r[2] <= p[6]


def test_every_flow_label_touches_its_edge():
    for fixture in (FIXTURE, "flows_per_compute", "kafka_flow", "app_flows", "app_pipeline"):
        for c in _flows(fixture)["chains"]:
            svg = chain_svg(_model(fixture), c["id"])
            _check_labels_on_their_edges(svg)
    for fixture in (FIXTURE, "flows_per_compute"):
        for name in ("network", "iam"):
            _check_labels_on_their_edges(tab_svg(_model(fixture), name))


def test_more_than_two_parallel_labels_touch_their_own_edges():
    chain = {
        "n": 1,
        "title": "t",
        "nodes": [
            {"id": "a", "label": "a", "kind_label": "k", "depth": 0, "role": "entry"},
            {"id": "b", "label": "b", "kind_label": "k", "depth": 1, "role": "target"},
        ],
        "steps": [
            {
                "n": i,
                "source": "a",
                "target": "b",
                "kind": "read" if i % 2 else "send",
                "status": "verified",
                "verb": "reads from" if i % 2 else "sends to",
                "text": f"{i}. a b",
            }
            for i in (1, 2, 3, 4)
        ],
    }
    svg = flow_svg(chain)
    assert _check_labels_on_their_edges(svg) == [1, 2, 3, 4]
    assert flow_svg(chain) == svg


# -- 4. the Network diagram's layout ---------------------------------------------------------


def _boxes(svg):
    return [
        tuple(int(v) for v in m)
        for m in re.findall(
            r'<g><title>[^<]*</title><rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"', svg
        )
    ]


def _segments(d):
    """The straight segments of an ``M x,y H x V y ...`` path."""
    tokens = re.findall(r"([MHV])(-?\d+)(?:,(-?\d+))?", d)
    out, x, y = [], 0.0, 0.0
    for cmd, a, b in tokens:
        if cmd == "M":
            x, y = float(a), float(b)
            continue
        nx, ny = (float(a), y) if cmd == "H" else (x, float(a))
        out.append((x, y, nx, ny))
        x, y = nx, ny
    return out


def _crosses(seg, box):
    """Does an axis-parallel segment enter the inside of a box (touching its edge is fine)?"""
    x1, y1, x2, y2 = seg
    bx, by, bw, bh = box
    return min(x1, x2) < bx + bw and max(x1, x2) > bx and min(y1, y2) < by + bh and max(y1, y2) > by


def _check_containment_clear_of_boxes(svg):
    paths = re.findall(r'class="contains" d="([^"]+)"', svg)
    assert paths
    boxes = _boxes(svg)
    for d in paths:
        assert re.fullmatch(r"M-?\d+,-?\d+( [HV]-?\d+)+", d), d  # right angles only
        for seg in _segments(d):
            hit = [b for b in boxes if _crosses(seg, b)]
            assert not hit, (d, seg, hit)
    return paths


def _boxes_by_id(svg):
    return {
        nid: tuple(int(v) for v in rect)
        for nid, *rect in re.findall(
            r'<g><title>([^ <]+) [^<]*</title><rect x="(\d+)" y="(\d+)" width="(\d+)" '
            r'height="(\d+)"',
            svg,
        )
    }


def _frames(svg):
    return {
        fid: tuple(int(v) for v in rect)
        for fid, *rect in re.findall(
            r'<g class="frame" data-frame="([^"]+)"><title>[^<]*</title><rect x="(\d+)" '
            r'y="(\d+)" width="(\d+)" height="(\d+)"',
            svg,
        )
    }


def _inside(inner, outer):
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return ox < ix and oy < iy and ix + iw < ox + ow and iy + ih < oy + oh


def test_three_columns_outside_groups_resources():
    dg = network_diagram(_model(FIXTURE))
    depth = {n["label"]: n["depth"] for n in dg["nodes"]}
    assert depth == {"192.0.2.0/24": EXTERNAL, "sg-a": GROUP, "lambda-a": PLACED}
    assert (EXTERNAL, GROUP, PLACED) == (0, 1, 2)
    ids = {n["label"]: n["id"] for n in dg["nodes"]}
    boxes = _boxes_by_id(tab_svg(_model(FIXTURE), "network"))
    x = {name: boxes[ids[name]][0] for name in depth}
    assert x["192.0.2.0/24"] < x["sg-a"] < x["lambda-a"]
    # what is outside is lined up with the group it reaches
    assert boxes[ids["192.0.2.0/24"]][1] == boxes[ids["sg-a"]][1]


def test_vpc_and_subnets_are_frames_around_what_is_in_them():
    dg = network_diagram(_model(FIXTURE))
    assert [(f["id"], f["parent"]) for f in dg["frames"]] == [
        ("aws_vpc.vpc-a", None),
        ("aws_subnet.subnet-a", "aws_vpc.vpc-a"),
    ]
    svg = tab_svg(_model(FIXTURE), "network")
    boxes, frames = _boxes_by_id(svg), _frames(svg)
    vpc, subnet = frames["aws_vpc.vpc-a"], frames["aws_subnet.subnet-a"]
    sg, fn = boxes["aws_security_group.sg-a"], boxes[LAMBDA_A]
    cidr = next(b for nid, b in boxes.items() if nid not in (LAMBDA_A, "aws_security_group.sg-a"))
    assert _inside(subnet, vpc) and _inside(fn, subnet)
    assert _inside(sg, vpc) and not _inside(sg, subnet)
    assert cidr[0] + cidr[2] < vpc[0]  # outside is outside
    # each frame named, with its code reference; drawn under the boxes, rules over them
    assert ">vpc-a</text>" in svg and ">subnet-a</text>" in svg
    assert re.search(r">subnet · main\.tf:\d+-\d+</text>", svg)
    first_box = svg.index("<g><title>")
    assert svg.rindex('class="frame"') < first_box < svg.index('<path data-step="1"')
    # the resource's box says where it is and what guards it
    meta = {n["id"]: n["meta"] for n in dg["nodes"]}
    assert meta[LAMBDA_A] == "subnet-a / sg-a"
    assert ' class="meta">subnet-a / sg-a</text>' in svg
    assert fn[3] > sg[3]  # the box grows a line for it


def test_network_has_no_containment_steps_only_rule_lines():
    for fixture in (FIXTURE, "flows_per_compute"):
        model = _model(fixture)
        dg = network_diagram(model)
        edges = {e["id"]: e for e in model["edges"]}
        types = {n["id"]: n["type"] for n in model["nodes"]}
        assert dg["steps"], fixture
        for s in dg["steps"]:
            assert s["kind"] == "rule" and not s["quiet"], s
            assert s["verb"] not in ("contains", "has in it", "guards"), s
            e = edges[s["evidence"][0]["edge"]]
            assert e["kind"] == "NETWORK" and "direction" in e["attributes"], e
            assert types.get(s["source"]) in SG_TYPES or types.get(s["target"]) in SG_TYPES
        svg = tab_svg(model, "network")
        assert 'class="contains"' not in svg
        drawn = sorted({int(n) for n in re.findall(r'<path data-step="(\d+)"', svg)})
        assert drawn == [s["n"] for s in dg["steps"]]


def test_no_rule_line_passes_through_a_box():
    for fixture in (FIXTURE, "flows_per_compute"):
        svg = tab_svg(_model(fixture), "network")
        boxes = list(_boxes_by_id(svg).values())
        steps = sorted({int(n) for n in re.findall(r'<path data-step="(\d+)"', svg)})
        assert steps
        for n in steps:
            _, p = _path(svg, n)
            for px, py in _curve_points(p):
                hit = [b for b in boxes if b[0] < px < b[0] + b[2] and b[1] < py < b[1] + b[3]]
                assert not hit, (fixture, n, (px, py), hit)


def test_public_rules_keep_their_red():
    svg = tab_svg(_model("flows_per_compute"), "network")
    # 1 and 4 reach / come from 0.0.0.0/0; 2 and 3 do not
    for n, colour in ((1, "#cf222e"), (2, "#bc4c00"), (3, "#bc4c00"), (4, "#cf222e")):
        assert re.search(rf'<path data-step="{n}"[^>]* stroke="{colour}"', svg), n
    assert svg.count(">(public)</text>") == 2
    assert ">public rule</text>" in svg


def test_iam_containment_still_goes_around_the_boxes():
    _check_containment_clear_of_boxes(tab_svg(_model("flows_per_compute"), "iam"))


def test_deterministic():
    from tflens.output import run_analysis

    model = run_analysis(FIXTURES / FIXTURE).model
    assert build_flows(model) == _flows()
    for name in ("network", "iam"):
        assert tab_svg(model, name) == tab_svg(_model(FIXTURE), name)
    for c in _flows()["chains"]:
        assert chain_svg(model, c["id"]) == chain_svg(_model(FIXTURE), c["id"])


# -- 4. placeholders only --------------------------------------------------------------------

_ALLOWED = [
    re.compile(p)
    for p in (
        r"aws_[a-z0-9_]+",  # Terraform types (block labels), not names
        r"[a-z]+(-[a-z]+)*-([ab]|assume)(-data|-send)?",  # *-a style placeholder names
        r"192\.0\.2\.\d+/\d+",  # RFC 5737 documentation range
        r"[a-z0-9]+:[A-Za-z*]+",  # IAM actions
        r"lambda\.amazonaws\.com",
        r"123456789012",
    )
] + [
    re.compile(re.escape(s))
    for s in (
        "2012-10-17",
        "Allow",
        "Service",
        "tcp",
        "pk",
        "S",
        "PAY_PER_REQUEST",
        "python3.12",
        "index.handler",
        "lambda.zip",
        "rate(1 hour)",
    )
]


def test_fixture_uses_placeholder_names_only():
    text = (FIXTURES / FIXTURE / "main.tf").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    hits = [s for s in re.findall(r'"([^"]*)"', code) if not any(p.fullmatch(s) for p in _ALLOWED)]
    assert hits == []
    # identifiers (resource / data labels and references) are placeholders too
    labels = re.findall(r'^(?:resource|data) "aws_[a-z0-9_]+" "([^"]+)"', code, re.M)
    assert labels and all(_ALLOWED[1].fullmatch(lb) for lb in labels), labels
    assert not re.findall(r"\b\d{12}\b", text.replace("123456789012", ""))
    assert not re.findall(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d+", text)
