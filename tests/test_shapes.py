"""Root shapes: each root classified (app / platform / network / module library) with a
stated reason, its primary view picked, the Provides / Network / Module catalog views,
and a mixed repository listed by shape with a one-line description per root.
Fixtures: app_pipeline, platform_bootstrap, network, module_library, mixed."""

from __future__ import annotations

import json
import re
import shutil

import pytest
from conftest import FIXTURES, _model

from tflens.flow import build_flows, chain_svg
from tflens.handoff import render_handoff
from tflens.output import run_analysis
from tflens.shapes import (
    build_root_view,
    build_shapes,
    classify_root,
    render_shapes_markdown,
    root_model,
)
from tflens.webapi import Api, ModelStore


def _shapes(name: str) -> dict[str, dict]:
    return {r["dir"]: r for r in build_shapes(_model(name))["roots"]}


def _texts(view: dict, section: str) -> list[str]:
    sec = next(s for s in view["sections"] if s["id"] == section)
    return [ln["text"] for ln in sec["lines"]]


def _line(view: dict, section: str, text: str) -> dict:
    sec = next(s for s in view["sections"] if s["id"] == section)
    return next(ln for ln in sec["lines"] if ln["text"] == text)


# -- 1. classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "root", "shape", "view", "why"),
    [
        ("app_pipeline", ".", "app", "flow", "it runs 1 Lambda function fed by"),
        ("platform_bootstrap", ".", "platform", "provides", "it creates shared services"),
        ("network", ".", "network", "network", "it builds network resources"),
        ("module_library", "modules/queue", "library", "catalog", "no backend and no provider"),
        ("module_library", "modules/topic", "library", "catalog", "no backend and no provider"),
    ],
)
def test_each_fixture_gets_its_shape_with_a_reason(fixture, root, shape, view, why):
    r = _shapes(fixture)[root]
    assert (r["shape"], r["view"]) == (shape, view)
    assert r["reason"] and why in r["reason"]
    assert r["summary"]
    # the reason states the resource counts and whether there is a backend
    assert re.search(r"\d+ resources?: \d+ compute, \d+ triggers/queues, \d+ network", r["reason"])
    assert "backend" in r["reason"]
    assert [v["view"] for v in r["views"] if v["primary"]] == [view]


def test_reasons_spell_out_counts_and_backend():
    app = _shapes("app_pipeline")["."]
    assert app["reason"] == (
        "app / pipeline: it runs 1 Lambda function fed by 1 event source mapping, "
        "1 SNS topic, 1 SNS subscription, 1 SQS queue (2 TRIGGER edges). "
        "8 resources: 1 compute, 4 triggers/queues, 0 network, 3 platform; "
        "backend s3 bucket-a/app-pipeline/terraform.tfstate (main.tf:2-6)."
    )
    platform = _shapes("platform_bootstrap")["."]
    assert platform["reason"].startswith(
        "platform / bootstrap: it creates shared services (container registry (ECR), "
        "DNS (Route 53), certificates (ACM), encryption keys (KMS), IAM, S3 buckets, "
        "SSM parameters) and runs no compute. 10 resources: 0 compute, 0 triggers/queues, "
        "2 network, 8 platform; backend s3 bucket-a/platform/terraform.tfstate"
    )
    net = _shapes("network")["."]
    assert "0 compute" in net["reason"] and "9 network" in net["reason"]
    lib = _shapes("module_library")["modules/queue"]
    assert lib["reason"] == (
        "module library: no backend and no provider configuration, nothing in the "
        "repository calls it, and it declares 3 variables and 2 outputs. "
        "2 resources: 0 compute, 2 triggers/queues, 0 network, 0 platform; "
        "no backend block (local state)."
    )


def test_one_line_descriptions():
    assert _shapes("app_pipeline")["."]["summary"] == (
        "Runs 1 Lambda function; 1 flow, main: topic-a → queue-a → fn-a → bucket-a"
    )
    assert _shapes("network")["."]["summary"] == (
        "Builds 1 VPC, 1 subnet, 2 VPC endpoints, 1 security group, 1 transit gateway"
    )
    assert _shapes("module_library")["modules/queue"]["summary"] == (
        "Reusable module: 3 inputs, 2 outputs, 2 resources (2 SQS queues)"
    )
    assert _shapes("platform_bootstrap")["."]["summary"].endswith(
        "; exposes 4 outputs and 1 SSM parameter"
    )


def test_existing_fixtures_keep_sensible_shapes():
    assert _shapes("kafka_flow")["."]["shape"] == "app"
    assert _shapes("iam")["."]["shape"] == "platform"
    assert _shapes("security_groups")["."]["shape"] == "network"
    only = _shapes("module_only")
    assert only["modules/naming"]["shape"] == "library"
    assert only["."]["shape"] == "other" and only["."]["view"] == "provides"
    assert "only calls 3 modules" in only["."]["reason"]
    assert "2 module calls not analyzed" in only["."]["reason"]


