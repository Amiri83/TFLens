"""A flow for every compute (Lambda, ECS, EC2, ASG), deduped, grouped by type, with what
each runs in / as; the Root view's Network and IAM diagrams. Fixture: flows_per_compute
(7 compute resources, 3 of them reached from a declared trigger, a VPC with subnets,
security groups and rules, a role per compute)."""

from __future__ import annotations

import json
import re
import shutil

from conftest import FIXTURES, _model

from tflens.flow import build_flows, chain_svg, render_flows_markdown
from tflens.output import run_analysis
from tflens.shapes import build_root_view
from tflens.tabdiagrams import iam_diagram, iam_sections, network_diagram, tab_svg
from tflens.webapi import Api, ModelStore

FIXTURE = "flows_per_compute"
COMPUTES = {
    "aws_autoscaling_group.asg-a",
    "aws_ecs_service.ecs-service-a",
    "aws_instance.instance-a",
    "aws_lambda_function.lambda-classifier",
    "aws_lambda_function.lambda-idle",
    "aws_lambda_function.lambda-orphan",
    "aws_lambda_function.lambda-scheduled",
}


def _flows():
    return build_flows(_model(FIXTURE))


def _chain(entry):
    return next(c for c in _flows()["chains"] if c["entry"] == entry)


# -- 1. coverage -----------------------------------------------------------------------------


def test_every_compute_has_a_flow_deduped():
    fl = _flows()
    chains = fl["chains"]
    in_chains = [nid for c in chains for nid in (n["id"] for n in c["nodes"]) if nid in COMPUTES]
    assert set(in_chains) == COMPUTES  # each compute is in a flow ...
    entries = [c["entry"] for c in chains]
    assert len(entries) == len(set(entries))
    # ... and the ones a declared trigger reaches get no flow of their own
    triggered = {
        "aws_lambda_function.lambda-scheduled",  # EventBridge rule-a
        "aws_autoscaling_group.asg-a",  # lb-a -> tg-a
        "aws_lambda_function.lambda-classifier",  # queue-a, which asg-a writes to
    }
    assert not triggered & set(entries)
    assert [c["entry"] for c in chains if c["no_trigger"]] == [
        "aws_instance.instance-a",
        "aws_ecs_service.ecs-service-a",
        "aws_lambda_function.lambda-orphan",
        "aws_lambda_function.lambda-idle",
    ]
    m = fl["metrics"]
    assert (m["flows"], m["compute_covered"], m["compute_total"], m["no_trigger"]) == (7, 7, 7, 4)


def test_order_is_longest_first_then_name():
    assert [c["title"] for c in _flows()["chains"]] == [
        "lb-a → tg-a → asg-a → queue-a → lambda-classifier → table-a",
        # lambda-classifier is expanded in flow 1: here a link to it
        "instance-a → queue-a → lambda-classifier (no trigger)",
        "rule-a → lambda-scheduled",
        "ecs-service-a (no trigger)",
        "lambda-orphan → queue-b (no trigger)",
        "lb-c → tg-c",
        "lambda-idle (no trigger)",
    ]
    ec2 = _chain("aws_instance.instance-a")
    link = next(n for n in ec2["nodes"] if n["id"] == "aws_lambda_function.lambda-classifier")
    assert (link["role"], link["label"]) == ("link", "continues in Flow 1")
    assert link["code_ref"] == {"file": "main.tf", "start_line": 173, "end_line": 191}
    assert not [s for s in ec2["steps"] if s["source"] == link["id"]]


def test_no_trigger_flow_is_marked_and_still_drawn():
    idle = _chain("aws_lambda_function.lambda-idle")
    assert idle["steps"] == [] and idle["entry_kind"] == "compute without a declared trigger"
    roles = {n["id"]: n["role"] for n in idle["nodes"]}
    assert roles == {
        "aws_lambda_function.lambda-idle": "no_trigger",
        "aws_iam_role.role-idle": "context",
    }
    svg = chain_svg(_model(FIXTURE), idle["id"])
    assert f"Flow {idle['n']}: lambda-idle (no trigger)" in svg
    assert ">lambda-idle</text>" in svg and ">role-idle</text>" in svg


