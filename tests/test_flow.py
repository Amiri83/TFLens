"""The Flow view: entry points, sends-to / reads-from (verified, unverified, can write),
numbered chains with file:line evidence, side branches, diagrams, metrics, and the Flow
tab as the landing view. Fixture: kafka_flow (Kafka -> Lambda -> SQS -> Lambda ->
DynamoDB, with a DLQ, an alarm, log groups and an ECS service behind a load balancer)."""

from __future__ import annotations

import json
import re

from conftest import FIXTURES, _model

from tflens.flow import build_flows, chain_svg, render_flows_markdown
from tflens.handoff import render_handoff
from tflens.output import run_analysis
from tflens.webapi import Api, ModelStore

KAFKA_STEPS = [
    "1. topic-a (external) triggers fn-a "
    "(event source mapping (self-managed Kafka) ingest.tf:7-22)",
    "2. fn-a sends to queue-a (env QUEUE_URL ingest.tf:33 + IAM sqs:SendMessage iam.tf:21-25)",
    "3. fn-a reads from secret-a "
    "(env SECRET_ARN ingest.tf:34 + IAM secretsmanager:GetSecretValue iam.tf:26-30)",
    "4. queue-a triggers fn-b (event source mapping store.tf:3-7)",
    "5. fn-b sends to table-a "
    "(env TABLE_NAME store.tf:18 + IAM dynamodb:PutItem, dynamodb:UpdateItem iam.tf:50-54)",
    "6. fn-b sends to bucket-a, unverified (env BUCKET_NAME store.tf:19; no IAM write action found)",
    "7. fn-b can write topic-b "
    "(IAM sns:Publish iam.tf:55-59; no reference to it in its configuration)",
]
ECS_STEPS = [
    "1. alb-a routes to tg-a (listener HTTPS 443 service.tf:22-25)",
    "2. tg-a routes to svc-a (load balancer target service.tf:84-88)",
    "3. svc-a reads from table-a "
    "(env TABLE_NAME service.tf:67 + IAM dynamodb:GetItem, dynamodb:Query service.tf:48-52)",
]


def _flows():
    return build_flows(_model("kafka_flow"))


# -- 1. the edges the flows are built from ---------------------------------------------------


def test_kafka_and_load_balancer_triggers(load):
    m = load("kafka_flow")
    kafka = m.edge("TRIGGER", "external:kafka_topic:topic-a", "aws_lambda_function.classifier")
    assert kafka["label"] == "event source mapping (self-managed Kafka)"
    assert kafka["attributes"]["topics"] == ["topic-a"]
    assert kafka["via"] == ["aws_lambda_event_source_mapping.kafka"]
    lb = m.edge("TRIGGER", "aws_lb.public", "aws_lb_target_group.svc")
    assert lb["label"] == "listener HTTPS 443"
    tg = m.edge("TRIGGER", "aws_lb_target_group.svc", "aws_ecs_service.svc")
    assert tg["attributes"] == {"container_name": "app", "container_port": "8080"}


def test_env_var_references_point_at_their_line(load):
    m = load("kafka_flow")
    queue = m.edge("CONFIG", "aws_lambda_function.classifier", "aws_sqs_queue.work")
    assert queue["label"] == "env QUEUE_URL"
    assert (queue["evidence"][0]["start_line"], queue["evidence"][0]["end_line"]) == (33, 33)
    # ECS container environment: one edge per variable, on its own line
    ecs = m.edge("CONFIG", "aws_ecs_task_definition.svc", "aws_dynamodb_table.orders")
    assert ecs["label"] == "env TABLE_NAME"
    assert ecs["attributes"]["env_var"] == "TABLE_NAME"
    assert ecs["attributes"]["container"] == "app"
    assert ecs["evidence"][0]["start_line"] == 67
    # the rest of the container definition still references the log group
    m.edge("CONFIG", "aws_ecs_task_definition.svc", "aws_cloudwatch_log_group.svc")


def test_ecs_service_runs_as_its_task_role(load):
    m = load("kafka_flow")
    e = m.edge("ACCESS", "aws_ecs_service.svc", "aws_dynamodb_table.orders")
    assert e["attributes"]["actions"] == ["dynamodb:GetItem", "dynamodb:Query"]
    assert [ev.get("note") for ev in e["evidence"]] == [
        "task definition",
        "task role",
        "policy statement",
    ]


# -- 2. chains -------------------------------------------------------------------------------


def test_chain_text_is_numbered_with_evidence():
    fl = _flows()
    assert [c["entry"] for c in fl["chains"]] == [
        "external:kafka_topic:topic-a",
        "aws_lb.public",
    ]
    kafka, ecs = fl["chains"]
    assert kafka["title"] == "topic-a → fn-a → queue-a → fn-b → table-a"
    assert [s["text"] for s in kafka["steps"]] == KAFKA_STEPS
    assert [s["text"] for s in ecs["steps"]] == ECS_STEPS
    assert kafka["entry_kind"] == "Kafka topic, external"


