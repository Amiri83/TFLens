"""Default environment, diagram names, Lambda aliases, named external sources, Kafka
triggers and the draw.io export.

The ``alias_triggers`` fixture: two environments (dev, prod) in ``environments/``; every
name starts with ``${var.env}-example-platform-``. An SQS queue triggers a function
through its alias, an SNS topic comes in as an ARN variable with no value, and two Kafka
event source mappings (self-managed, MSK) target aliases.
"""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET

import pytest
from conftest import FIXTURES

from tflens.cli import main
from tflens.drawio import MIME, chain_drawio, flow_drawio, tab_drawio
from tflens.envs import default_env
from tflens.flow import (
    ROLE_STROKE,
    STYLE,
    build_flows,
    chain_svg,
    common_prefix,
    flow_layout,
    kind_of,
    wrap_name,
)
from tflens.output import run_analysis
from tflens.tabdiagrams import tab_svg
from tflens.webapi import Api, ModelStore, environments_view

FIXTURE = FIXTURES / "alias_triggers"
PREFIX = "dev-example-platform-"


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False, auto_env=True).model


def _chain(model, entry):
    return next(c for c in build_flows(model)["chains"] if c["entry"] == entry)


def _triggers(model, target):
    return [e for e in model["edges"] if e["kind"] == "TRIGGER" and e["target"] == target]


# -- 0. the default environment ----------------------------------------------------------


def test_default_env_prefers_dev_else_the_first():
    envs = [{"name": n} for n in ("prod", "Dev", "uat")]
    assert default_env(envs)["name"] == "Dev"
    assert default_env([{"name": "prod"}, {"name": "uat"}])["name"] == "prod"
    assert default_env([]) is None


def test_analysis_defaults_to_dev_so_names_resolve(model):
    env = model["environment"]
    assert (env["name"], env["default"], env["var_files"]) == (
        "dev",
        True,
        ["environments/dev.tfvars"],
    )
    titles = [c["title"] for c in build_flows(model)["chains"]]
    assert all(PREFIX in t for t in titles), titles
    # without the default, the labels are the blocks' names
    bare = run_analysis(FIXTURE, git=False).model
    assert bare["environment"] is None
    assert "orders → orders" in [c["title"] for c in build_flows(bare)["chains"]]


def test_default_env_falls_back_to_the_first(tmp_path):
    (tmp_path / "main.tf").write_text(
        'variable "env" {\n  type = string\n}\n\n'
        'resource "aws_sqs_queue" "this" {\n  name = "${var.env}-example"\n}\n'
    )
    (tmp_path / "environments").mkdir()
    for name in ("uat", "prod"):
        (tmp_path / "environments" / f"{name}.tfvars").write_text(f'env = "{name}"\n')
    m = run_analysis(tmp_path, git=False, auto_env=True).model
    assert m["environment"]["name"] == "prod"
    queue = next(n for n in m["nodes"] if n["id"] == "aws_sqs_queue.this")
    assert queue["attributes"]["name"] == "prod-example"


def test_explicit_inputs_win_over_the_default():
    m = run_analysis(FIXTURE, git=False, env="prod", auto_env=True).model
    assert (m["environment"]["name"], m["environment"]["default"]) == ("prod", False)
    m = run_analysis(
        FIXTURE, ["environments/prod.tfvars"], git=False, auto_env=True
    ).model  # explicit var files: no environment on top
    assert m["environment"] is None