def test_every_node_carries_a_code_reference():
    for c in _flows()["chains"]:
        for n in c["nodes"]:
            ref = n["code_ref"]
            assert ref and ref["file"] == "main.tf" and ref["start_line"] >= 1, (c["id"], n)


# -- 2. per-type chains ----------------------------------------------------------------------


def test_lambda_chain_trigger_function_reads():
    c = _chain("aws_cloudwatch_event_rule.rule-a")
    assert [s["text"] for s in c["steps"]] == [
        "1. rule-a triggers lambda-scheduled (EventBridge rule target main.tf:218-221)",
        "2. lambda-scheduled reads from table-a "
        "(env TABLE_NAME main.tf:208 + IAM dynamodb:GetItem main.tf:141-145)",
    ]
    orphan = _chain("aws_lambda_function.lambda-orphan")
    assert [s["text"] for s in orphan["steps"]] == [
        "1. lambda-orphan sends to queue-b "
        "(env QUEUE_URL main.tf:233 + IAM sqs:SendMessage main.tf:159-163)",
    ]
    assert [(n["relation"], n["label"]) for n in orphan["nodes"] if n["role"] == "context"] == [
        ("execution role", "role-orphan")
    ]


def test_ecs_chain_cluster_service_taskdef_containers_task_role():
    c = _chain("aws_ecs_service.ecs-service-a")
    ctx = [(n["parent"], n["relation"], n["label"]) for n in c["nodes"] if n["role"] == "context"]
    svc, td = "aws_ecs_service.ecs-service-a", "aws_ecs_task_definition.task-def-a"
    assert ctx == [
        (svc, "cluster", "cluster-a"),
        (svc, "task definition", "task-def-a"),
        (td, "container", "app"),
        (td, "container", "sidecar"),
        (td, "task role", "role-task"),
    ]
    # what the task role can access, through the service
    assert [s["text"] for s in c["steps"]] == [
        "1. ecs-service-a reads from table-a (env TABLE_NAME main.tf:290 + "
        "IAM dynamodb:GetItem, dynamodb:Query main.tf:270-274)",
    ]
    app = next(n for n in c["nodes"] if n["label"] == "app")
    assert (
        app["kind_label"] == "container, 123456789012.dkr.ecr.us-east-1.amazonaws.com/app-a:1.0.0"
    )
    assert app["code_ref"] == {"file": "main.tf", "start_line": 278, "end_line": 298}


def test_ecs_behind_a_load_balancer_keeps_lb_and_tg_in_its_chain():
    # kafka_flow: alb-a -> tg-a -> svc-a, with the cluster / task definition / container
    ecs = next(c for c in build_flows(_model("kafka_flow"))["chains"] if c["group"] == "ECS")
    assert ecs["title"] == "alb-a → tg-a → svc-a"
    assert [n["relation"] for n in ecs["nodes"] if n["role"] == "context"] == [
        "cluster",
        "task definition",
        "container",
        "task role",
    ]


def test_asg_and_instance_root_target_group_and_role_access(load):
    m = load(FIXTURE)
    tg = m.edge("TRIGGER", "aws_lb_target_group.tg-a", "aws_autoscaling_group.asg-a")
    assert tg["label"] == "load balancer target"
    access = m.edge("ACCESS", "aws_autoscaling_group.asg-a", "aws_sqs_queue.queue-a")
    assert access["attributes"]["derived_via_role"] == "aws_iam_role.role-ec2"
    assert [e.get("note") for e in access["evidence"]] == [
        "launch template",
        "instance profile",
        "instance profile role",
        "policy statement",
    ]
    asg = _chain("aws_lb.lb-a")
    assert [s["text"] for s in asg["steps"][:4]] == [
        "1. lb-a routes to tg-a (listener HTTPS 443 main.tf:391-394)",
        "2. tg-a routes to asg-a (load balancer target main.tf:403)",
        "3. asg-a can write queue-a "
        "(IAM sqs:SendMessage main.tf:339-343; no reference to it in its configuration)",
        "4. asg-a can read bucket-a "
        "(IAM s3:GetObject main.tf:334-338; no reference to it in its configuration)",
    ]
    assert [(n["relation"], n["label"]) for n in asg["nodes"] if n["role"] == "context"][:3] == [
        ("launch template", "lt-a"),
        ("instance profile", "profile-ec2"),
        ("IAM role", "role-ec2"),
    ]
    ec2 = _chain("aws_instance.instance-a")
    assert ec2["nodes"][0]["role"] == "no_trigger"
    assert [(n["relation"], n["label"]) for n in ec2["nodes"] if n["role"] == "context"][:2] == [
        ("instance profile", "profile-ec2"),
        ("IAM role", "role-ec2"),
    ]