def test_classification_is_deterministic():
    model = run_analysis(FIXTURES / "mixed").model
    assert build_shapes(model) == build_shapes(_model("mixed"))


# -- 2. view selection per root --------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "root", "view"),
    [
        ("app_pipeline", ".", "flow"),
        ("platform_bootstrap", ".", "provides"),
        ("network", ".", "network"),
        ("module_library", "modules/queue", "catalog"),
        ("mixed", "services/app", "flow"),
        ("mixed", "stacks/platform", "provides"),
        ("mixed", "stacks/network", "network"),
        ("mixed", "modules/naming", "catalog"),
    ],
)
def test_primary_view_per_root(fixture, root, view):
    v = build_root_view(_model(fixture), root)
    assert (v["view"], v["primary"]) == (view, True)
    assert ("flow" in v) == (view == "flow") and ("sections" in v) == (view != "flow")


def test_view_switcher_overrides_and_rejects_unknowns():
    model = _model("platform_bootstrap")
    net = build_root_view(model, ".", "network")
    assert (net["view"], net["primary"]) == ("network", False)
    assert "VPC: vpc-a — 10.0.0.0/16" in _texts(net, "vpcs")
    assert build_root_view(model, ".", "nope") is None
    assert build_root_view(model, "no/such/root") is None


def test_app_root_flow_is_the_flow_view_of_that_root_only():
    model = _model("mixed")
    v = build_root_view(model, "services/app")
    assert [c["title"] for c in v["flow"]["chains"]] == ["queue-a → fn-a"]
    assert v["flow"]["chains"][0]["steps"][0]["text"] == (
        "1. queue-a triggers fn-a (event source mapping services/app/main.tf:38-41)"
    )
    assert v["flow"]["start_here"][0]["text"] == "Root directory: services/app/ (terraform root)"
    # a single-root app repository: the root's flow is the repository's flow
    app = _model("app_pipeline")
    assert build_root_view(app, ".")["flow"] == build_flows(app)
    sub = root_model(model, "stacks/network")
    assert {n["id"] for n in sub["nodes"] if n["kind"] != "external"} == {
        "stacks/network::aws_security_group.app",
        "stacks/network::aws_subnet.private",
        "stacks/network::aws_vpc.main",
    }


# -- 3. the Provides view --------------------------------------------------------------------


def test_provides_view_creates_outputs_and_environments():
    v = build_root_view(_model("platform_bootstrap"), ".")
    creates = _texts(v, "creates")
    for text in (
        "container registry (ECR) (1)",
        "ECR repository: ecr-a",
        "certificates (ACM) (1)",
        "ACM certificate: cert-a",
        "KMS key: key-a",
        "S3 buckets (1)",
        "SSM parameter: /repo-a/ecr-url",
        "network (2)",
        "VPC: vpc-a",
        "VPC endpoint: ep-a",
    ):
        assert text in creates, text
    ecr = _line(v, "creates", "ECR repository: ecr-a")
    assert ecr["node"] == "aws_ecr_repository.app"
    assert ecr["location"] == {"file": "main.tf", "start_line": 33, "end_line": 35}
    outputs = _texts(v, "outputs")
    assert outputs == [
        "certificate_arn = aws_acm_certificate.main.arn (cert-a)",
        "ecr_repository_url = aws_ecr_repository.app.repository_url (ecr-a) "
        "— where images are pushed",
        "kms_key_arn = aws_kms_key.main.arn (key-a)",
        "vpc_id = aws_vpc.main.id (vpc-a) — the shared VPC",
    ]
    vpc = _line(v, "outputs", "vpc_id = aws_vpc.main.id (vpc-a) — the shared VPC")
    assert vpc["location"] == {"file": "outputs.tf", "start_line": 1, "end_line": 4}
    assert vpc["node"] == "aws_vpc.main"
    consumers = _texts(v, "consumers")
    assert "SSM parameter /repo-a/ecr-url (value from ecr-a)" in consumers
    assert "no reader of this parameter in this repository" in consumers
    assert any("No terraform_remote_state data source" in t for t in consumers)
    assert _texts(v, "envs") == [
        "dev (tfvars): envs/dev.tfvars",
        "state: backend s3 bucket-a/platform/terraform.tfstate",
        "prod (tfvars): envs/prod.tfvars",
        "state: backend s3 bucket-a/platform/terraform.tfstate",
    ]