def test_link_tri_state():
    steps = {(s["source"], s["target"]): s for s in _flows()["chains"][0]["steps"]}
    fn_a, fn_b = "aws_lambda_function.classifier", "aws_lambda_function.writer"
    assert (steps[fn_a, "aws_sqs_queue.work"]["verb"]) == "sends to"
    assert steps[fn_a, "aws_sqs_queue.work"]["status"] == "verified"
    assert steps[fn_a, "aws_secretsmanager_secret.kafka"]["verb"] == "reads from"
    unverified = steps[fn_b, "aws_s3_bucket.archive"]
    assert (unverified["verb"], unverified["status"]) == ("sends to (unverified)", "unverified")
    can_write = steps[fn_b, "aws_sns_topic.events"]
    assert (can_write["verb"], can_write["status"]) == ("can write", "iam_only")
    # evidence: the env var line and the policy statement, each a file:line
    ev = steps[fn_a, "aws_sqs_queue.work"]["evidence"]
    assert [(e["what"], e["location"]) for e in ev] == [
        ("env QUEUE_URL", {"file": "ingest.tf", "start_line": 33, "end_line": 33}),
        ("IAM sqs:SendMessage", {"file": "iam.tf", "start_line": 21, "end_line": 25}),
    ]
    # the queue that triggers fn-b is the step before, not a "can read" link
    assert (fn_b, "aws_sqs_queue.work") not in steps


def test_side_branches_are_not_chain_steps():
    kafka, ecs = _flows()["chains"]
    side = [(s["kind"], s["parent"], s["node"]) for s in kafka["side"]]
    assert side == [
        ("logs", "aws_lambda_function.classifier", "aws_cloudwatch_log_group.classifier"),
        ("dlq", "aws_sqs_queue.work", "aws_sqs_queue.work_dlq"),
        ("alarm", "aws_sqs_queue.work_dlq", "aws_cloudwatch_metric_alarm.dlq"),
    ]
    assert kafka["side"][1]["text"] == "queue-a dead-letter queue: queue-a-dlq"
    assert kafka["side"][1]["location"] == {"file": "queue.tf", "start_line": 3, "end_line": 6}
    targets = {s["target"] for s in kafka["steps"]}
    assert not targets & {s["node"] for s in kafka["side"]}
    assert [s["text"] for s in ecs["side"]] == ["svc-a logs to /ecs/svc-a"]


def test_metrics():
    assert _flows()["metrics"] == {
        "flows": 2,
        "entry_points": 2,
        "steps": 10,
        "unverified": 1,
        "iam_only": 1,
        "side_branches": 4,
        # every compute (fn-a, fn-b, svc-a) is in a chain; none needs a flow of its own
        "compute_covered": 3,
        "compute_total": 3,
        "no_trigger": 0,
    }


def test_start_here_lists_root_entry_files_and_flow_order():
    lines = [ln["text"] for ln in _flows()["start_here"]]
    assert lines[0] == "Root directory: ./ (terraform root)"
    assert "ingest.tf — flow 1: topic-a (Kafka topic, external)" in lines
    assert "service.tf — flow 2: alb-a (load balancer)" in lines
    i = lines.index("Files in flow order:")
    assert lines[i + 1 :] == ["ingest.tf", "iam.tf", "queue.tf", "store.tf", "service.tf"]
    assert lines[lines.index("Modules in flow order:") + 1] == "root module"


def test_flows_are_deterministic():
    model = run_analysis(FIXTURES / "kafka_flow").model
    assert build_flows(model) == _flows()
    assert chain_svg(model, "flow-1") == chain_svg(_model("kafka_flow"), "flow-1")


def test_markdown_and_handoff():
    md = render_flows_markdown(_flows())
    assert md.startswith("# Flows\n\n2 flows found · 10 steps · 1 unverified link · 1 IAM-only.")
    assert "\n".join(KAFKA_STEPS) in md
    assert "- queue-a dead-letter queue: queue-a-dlq (`queue.tf:3-6`)" in md
    handoff = render_handoff(_model("kafka_flow"), "repo-a")
    assert "## Flows" in handoff and "### Flow 1: topic-a → fn-a" in handoff
    assert handoff.index("## Flows") < handoff.index("## Overview")
    assert handoff.count("```mermaid") == 3  # flows are text; the three diagrams unchanged


def test_no_trigger_means_no_flow():
    fl = build_flows(_model("iam"))
    assert fl["metrics"]["flows"] == 0
    assert any("No entry point found" in ln["text"] for ln in fl["start_here"])