# -- 3. grouping and summary -----------------------------------------------------------------


def test_groups_lambda_ecs_ec2_network_in_order():
    fl = _flows()
    n_of = {c["id"]: c["title"].split(" ")[0] for c in fl["chains"]}
    assert [(g["group"], [n_of[i] for i in g["flows"]]) for g in fl["groups"]] == [
        ("Lambda", ["rule-a", "lambda-orphan", "lambda-idle"]),
        ("ECS", ["ecs-service-a"]),
        ("EC2", ["lb-a", "instance-a"]),
        ("Network", ["lb-c"]),
    ]


def test_markdown_reports_coverage_and_groups():
    md = render_flows_markdown(_flows())
    assert "7 of 7 compute resources covered (4 without a declared trigger)." in md
    assert "By type: Lambda: 3, 5, 7; ECS: 4; EC2: 1, 2; Network: 6." in md
    assert "- task-def-a container: sidecar (`main.tf:278-298`)" in md
    assert "## Flow 7: lambda-idle (no trigger)\n\n(no steps)" in md


def test_deterministic():
    model = run_analysis(FIXTURES / FIXTURE).model
    assert build_flows(model) == _flows()
    assert network_diagram(model) == network_diagram(_model(FIXTURE))
    assert iam_diagram(model) == iam_diagram(_model(FIXTURE))
    for name in ("network", "iam"):
        assert tab_svg(model, name) == tab_svg(_model(FIXTURE), name)


# -- 4. the Network diagram ------------------------------------------------------------------


def test_network_diagram_outside_groups_resources_in_frames():
    dg = network_diagram(_model(FIXTURE))
    depth = {n["label"]: n["depth"] for n in dg["nodes"]}
    # the CIDRs (ingress and egress peers) | the groups | the resources
    assert depth["0.0.0.0/0 (internet)"] == depth["10.9.0.0/16"] == 0
    assert depth["sg-app"] == depth["sg-lb"] == 1
    for r in ("instance-a", "lambda-classifier", "ecs-service-a", "lb-a", "asg-a"):
        assert depth[r] == 2, r
    # the VPC and its subnets are frames, not boxes
    assert "vpc-a" not in depth and "subnet-a" not in depth
    assert [(f["label"], f["parent"], f["columns"]) for f in dg["frames"]] == [
        ("vpc-a", None, [1, 2]),
        ("subnet-a", "aws_vpc.vpc-a", [2, 2]),
        ("subnet-b", "aws_vpc.vpc-a", [2, 2]),
    ]
    frame = {n["label"]: n["frame"] for n in dg["nodes"]}
    assert frame["sg-app"] == frame["sg-lb"] == "aws_vpc.vpc-a"
    assert frame["0.0.0.0/0 (internet)"] is None
    assert frame["instance-a"] == frame["lb-a"] == "aws_subnet.subnet-a"
    assert frame["ecs-service-a"] == "aws_subnet.subnet-b"
    # placement and guards are a line of text in the box, not a line
    meta = {n["label"]: n["meta"] for n in dg["nodes"]}
    assert meta["instance-a"] == meta["lambda-classifier"] == "subnet-a / sg-app"
    assert meta["ecs-service-a"] == "subnet-b / sg-app"
    assert meta["lb-a"] == "subnet-a, subnet-b / sg-lb"
    assert meta["sg-app"] == meta["10.9.0.0/16"] == ""
    assert not [s for s in dg["steps"] if s["quiet"] or s["kind"] != "rule"]
    for n in dg["nodes"]:
        assert n["code_ref"], n
    for f in dg["frames"]:
        assert f["code_ref"], f