def test_provides_view_finds_remote_state_and_ssm_consumers():
    v = build_root_view(_model("mixed"), "stacks/platform")
    assert _texts(v, "outputs") == [
        "kms_key_arn = aws_kms_key.main.arn (key-a)",
        "read by services/app/ aws_sqs_queue.work.kms_master_key_id "
        "(via data.terraform_remote_state.platform)",
        "log_bucket = aws_s3_bucket.logs.bucket (logs)",
    ]
    use = _line(
        v,
        "outputs",
        "read by services/app/ aws_sqs_queue.work.kms_master_key_id "
        "(via data.terraform_remote_state.platform)",
    )
    assert use["location"]["file"] == "services/app/main.tf" and use["edge"]
    assert _texts(v, "consumers") == [
        "services/app/ reads its state via data.terraform_remote_state.platform "
        "(outputs: kms_key_arn)",
        "SSM parameter /repo-a/ecr-url (value from ecr-a)",
        "read by services/app/ data.aws_ssm_parameter.ecr_url",
    ]
    reader = _line(v, "consumers", "read by services/app/ data.aws_ssm_parameter.ecr_url")
    assert reader["node"] == "services/app::data.aws_ssm_parameter.ecr_url"
    # the network stack's state is read by nobody: the remote state names the platform key
    net = build_root_view(_model("mixed"), "stacks/network", "provides")
    assert any("No terraform_remote_state" in t for t in _texts(net, "consumers"))


# -- 4. the Network view ---------------------------------------------------------------------


def test_network_view_vpc_subnets_endpoints_and_reach():
    v = build_root_view(_model("network"), ".")
    sec = next(s for s in v["sections"] if s["id"] == "vpcs")
    tree = [("  " * ln["level"]) + ln["text"] for ln in sec["lines"]]
    assert tree == [
        "VPC: vpc-a — 10.0.0.0/16",
        "  subnet: subnet-a — 10.0.1.0/24 us-east-1a",
        "    TGW attachment: main — placed in this subnet",
        "    VPC endpoint: ep-a — placed in this subnet",
        "  security group: sg-a",
        "    VPC endpoint: ep-a — uses this security group",
        "  VPC endpoint: s3 — com.amazonaws.us-east-1.s3, Gateway",
        "  VPC endpoint: ep-a — com.amazonaws.us-east-1.sqs, Interface",
        "  route table: private",
        "  TGW attachment: main",
    ]
    assert sec["lines"][0]["location"] == {"file": "main.tf", "start_line": 13, "end_line": 18}
    assert _texts(v, "outside") == ["transit gateway: tgw for repo-a"]
    reach = next(s for s in v["sections"] if s["id"] == "reach")
    assert [ln["text"] for ln in reach["lines"]] == [
        "vpc-a → sg-a: ingress tcp 443",
        "s3 → com.amazonaws.us-east-1.s3: private Gateway endpoint",
        "ep-a → com.amazonaws.us-east-1.sqs: private Interface endpoint",
    ]
    assert all(ln["edge"] and ln["location"] for ln in reach["lines"])


# -- 5. the Module catalog -------------------------------------------------------------------


def test_module_catalog_inputs_outputs_resources_usage_callers():
    v = build_root_view(_model("module_library"), "modules/queue")
    queue, naming = v["sections"]
    assert (queue["title"], naming["title"]) == ("Module modules/queue/", "Module modules/naming/")
    texts = [ln["text"] for ln in queue["lines"]]
    assert texts[:4] == [
        "Inputs (3)",
        "name: string — required — queue name",
        "tags: map(string) = {}",
        "visibility_timeout: number = 30",
    ]
    assert "queue_url = aws_sqs_queue.this.url — URL to send messages to" in texts
    assert "Resources (2)" in texts and "SQS queue: this" in texts and "SQS queue: dlq" in texts
    assert "module.naming: local ../naming" in texts
    assert texts[-1] == (
        "nothing in this repository calls it (it is a root: used from other repositories)"
    )
    name_input = next(ln for ln in queue["lines"] if ln["text"].startswith("name:"))
    assert name_input["location"] == {
        "file": "modules/queue/main.tf",
        "start_line": 1,
        "end_line": 4,
    }
    assert queue["code"] == (
        'module "queue" {\n'
        '  source = "./modules/queue"\n'
        "\n"
        '  name               = "..."  # required\n'
        "  # tags               = {}\n"
        "  # visibility_timeout = 30\n"
        "}\n"
    )
    # the local child module: who calls it, from where
    called = [ln for ln in naming["lines"] if ln["text"] == "modules/queue/ as module.naming"]
    assert called and called[0]["location"]["file"] == "modules/queue/main.tf"


# -- 6. a mixed repository -------------------------------------------------------------------


