"""Flow / diagram follow-ups. Fixture: flow_followups (lambda-a fed by an EventBridge rule
and by queue-a, which the no-trigger lambda-b writes to; lambda-a reads and writes
table-a; a VPC, subnet and group with two rules from one CIDR).

* a compute is expanded in exactly one flow; every other flow that reaches it ends in a
  "continues in Flow N" link;
* steps between the same two nodes are drawn in lanes of their own, each labelled;
* the Network diagram puts what is outside the VPC in the leftmost column, and its
  containment lines never cross a box.
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
from tflens.tabdiagrams import EXTERNAL, VPC, network_diagram, tab_svg

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


# -- 3. the Network diagram's layout ---------------------------------------------------------


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


def test_external_cidr_is_the_leftmost_column():
    dg = network_diagram(_model(FIXTURE))
    depth = {n["label"]: n["depth"] for n in dg["nodes"]}
    assert depth == {
        "192.0.2.0/24": EXTERNAL,
        "vpc-a": VPC,
        "subnet-a": 2,
        "sg-a": 3,
        "lambda-a": 4,
    }
    assert EXTERNAL < VPC
    svg = tab_svg(_model(FIXTURE), "network")
    x = {
        name: int(m.group(1))
        for name in depth
        if (m := re.search(rf'<rect x="(\d+)"[^>]*/><text[^>]*>{re.escape(name)}</text>', svg))
    }
    assert x["192.0.2.0/24"] < x["vpc-a"] < x["subnet-a"] < x["sg-a"] < x["lambda-a"]
    assert x["192.0.2.0/24"] == min(x.values())


def test_containment_lines_go_around_the_boxes():
    for fixture in (FIXTURE, "flows_per_compute"):
        svg = tab_svg(_model(fixture), "network")
        paths = _check_containment_clear_of_boxes(svg)
        if fixture == FIXTURE:
            # vpc-a -> sg-a and subnet-a -> lambda-a skip a column: along the bottom
            bottom = max(b[1] + b[3] for b in _boxes(svg))
            assert sum(1 for d in paths if max(s[1] for s in _segments(d)) > bottom) == 2
    iam = tab_svg(_model("flows_per_compute"), "iam")
    _check_containment_clear_of_boxes(iam)


def test_rule_lines_are_drawn_over_the_boxes():
    svg = tab_svg(_model(FIXTURE), "network")
    last_box = max(m.start() for m in re.finditer(r"<g><title>", svg))
    first_rule = svg.index('<path data-step="1"')
    last_contains = max(m.start() for m in re.finditer('class="contains"', svg))
    assert last_contains < last_box < first_rule


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