def test_network_diagram_security_group_rules_are_the_steps():
    dg = network_diagram(_model(FIXTURE))
    rules = [s for s in dg["steps"] if not s["quiet"]]
    assert [s["text"] for s in rules] == [
        "1. sg-app → 0.0.0.0/0 (internet): egress all traffic (egress all traffic main.tf:47-52)",
        "2. 10.9.0.0/16 → sg-app: ingress tcp 22 "
        "(ingress tcp 22 (aws_security_group_rule.sg-app-ssh) main.tf:55-62)",
        "3. sg-lb → sg-app: ingress tcp 8080 (from the load balancer) "
        "(ingress tcp 8080 main.tf:39-45)",
        "4. 0.0.0.0/0 (internet) → sg-lb: ingress tcp 443 (public https) "
        "(ingress tcp 443 main.tf:26-32)",
    ]
    assert [s["status"] for s in rules] == ["public", "", "", "public"]
    svg = tab_svg(_model(FIXTURE), "network", "repo-a")
    assert "Network: outside → security groups → resources, by VPC and subnet — repo-a" in svg
    # a numbered rule label, the public mark, the legend, a box's code reference (sg-app),
    # a box's subnet / group line, a frame's name
    for text in (
        ">3. ingress tcp 8080<",
        ">(public)<",
        ">security group rule<",
        ">main.tf:35-53<",
        ' class="meta">subnet-a / sg-app<',
        'data-frame="aws_subnet.subnet-b"',
    ):
        assert text in svg, text
    assert "contains" not in svg  # no containment line, nor its legend
    sizes = [int(s) for s in re.findall(r'font-size="(\d+)"', svg)]
    assert min(sizes) >= 14


# -- 5. the IAM diagram ----------------------------------------------------------------------


def test_iam_diagram_role_policies_resources():
    dg = iam_diagram(_model(FIXTURE))
    depth = {n["id"]: n["depth"] for n in dg["nodes"]}
    assert depth["aws_iam_role.role-lambda"] == 0
    assert depth["aws_iam_role_policy.role-lambda"] == 1
    assert depth["aws_dynamodb_table.table-a"] == 2
    grants = [(s["source"], s["verb"], s["target"]) for s in dg["steps"] if not s["quiet"]]
    assert grants[:4] == [
        ("aws_iam_role_policy.role-ec2", "can read", "aws_s3_bucket.bucket-a"),
        ("aws_iam_role_policy.role-lambda", "can read", "aws_s3_bucket.bucket-a"),
        ("aws_iam_role_policy.role-ec2", "can write", "aws_sqs_queue.queue-a"),
        ("aws_iam_role_policy.role-lambda", "can read", "aws_sqs_queue.queue-a"),
    ]
    attached = {(s["source"], s["target"]) for s in dg["steps"] if s["quiet"]}
    assert ("aws_iam_role.role-task", "aws_iam_role_policy.role-task") in attached
    sec = iam_sections(_model(FIXTURE))[0]
    texts = [ln["text"] for ln in sec["lines"]]
    i = texts.index("IAM role: role-lambda")
    assert texts[i + 1 : i + 5] == [
        "inline role policy: role-lambda-data",
        "can read bucket-a (Allow s3:GetObject)",
        "can read queue-a (Allow sqs:ReceiveMessage, sqs:DeleteMessage)",
        "can write table-a (Allow dynamodb:PutItem)",
    ]
    assert "no policy found in this repository" in texts


def test_iam_diagram_managed_inline_and_deny():
    dg = iam_diagram(_model("iam"))
    labels = {n["id"]: (n["depth"], n["label"]) for n in dg["nodes"]}
    assert labels["aws_iam_role.app#inline"] == (1, "example-app (inline)")
    managed = "external:aws_managed_policy:arn:aws:iam::aws:policy/ReadOnlyAccess"
    assert labels[managed] == (1, "ReadOnlyAccess")
    verbs = {(s["source"], s["verb"], s["target"]) for s in dg["steps"]}
    assert ("aws_iam_policy.reports", "denies", "aws_s3_bucket.reports") in verbs
    assert ("aws_iam_policy.reports", "can write", "aws_s3_bucket.reports") in verbs
    assert ("aws_iam_role.app", "attaches", managed) in verbs
    svg = tab_svg(_model("iam"), "iam")
    assert re.search(r">\d+\. denies</text>", svg)  # a numbered step label
    assert "IAM only (can write / can read / can access)" in svg


# -- 6. the API and the UI -------------------------------------------------------------------