def test_mixed_repo_lists_roots_grouped_by_shape():
    s = build_shapes(_model("mixed"))
    assert s["mixed"] is True
    assert [(g["shape"], [r["dir"] for r in g["roots"]]) for g in s["groups"]] == [
        ("app", ["services/app"]),
        ("platform", ["stacks/platform"]),
        ("network", ["stacks/network"]),
        ("library", ["modules/naming"]),
    ]
    assert [ln["text"] for ln in s["lines"]] == [
        "app / pipeline (1 root, Flow view)",
        "services/app/ — Runs 1 Lambda function; 1 flow, main: queue-a → fn-a",
        "platform / bootstrap (1 root, Provides view)",
        "stacks/platform/ — Provides container registry (ECR), certificates (ACM), "
        "encryption keys (KMS), S3 buckets and SSM parameters; "
        "exposes 2 outputs and 1 SSM parameter",
        "network (1 root, Network view)",
        "stacks/network/ — Builds 1 VPC, 1 subnet, 1 security group",
        "module library (1 root, Module catalog view)",
        "modules/naming/ — Reusable module: 2 inputs, 1 output, 0 resources",
    ]
    assert all(r["reason"] for r in s["roots"])
    assert not build_shapes(_model("module_library"))["mixed"]


def test_mixed_markdown_and_handoff():
    md = render_shapes_markdown(build_shapes(_model("mixed")))
    assert md.startswith(
        "# Roots by shape\n\nMixed repository: 4 roots of 4 shapes, each with its own view."
    )
    assert "## app / pipeline (Flow view)" in md
    assert "- `stacks/network/` — Builds 1 VPC, 1 subnet, 1 security group" in md
    assert "  - Why: network: it builds network resources" in md
    handoff = render_handoff(_model("mixed"), "repo-a")
    assert handoff.index("## Roots by shape") < handoff.index("## Flows")


# -- 7. the web UI ---------------------------------------------------------------------------


def _api(tmp_path, fixture="mixed"):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / fixture, repo)
    return Api(ModelStore(repo))


def test_shapes_and_root_view_api(tmp_path):
    api = _api(tmp_path)
    shapes = json.loads(api.handle("GET", "/api/shapes").body)
    assert shapes["mixed"] and len(shapes["groups"]) == 4
    assert shapes["default_root"] == "modules/naming"
    v = json.loads(api.handle("GET", "/api/root-view?root=stacks/network").body)
    assert v["view"] == "network" and v["shape"]["shape"] == "network"
    v = json.loads(api.handle("GET", "/api/root-view?root=stacks/network&view=catalog").body)
    assert v["view"] == "catalog" and not v["primary"]
    assert api.handle("GET", "/api/root-view?root=nope").status == 404
    assert api.handle("GET", "/api/root-view?root=stacks/network&view=nope").status == 404
    summary = json.loads(api.handle("GET", "/api/summary").body)
    assert {(r["dir"], r["view"]) for r in summary["shapes"]} == {
        ("modules/naming", "catalog"),
        ("services/app", "flow"),
        ("stacks/network", "network"),
        ("stacks/platform", "provides"),
    }


def test_per_root_flow_diagram(tmp_path):
    api = _api(tmp_path)
    resp = api.handle("GET", "/api/diagram?name=flow-1&format=svg&root=services/app")
    assert resp.status == 200
    model = api.store.model()
    assert resp.body.decode() == chain_svg(root_model(model, "services/app"), "flow-1", "repo-a")
    assert ">fn-a</text>" in resp.body.decode()
    assert api.handle("GET", "/api/diagram?name=flow-1&format=svg&root=nope").status == 404
    assert (
        api.handle("GET", "/api/diagram?name=flow-1&format=svg&root=stacks/network").status == 404
    )


def test_root_view_is_the_landing_tab_with_switchers(tmp_path):
    api = _api(tmp_path)
    html = api.handle("GET", "/").body.decode()
    assert '<button type="button" class="tab active" data-view="roots">Root view</button>' in html
    assert '<div id="roots" class="view-roots">' in html
    assert '<div id="root-list"></div>' in html  # the per-root switcher
    js = api.handle("GET", "/static/app.js").body.decode()
    assert 'api("api/shapes")' in js and "renderRoots(shapes)" in js
    assert "api/root-view?root=${encodeURIComponent(root)}&view=${encodeURIComponent(view)}" in js
    assert 'class: "view-switcher"' in js and '"data-rview": v.view' in js
    assert "`${v.label} (primary)`" in js
    assert 'flowParts(d.flow, "rv", `&root=${encodeURIComponent(root)}`)' in js
    css = api.handle("GET", "/static/app.css").body.decode()
    assert "#roots .diagram img { display: block; max-width: none;" in css


def test_classify_root_handles_a_root_missing_from_the_model():
    model = _model("terragrunt_only")
    entry = model["structure"]["roots"][0]
    r = classify_root(model, entry)
    assert r["shape"] == "other" and "Not analyzed:" in r["reason"]