def test_eventbridge_and_api_gateway_entry_points(tmp_path):
    (tmp_path / "main.tf").write_text(
        """
resource "aws_lambda_function" "fn" {
  function_name = "fn-a"
  role          = "arn:aws:iam::123456789012:role/role-a"
}
resource "aws_cloudwatch_event_rule" "nightly" {
  name                = "rule-a"
  schedule_expression = "rate(1 day)"
}
resource "aws_cloudwatch_event_target" "nightly" {
  rule = aws_cloudwatch_event_rule.nightly.name
  arn  = aws_lambda_function.fn.arn
}
resource "aws_apigatewayv2_api" "http" {
  name          = "api-a"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "fn" {
  api_id           = aws_apigatewayv2_api.http.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.fn.invoke_arn
}
""",
        encoding="utf-8",
    )
    fl = build_flows(run_analysis(tmp_path).model)
    assert [c["steps"][0]["text"] for c in fl["chains"]] == [
        "1. api-a triggers fn-a (API integration main.tf:18-22)",
        "1. rule-a triggers fn-a (EventBridge rule target main.tf:10-13)",
    ]


# -- 3. diagrams -----------------------------------------------------------------------------


def test_flow_diagram_left_to_right_readable():
    model = _model("kafka_flow")
    svg = chain_svg(model, "flow-1", "repo-a")
    assert svg.startswith("<svg") and "max-width" not in svg  # natural size
    sizes = [int(s) for s in re.findall(r'font-size="(\d+)"', svg)]
    assert sizes and min(sizes) >= 14
    # boxes left to right along the main line: topic-a, fn-a, queue-a, fn-b, table-a
    xs = []
    for name in ("topic-a", "fn-a", "queue-a", "fn-b", "table-a"):
        m = re.search(rf'<rect x="(\d+)"[^>]*/><text[^>]*>{re.escape(name)}</text>', svg)
        assert m, name
        xs.append(int(m.group(1)))
    assert xs == sorted(xs) and len(set(xs)) == 5
    # numbered step labels, the unverified / IAM-only marks, the side boxes
    for label in ("1. triggers", "2. sends to", "6. sends to", "(unverified)", "(IAM only)"):
        assert f">{label}</text>" in svg, label
    for side in ("queue-a-dlq", "alarm-a", "/aws/lambda/fn-a", "dead-letter queue"):
        assert f">{side}</text>" in svg
    width = int(re.search(r'width="(\d+)"', svg).group(1))
    assert width > 1500  # wide: the page scrolls it instead of shrinking it
    assert chain_svg(model, "flow-9") is None


# -- 4. the web UI ---------------------------------------------------------------------------


def _api(tmp_path):
    import shutil

    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "kafka_flow", repo)
    return Api(ModelStore(repo))


def test_flow_tab_follows_the_root_view(tmp_path):
    # the landing tab is the Environments view, then the Root view (an app root shows its
    # Flow there, see test_shapes.py); the Flow tab keeps the repository-wide flows after it
    api = _api(tmp_path)
    html = api.handle("GET", "/").body.decode()
    tabs = re.findall(r'<button type="button" class="([^"]+)" data-view="(\w+)"', html)
    assert tabs[:3] == [("tab active", "environments"), ("tab", "roots"), ("tab", "flow")]
    assert [v for c, v in tabs if "active" in c] == ["environments"]
    assert '<div id="flow" class="view-flow hidden">' in html
    assert '<div id="overview" class="view-overview hidden">' in html
    js = api.handle("GET", "/static/app.js").body.decode()
    assert 'const VIEWS = ["environments", "roots", "flow",' in js and "renderFlow(fl, ov)" in js
    assert 'api("api/flow")' in js
    # the inventory is still there, collapsed, after the flows
    assert 'allResources(ov, "all-resources", "fl-all", true)' in js
    assert "`All resources (${res.length})`" in js
    assert 'el("details", { class: cls, id }' in js  # no "open": collapsed
    css = api.handle("GET", "/static/app.css").body.decode()
    rules = dict(re.findall(r"^#flow (\.[\w\- ]+?) \{ (.*?) \}$", css, re.M))
    assert "overflow: auto" in rules[".diagram"]
    assert "max-width: none" in rules[".diagram img"]


def test_flow_api_summary_and_diagram(tmp_path):
    api = _api(tmp_path)
    fl = json.loads(api.handle("GET", "/api/flow").body)
    assert [s["text"] for s in fl["chains"][0]["steps"]] == KAFKA_STEPS
    summary = json.loads(api.handle("GET", "/api/summary").body)
    assert summary["flow"]["flows"] == 2 and summary["flow"]["unverified"] == 1
    resp = api.handle("GET", "/api/diagram?name=flow-1&format=svg")
    assert resp.status == 200 and resp.content_type.startswith("image/svg+xml")
    assert resp.body.decode() == chain_svg(api.store.model(), "flow-1", "repo-a")
    assert api.handle("GET", "/api/diagram?name=flow-7&format=svg").status == 404
    assert api.handle("GET", "/api/diagram?name=flow-1&format=mmd").status == 404