def _api(tmp_path, fixture=FIXTURE):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / fixture, repo)
    return Api(ModelStore(repo))


def test_api_tab_diagrams_and_root_views(tmp_path):
    api = _api(tmp_path)
    for name in ("network", "iam"):
        resp = api.handle("GET", f"/api/diagram?name={name}&format=svg")
        assert resp.status == 200 and resp.content_type.startswith("image/svg+xml")
        assert resp.body.decode() == tab_svg(api.store.model(), name, "repo-a")
        assert api.handle("GET", f"/api/diagram?name={name}&format=svg&root=.").status == 200
        assert api.handle("GET", f"/api/diagram?name={name}&format=svg&root=nope").status == 404
        assert api.handle("GET", f"/api/diagram?name={name}&format=mmd").status == 404
    v = json.loads(api.handle("GET", "/api/root-view?root=.&view=iam").body)
    assert (v["view"], v["view_label"], v["primary"], v["diagram"]) == ("iam", "IAM", False, "iam")
    assert v["sections"][0]["title"] == "Roles → policies → what they can access"
    v = json.loads(api.handle("GET", "/api/root-view?root=.&view=network").body)
    assert v["diagram"] == "network"
    fl = json.loads(api.handle("GET", "/api/flow").body)
    assert [g["group"] for g in fl["groups"]] == ["Lambda", "ECS", "EC2", "Network"]
    summary = json.loads(api.handle("GET", "/api/summary").body)
    assert (summary["flow"]["compute_covered"], summary["flow"]["compute_total"]) == (7, 7)


def test_nothing_to_draw_is_not_found(tmp_path):
    api = _api(tmp_path, "iam")  # no VPC, subnet or security group
    assert api.handle("GET", "/api/diagram?name=network&format=svg").status == 404
    assert "diagram" not in build_root_view(api.store.model(), ".", "network")


def test_ui_groups_flows_and_embeds_tab_diagrams(tmp_path):
    js = _api(tmp_path).handle("GET", "/static/app.js").body.decode()
    assert "for (const g of fl.groups)" in js and 'class: "flow-group"' in js
    assert "`${m.compute_covered}/${m.compute_total} compute covered`" in js
    assert "c.no_trigger" in js and 'el("ul", { class: "flow-context" }' in js
    assert "if (d.diagram)" in js and "diagramPanel(d.diagram," in js


# -- 7. placeholders only --------------------------------------------------------------------

_ALLOWED = [
    re.compile(p)
    for p in (
        r"aws_[a-z0-9_]+",  # Terraform types (block labels), not names
        # generic placeholder names
        r"(lambda|queue|table|bucket|ecs|task|tg|lb|asg|lt|role|profile|cluster|rule|vpc|"
        r"subnet|sg|instance|app|sidecar|pk)(-[a-z0-9]+)*(-data|-read|-send|-access)?",
        r"\d+\.\d+\.\d+\.\d+/\d+",  # CIDRs
        r"us-east-1[ab]",
        r"123456789012\.dkr\.ecr\.us-east-1\.amazonaws\.com/[a-z]+-a:1\.0\.0",
        r"(lambda|ecs-tasks|ec2)\.amazonaws\.com",
        r"[a-z0-9]+:[A-Za-z*]+",  # IAM actions
        r"ami-0123456789abcdef0",
        r"\$\{aws_s3_bucket\.bucket-a\.arn\}/\*",
    )
] + [
    re.compile(re.escape(s))
    for s in (
        "2012-10-17",
        "Allow",
        "Service",
        "tcp",
        "-1",
        "python3.12",
        "index.handler",
        "lambda.zip",
        "rate(1 hour)",
        "FARGATE",
        "awsvpc",
        "application",
        "HTTP",
        "HTTPS",
        "forward",
        "PAY_PER_REQUEST",
        "S",
        "ingress",
        "t3.micro",
        "public https",
        "TABLE_NAME",
        "from the load balancer",
    )
]


def test_fixture_uses_placeholder_names_only():
    text = (FIXTURES / FIXTURE / "main.tf").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    hits = [s for s in re.findall(r'"([^"]*)"', code) if not any(p.fullmatch(s) for p in _ALLOWED)]
    assert hits == []
    # no account id but the placeholder one
    assert set(re.findall(r"\b\d{12}\b", text)) == {"123456789012"}