def test_cli_defaults_to_dev_and_no_env_turns_it_off(tmp_path, capsys):
    out = tmp_path / "model.json"
    assert main(["--dir", str(FIXTURE), "--no-git", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["environment"]["name"] == "dev"
    assert main(["--dir", str(FIXTURE), "--no-git", "--no-env", "--out", str(out)]) == 0
    assert json.loads(out.read_text())["environment"] is None
    with pytest.raises(SystemExit) as exc:
        main(["--dir", str(FIXTURE), "--env", "dev", "--no-env"])
    assert exc.value.code == 2
    assert "cannot be used together" in capsys.readouterr().err


def test_store_defaults_on_startup_and_keeps_no_environment(tmp_path):
    path = tmp_path / "model.json"
    # a model.json written with no environment: re-analyzed with the default one
    path.write_text(json.dumps(run_analysis(FIXTURE, git=False).model))
    store = ModelStore(FIXTURE, path, auto_env=True)
    assert store.model()["environment"]["id"] == "dev"
    view = environments_view(store.model(), store)
    assert (view["active"], view["default"]) == ("dev", True)
    # "no environment" in the switcher: kept, not replaced by the default again
    api = Api(store)
    r = api.handle("POST", "/api/env?name=")
    assert r.status == 200 and json.loads(r.body)["active"] is None
    assert store.model()["environment"] is None
    r = api.handle("POST", "/api/env?name=prod")
    body = json.loads(r.body)
    assert (body["active"], body["default"]) == ("prod", False)
    # without auto_env (an existing model.json is served as is)
    path.write_text(json.dumps(run_analysis(FIXTURE, git=False).model))
    assert ModelStore(FIXTURE, path)._read()["environment"] is None
    assert ModelStore(FIXTURE, path).model()["environment"] is None


def test_switcher_offers_no_environment():
    js = (FIXTURES.parent.parent / "src/tflens/static/app.js").read_text()
    assert '"no environment (variable defaults)"' in js
    assert '" (default)"' in js


# -- 1. names in the diagrams -------------------------------------------------------------


def test_common_prefix_ends_at_a_separator():
    assert common_prefix(["dev-app-orders", "dev-app-audit"]) == "dev-app-"
    assert common_prefix(["dev-app-orders", "dev-app-orders-dlq"]) == "dev-app-"
    assert common_prefix(["/aws/lambda/a", "/aws/lambda/b"]) == "/aws/lambda/"
    assert common_prefix(["orders", "audit"]) == ""
    assert common_prefix(["dev-a", "dev-b"]) == "dev-"
    assert common_prefix(["d-a", "d-b"]) == ""  # too short to be worth it
    assert common_prefix(["only-one"]) == ""


def test_long_names_wrap_to_two_lines():
    assert wrap_name("short", 216, 16) == ["short"]
    lines = wrap_name("orders-processor-with-a-long-name", 216, 16)
    assert lines == ["orders-processor-with-", "a-long-name"]
    assert "".join(lines) == "orders-processor-with-a-long-name"
    # only a name too long for two lines is shortened
    assert wrap_name("x" * 60, 216, 16)[1].endswith("…")


def test_flow_diagram_strips_the_shared_prefix_and_wraps(model):
    c = _chain(model, "aws_sqs_queue.orders")
    svg = chain_svg(model, c["id"])
    head = re.search(r'font-size="18"[^>]*>([^<]+)</text>', svg).group(1)
    assert head.endswith(f"· names: {PREFIX}…")
    assert head.count(PREFIX) == 1  # once, in the title
    assert "orders-queue → orders-processor-with-a-long-name" in head
    assert ">orders-queue</text>" in svg
    assert f">{PREFIX}orders-queue</text>" not in svg
    # the long name on two lines, nothing cut
    assert ">orders-processor-with-</text>" in svg
    assert ">a-long-name</text>" in svg
    names = re.findall(r'font-size="1[46]" fill="#24292f" font-weight="700">([^<]*)</text>', svg)
    assert "orders-queue" in names and not [n for n in names if n.endswith("…")]
    # the tooltip: id, full file path and lines, full name
    assert re.search(
        rf"<title>aws_lambda_function\.orders \(main\.tf:\d+-\d+\)\n"
        rf"{PREFIX}orders-processor-with-a-long-name\n",
        svg,
    )


def test_wrapped_boxes_are_taller(model):
    c = _chain(model, "aws_sqs_queue.orders")
    lay = flow_layout(c)
    by_id = {n["id"]: n for n in lay["nodes"]}
    fn, queue = by_id["aws_lambda_function.orders"], by_id["aws_sqs_queue.orders"]
    assert len(fn["name_lines"]) == 2 and len(queue["name_lines"]) == 1
    assert lay["pos"][fn["id"]][3] == lay["pos"][queue["id"]][3] + 20


# -- 2. Lambda aliases ----------------------------------------------------------------------


def test_trigger_on_an_alias_is_a_trigger_on_its_function(model):
    [e] = _triggers(model, "aws_lambda_function.orders")
    assert e["source"] == "aws_sqs_queue.orders"
    assert e["via"] == ["aws_lambda_event_source_mapping.orders", "aws_lambda_alias.orders_live"]
    assert not any(
        e["target"].startswith("aws_lambda_alias.")
        for e in model["edges"]
        if e["kind"] == "TRIGGER"
    )
    assert not [
        u for u in model["unknowns"] if u["subject"] == "aws_lambda_event_source_mapping.orders"
    ]
    c = _chain(model, "aws_sqs_queue.orders")
    assert (c["group"], c["no_trigger"]) == ("Lambda", False)
    assert c["title"] == f"{PREFIX}orders-queue → {PREFIX}orders-processor-with-a-long-name"
    assert "live" not in c["title"]


def test_alias_by_qualified_name_and_subscription_to_an_alias(tmp_path):
    (tmp_path / "main.tf").write_text(
        """
resource "aws_lambda_function" "this" {
  function_name = "example-fn"
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.this.function_name
  function_version = "$LATEST"
}

resource "aws_sqs_queue" "this" {
  name = "example-queue"
}

resource "aws_sns_topic" "this" {
  name = "example-topic"
}

resource "aws_lambda_event_source_mapping" "this" {
  event_source_arn = aws_sqs_queue.this.arn
  function_name    = "example-fn:live"
}

resource "aws_sns_topic_subscription" "this" {
  topic_arn = aws_sns_topic.this.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_alias.live.arn
}
"""
    )
    m = run_analysis(tmp_path, git=False).model
    sources = {e["source"] for e in _triggers(m, "aws_lambda_function.this")}
    assert sources == {"aws_sqs_queue.this", "aws_sns_topic.this"}
    groups = {c["entry"]: c["group"] for c in build_flows(m)["chains"]}
    assert groups == {"aws_sns_topic.this": "Lambda", "aws_sqs_queue.this": "Lambda"}


# -- 3. external sources named after their variable ---------------------------------------


def test_sns_topic_from_an_arn_variable_is_named_after_it(model):
    [e] = _triggers(model, "aws_lambda_function.alerts")
    topic = next(n for n in model["nodes"] if n["id"] == e["source"])
    assert (topic["kind"], topic["name"]) == ("external", "var.alerts_topic_arn")
    assert kind_of(topic) == "SNS topic, external"
    c = _chain(model, e["source"])
    assert c["title"] == f"var.alerts_topic_arn → {PREFIX}alerts"
    assert not c["no_trigger"]
    # the value is still reported as unknown
    assert any(u["attribute"] == "topic_arn" for u in model["unknowns"])


def test_an_empty_arn_never_gives_a_blank_name(tmp_path):
    (tmp_path / "main.tf").write_text(
        """
variable "topic_arn" {
  type    = string
  default = ""
}

resource "aws_lambda_function" "this" {
  function_name = "example-fn"
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_sns_topic_subscription" "this" {
  topic_arn = var.topic_arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.this.arn
}
"""
    )
    m = run_analysis(tmp_path, git=False).model
    assert all(n["name"] for n in m["nodes"])
    [e] = _triggers(m, "aws_lambda_function.this")
    assert e["source"] == "external:sns_topic:var.topic_arn"


# -- 4. Kafka -----------------------------------------------------------------------------


def test_kafka_mappings_on_aliases_trigger_their_functions(model):
    [kafka] = _triggers(model, "aws_lambda_function.ingest")
    assert kafka["source"] == "external:kafka_topic:example-events"
    assert kafka["label"] == "event source mapping (self-managed Kafka)"
    assert "aws_lambda_alias.ingest_live" in kafka["via"]
    [msk] = _triggers(model, "aws_lambda_function.audit")
    assert msk["source"] == "aws_msk_cluster.events"
    assert msk["label"] == "event source mapping (MSK)"
    assert msk["attributes"]["topics"] == ["example-audit"]
    flows = build_flows(model)
    assert flows["metrics"]["no_trigger"] == 0
    assert not any(c["no_trigger"] for c in flows["chains"])
    assert _chain(model, "aws_msk_cluster.events")["title"] == (f"{PREFIX}events → {PREFIX}audit")


def test_self_managed_kafka_with_unknown_topics_still_triggers(tmp_path):
    (tmp_path / "main.tf").write_text(
        """
variable "topic" {
  type = string
}

resource "aws_lambda_function" "this" {
  function_name = "example-fn"
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_lambda_event_source_mapping" "this" {
  function_name = aws_lambda_function.this.arn
  topics        = [var.topic]

  self_managed_event_source {
    endpoints = {
      KAFKA_BOOTSTRAP_SERVERS = "ip-10-0-0-1:9092"
    }
  }
}
"""
    )
    m = run_analysis(tmp_path, git=False).model
    [e] = _triggers(m, "aws_lambda_function.this")
    assert e["source"] == "external:kafka_topic:var.topic"
    assert build_flows(m)["metrics"]["no_trigger"] == 0


# -- 5. draw.io -----------------------------------------------------------------------------


def _cells(text):
    root = ET.fromstring(text)
    assert root.tag == "mxfile"
    cells = {}
    for el in root.iter():
        if el.tag == "UserObject":
            cell = el.find("mxCell")
            cells[el.get("id")] = {
                **cell.attrib,
                "id": el.get("id"),
                "value": el.get("label"),
                "tooltip": el.get("tooltip"),
            }
        elif el.tag == "mxCell" and el.get("id"):
            cells[el.get("id")] = dict(el.attrib)
    return cells


def _style(cell):
    return dict(p.split("=", 1) for p in cell["style"].split(";") if "=" in p)


def test_flow_drawio_matches_the_svg(model):
    c = _chain(model, "aws_sqs_queue.orders")
    lay = flow_layout(c)
    cells = _cells(chain_drawio(model, c["id"]))
    boxes = {
        cell["tooltip"].split(" ", 1)[0]: cell for cell in cells.values() if cell.get("tooltip")
    }
    assert set(boxes) == {n["id"] for n in lay["nodes"]}
    for n in lay["nodes"]:
        cell = boxes[n["id"]]
        # full names (no prefix taken off, no "…"), the role's colour, the same geometry
        assert f">{n['label']}</span>" in cell["value"]
        assert _style(cell)["strokeColor"] == ROLE_STROKE[n["role"]]
        assert n["label"] in cell["tooltip"] and "main.tf:" in cell["tooltip"]
    fn = boxes["aws_lambda_function.orders"]
    assert f"{PREFIX}orders-processor-with-a-long-name" in fn["value"]
    edges = [cell for cell in cells.values() if cell.get("edge") == "1" and cell.get("source")]
    steps = [cell for cell in edges if cell["value"]]
    assert len(steps) == len([s for s in c["steps"] if not s.get("quiet")])
    assert steps[0]["value"] == "1. triggers"
    assert _style(steps[0])["strokeColor"] == STYLE[("trigger", "")][0]
    # the legend: the same entries and colours
    texts = {
        cell["value"]: _style(cell) for cell in cells.values() if "text;" in cell.get("style", "")
    }
    for label, kind, status in (("sends to", "send", "verified"), ("IAM only", "send", "iam_only")):
        assert texts[label]["fontColor"] == STYLE[(kind, status)][0]
    assert chain_drawio(model, c["id"]) == chain_drawio(model, c["id"])  # deterministic
    assert chain_drawio(model, "flow-99") is None


def test_network_and_iam_drawio(tmp_path):
    m = run_analysis(FIXTURES / "flows_per_compute", git=False).model
    for name in ("network", "iam"):
        text = tab_drawio(m, name, "repo-a")
        cells = _cells(text)
        svg = tab_svg(m, name, "repo-a")
        n_boxes = len(re.findall(r"<g><title>", svg))
        assert len([c for c in cells.values() if c.get("tooltip") and "frame" not in c]) >= n_boxes
        assert "repo-a" in next(
            c["value"] for c in cells.values() if "fontSize=18" in c.get("style", "")
        )
    net = _cells(tab_drawio(m, "network"))
    frames = [c for c in net.values() if "container=1" in c.get("style", "")]
    assert frames, "VPCs and subnets are containers"
    frame_ids = {c["id"] for c in frames}
    assert any(c.get("parent") in frame_ids for c in net.values() if c.get("vertex") == "1")
    assert tab_drawio(m, "nope") is None


def test_api_serves_drawio_next_to_svg(tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "flows_per_compute", repo)
    api = Api(ModelStore(repo))
    for name in ("flow-1", "network", "iam"):
        resp = api.handle("GET", f"/api/diagram?name={name}&format=drawio&download=1")
        assert resp.status == 200 and resp.content_type.startswith(MIME)
        assert resp.headers["Content-Disposition"] == f'attachment; filename="{name}.drawio"'
        ET.fromstring(resp.body)
        assert api.handle("GET", f"/api/diagram?name={name}&format=drawio&root=.").status == 200
        assert api.handle("GET", f"/api/diagram?name={name}&format=drawio&root=nope").status == 404
    assert api.handle("GET", "/api/diagram?name=flow-99&format=drawio").status == 404
    assert api.handle("GET", "/api/diagram?name=architecture&format=drawio").status == 404
    js = (FIXTURES.parent.parent / "src/tflens/static/app.js").read_text()
    assert "Download .drawio" in js and 'href("drawio")' in js
    assert js.count("diagramLinks(") == 3  # defined once, used by flows and Network / IAM


def test_flow_drawio_layout_follows_the_svg(model):
    c = _chain(model, "aws_sqs_queue.orders")
    lay = flow_layout(c, "Flow")
    text = flow_drawio(c, "Flow")
    cells = _cells(text)
    for n in lay["nodes"]:
        x, y, w, h = lay["pos"][n["id"]]
        cell = next(v for v in cells.values() if (v.get("tooltip") or "").startswith(n["id"] + " "))
        geo = ET.fromstring(text).find(f".//UserObject[@id='{cell['id']}']/mxCell/mxGeometry")
        assert [float(geo.get(k)) for k in ("x", "y", "width", "height")] == [
            round(v, 1) for v in (x, y, w, h)
        ]
