"""Environments view, env detection hardening and honest counting.

The ``env_layout`` fixture: one root, ``backend "s3" {}``, ``env/<env>.tfvars`` for dev /
uat / prod / intg and ``env/<env>.hcl`` backend configurations for dev / uat / prod only
(intg is not wired: tfvars only, not in the workflow's matrix). Four Lambda functions go
wrapper module -> base module with ``count = var.enabled ? 1 : 0`` (the wrapper's legacy
function has the opposite count). Per environment: 1 ECS service, 4 Lambda functions;
the archive bucket only in prod; intg leaves ``alerts_enabled`` unset.
"""

from __future__ import annotations

import json
import re

import pytest
from conftest import FIXTURES

from tflens.countwhy import source_text
from tflens.detect import backend_env, backend_files
from tflens.envs import count_text, inventory, inventory_key
from tflens.flow import build_flows
from tflens.loader import load_repo
from tflens.output import run_analysis
from tflens.overview import build_overview, purpose_text
from tflens.shapes import build_root_view
from tflens.webapi import Api, ModelStore, environments_view

FIXTURE = FIXTURES / "env_layout"
ENVS = ["dev", "intg", "prod", "uat"]
WIRED = ["dev", "prod", "uat"]
LAMBDAS = ["api", "notifier", "scheduler", "worker"]


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False).model


def _env(model, name):
    return next(e for e in model["structure"]["environments"] if e["name"] == name)


def _row(model, name):
    return next(r for r in model["environment_analysis"]["environments"] if r["id"] == name)


def _call(api, path, method="GET"):
    r = api.handle(method, path)
    return r.status, json.loads(r.body)


# -- 1. detection ---------------------------------------------------------------------------


def test_env_dir_tfvars_and_hcl_named_after_the_env(model):
    envs = model["structure"]["environments"]
    assert [(e["name"], e["root"]) for e in envs] == [(n, ".") for n in ENVS]
    for name in ENVS:
        assert _env(model, name)["var_files"] == [f"env/{name}.tfvars"]
    for name in WIRED:
        e = _env(model, name)
        assert e["backend_hcl"] == [f"env/{name}.hcl"]
        assert e["wired"] is True
        assert e["wiring"] == [
            f"backend config: env/{name}.hcl",
            "CI / scripts: .github/workflows/deploy.yml",
        ]
    # the backend files are listed, so their source can be shown
    assert {f"env/{n}.hcl" for n in WIRED} <= set(model["files"])


def test_env_with_tfvars_only_is_not_wired_but_analyzed(model):
    intg = _env(model, "intg")
    assert intg["backend_hcl"] == [] and intg["var_file_cli"] == []
    assert intg["wired"] is False and intg["wiring"] == []
    assert "tfvars only" in intg["not_wired_reason"]
    # the workflow's ${{ matrix.env }} lists dev, uat, prod: it does not stand for intg
    assert model["environment_analysis"]["not_wired"] == ["intg"]
    assert _row(model, "intg")["counts"]["aws_lambda_function"] == 4


def test_backend_files_by_name_and_by_content(tmp_path):
    assert backend_env("env/dev.hcl") == "dev"
    assert backend_env("env/prod.backend.hcl") == "prod"
    assert backend_env("backend-config/uat.hcl") == "uat"
    (tmp_path / "main.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
    for d in ("env", "vars", "tfvars", "envs", "environments", "backend-config"):
        (tmp_path / d).mkdir()
    (tmp_path / "env" / "dev.tfvars").write_text('env = "dev"\n')
    (tmp_path / "env" / "dev.hcl").write_text('bucket = "example"\nkey = "app.tfstate"\n')
    # named after an environment but not backend settings: not a backend configuration
    (tmp_path / "vars" / "prod.hcl").write_text('locals {\n  x = "y"\n}\n')
    (tmp_path / "tfvars" / "uat.hcl").write_text('instance_type = "t3.micro"\n')
    (tmp_path / "envs" / "qa.backend.hcl").write_text('bucket = "example"\n')
    (tmp_path / "backend-config" / "stage.hcl").write_text('bucket = "example"\n')
    found = backend_files(load_repo(tmp_path))
    assert sorted(found) == [
        "backend-config/stage.hcl",
        "env/dev.hcl",
        "envs/qa.backend.hcl",
    ]
    m = run_analysis(tmp_path, git=False).model
    envs = {e["name"]: e for e in m["structure"]["environments"]}
    assert envs["dev"]["backend_hcl"] == ["env/dev.hcl"]
    assert envs["stage"]["backend_hcl"] == ["backend-config/stage.hcl"]
    assert envs["stage"]["root"] == "."  # backend-config/ belongs to the directory above


@pytest.mark.parametrize("folder", ["env", "envs", "environments", "vars", "tfvars"])
def test_env_folders(tmp_path, folder):
    (tmp_path / "main.tf").write_text('variable "env" {\n  type = string\n}\n')
    (tmp_path / folder).mkdir()
    for name in ("dev", "intg"):
        (tmp_path / folder / f"{name}.tfvars").write_text(f'env = "{name}"\n')
    m = run_analysis(tmp_path, git=False).model
    envs = m["structure"]["environments"]
    assert [e["name"] for e in envs] == ["dev", "intg"]
    assert all(not e["wired"] for e in envs)


# -- 2. state file per environment -----------------------------------------------------------


def test_state_file_per_environment(model):
    for name in WIRED:
        state = _env(model, name)["state"]
        assert state["type"] == "s3" and state["files"] == [f"env/{name}.hcl"]
        by = {s["key"]: s for s in state["settings"]}
        assert [s["key"] for s in state["settings"]] == [
            "bucket",
            "key",
            "region",
            "dynamodb_table",
        ]
        assert by["bucket"]["value"] == f"example-tfstate-{name}"
        assert by["key"]["value"] == "app/terraform.tfstate"
        assert by["region"]["value"] == "us-east-1"
        assert by["dynamodb_table"]["value"] == f"example-tflock-{name}"
        assert by["dynamodb_table"]["location"] == {
            "file": f"env/{name}.hcl",
            "start_line": 4,
            "end_line": 4,
        }
    intg = _env(model, "intg")["state"]
    assert intg["type"] == "s3" and intg["files"] == []
    assert all(not s["known"] for s in intg["settings"])
    assert "no backend configuration for this environment" in intg["settings"][0]["reason"]


# -- 3. counts per environment, never assuming 1 ----------------------------------------------


def test_counts_per_environment(model):
    for name in ENVS:
        r = _row(model, name)
        assert r["counts"]["aws_ecs_service"] == 1
        assert r["counts"]["aws_lambda_function"] == 4
        assert [f["name"] for f in r["lambda_functions"]] == [f"{name}-{x}" for x in LAMBDAS]
    assert _row(model, "intg")["uncertain"] == {"aws_sns_topic": 1}
    assert _row(model, "intg")["counts"]["aws_sns_topic"] == 0
    assert all(_row(model, n)["uncertain"] == {} for n in WIRED)


def test_unresolved_count_is_not_assumed_to_be_one(model):
    # no environment applied: var.lambdas_enabled is unset, so both the base-module and the
    # legacy functions may exist; none is counted (was 8, one per block)
    assert purpose_text(model).startswith("deploys 0 (+8 uncertain) Lambda functions")
    view = environments_view(model)
    assert view["current"]["counts"]["aws_lambda_function"] == 0
    assert view["current"]["uncertain"]["aws_lambda_function"] == 8
    assert view["current"]["text"].startswith("0 (+8 uncertain) Lambda functions")
    assert count_text(4, 3) == "4 (+3 uncertain)" and count_text(4) == "4"


def test_why_counted_names_expression_value_and_source(model):
    nodes = {n["id"]: n for n in model["nodes"]}
    why = nodes["module.api.module.base.aws_lambda_function.example"]["properties"]["count_why"]
    assert why["known"] is False
    (step,) = why["steps"]
    assert (step["address"], step["meta"]) == ("module.api.module.base", "count")
    assert step["expression"] == "var.enabled ? 1 : 0"
    assert step["instances"] is None and step["value"] == "not known"
    assert step["location"]["file"] == "modules/lambda_wrapper/main.tf"
    (src,) = step["sources"]
    assert (src["ref"], src["origin"], src["expression"]) == (
        "var.enabled",
        "module input",
        "var.lambdas_enabled",
    )
    assert src["location"] == {"file": "main.tf", "start_line": 23, "end_line": 23}
    (inner,) = src["from"]
    assert (inner["ref"], inner["origin"], inner["known"]) == (
        "var.lambdas_enabled",
        "unset",
        False,
    )
    assert "var.lambdas_enabled is not set" in source_text(src)
    # a known count also says where its value comes from
    archive = nodes["aws_s3_bucket.archive"]["properties"]["count_why"]
    assert archive["steps"][0]["expression"] == 'var.env == "prod" ? 1 : 0'


def test_why_counted_with_an_environment_names_the_tfvars():
    m = run_analysis(FIXTURE, git=False, env="dev").model
    nodes = {n["id"]: n for n in m["nodes"]}
    why = nodes["module.api.module.base.aws_lambda_function.example"]["properties"]["count_why"]
    assert why["known"] is True
    (step,) = why["steps"]
    assert step["instances"] == 1 and step["value"] == "1"
    (inner,) = step["sources"][0]["from"]
    assert (inner["origin"], inner["file"], inner["value"]) == (
        "tfvars",
        "env/dev.tfvars",
        "true",
    )
    assert inner["location"]["file"] == "env/dev.tfvars"
    assert "var.lambdas_enabled = true from env/dev.tfvars:2" in why["text"]
    assert purpose_text(m).startswith("deploys 4 Lambda functions")


def test_overview_lines_explain_uncertain_counts(model):
    ov = build_overview(model, "env-layout")
    texts = [ln["text"] for ln in ov["purpose"]]
    assert "0 (+1 uncertain) SNS topics: alerts" in texts
    why = next(t for t in texts if t.startswith("why counted: aws_sns_topic.alerts"))
    assert "var.alerts_enabled is not set" in why
    lines = [ln["text"] for ln in ov["environment_counts"]]
    intg = next(t for t in lines if t.startswith("intg"))
    assert intg.startswith("intg (not wired): 4 Lambda functions")
    assert "0 (+1 uncertain) SNS topics" in intg
    assert "+1 SNS topic uncertain: count not known statically (not counted)" in lines
    assert "differences between environments:" in lines
    assert "aws_s3_bucket.archive only in prod" in lines


# -- 4. service inventory and differences -----------------------------------------------------


def test_inventory_table_with_envs_as_columns(model):
    view = environments_view(model)
    assert view["inventory_columns"] == "environments"
    keys = [r["key"] for r in view["inventory"]]
    assert keys == [
        "lambda",
        "ecs_cluster",
        "ecs_service",
        "ec2",
        "alb",
        "nlb",
        "sns",
        "sqs",
        "dynamodb",
        "eventbridge",
        "log_group",
        "ecr",
        "s3",
        "ebs",
    ]
    rows = {r["id"]: r for r in view["environments"]}
    for name in ENVS:
        inv = rows[name]["inventory"]
        assert (inv["lambda"]["count"], inv["ecs_service"]["count"]) == (4, 1)
        assert (inv["alb"]["count"], inv["nlb"]["count"]) == (1, 0)
        assert inv["log_group"]["count"] == 4
        names = [i["name"] for i in inv["lambda"]["items"]]
        assert sorted(names) == [f"{name}-{x}" for x in LAMBDAS]
        item = inv["lambda"]["items"][0]
        assert item["location"]["file"] == "modules/lambda_base/main.tf"
        assert item["why"]["steps"][0]["expression"] == "var.enabled ? 1 : 0"
    assert rows["prod"]["inventory"]["s3"]["count"] == 1
    assert rows["intg"]["inventory"]["sns"] == {
        "count": 0,
        "uncertain": 1,
        "items": rows["intg"]["inventory"]["sns"]["items"],
    }


def test_differences_between_environments_are_flagged(model):
    view = environments_view(model)
    by = {r["key"]: r for r in view["inventory"]}
    assert by["s3"]["differs"] and by["s3"]["only"] == {"aws_s3_bucket.archive": ["prod"]}
    assert by["sns"]["differs"] and by["sns"]["only"] == {}
    assert not by["lambda"]["differs"] and not by["ecs_service"]["differs"]
    # columns in promotion order (dev | intg | uat | prod), never summed
    assert view["differences"] == [
        "SNS topics: dev 1, intg 0 (+1 uncertain), uat 1, prod 1",
        "S3 buckets: dev 0, intg 0, uat 0, prod 1",
        "  aws_s3_bucket.archive only in prod",
    ]


def test_load_balancers_by_type(tmp_path):
    (tmp_path / "main.tf").write_text(
        'variable "lb_type" {\n  type = string\n}\n'
        'resource "aws_lb" "web" {\n  name = "example-web"\n}\n'
        'resource "aws_lb" "tcp" {\n  name               = "example-tcp"\n'
        '  load_balancer_type = "network"\n}\n'
        'resource "aws_lb" "x" {\n  name               = "example-x"\n'
        "  load_balancer_type = var.lb_type\n}\n"
        'resource "aws_ebs_volume" "data" {\n  availability_zone = "us-east-1a"\n  size = 10\n}\n'
    )
    m = run_analysis(tmp_path, git=False).model
    keys = {n["id"]: inventory_key(n) for n in m["nodes"] if n["kind"] == "resource"}
    assert keys == {
        "aws_lb.web": "alb",
        "aws_lb.tcp": "nlb",
        "aws_lb.x": "lb_unknown",
        "aws_ebs_volume.data": "ebs",
    }
    inv = inventory(m["nodes"])
    assert (inv["alb"]["count"], inv["nlb"]["count"], inv["ebs"]["count"]) == (1, 1, 1)
    # no environment: one column, the optional "type not known" row only when needed
    view = environments_view(m)
    assert view["inventory_columns"] == "current" and view["environments"] == []
    assert "lb_unknown" in [r["key"] for r in view["inventory"]]
    assert "gwlb" not in [r["key"] for r in view["inventory"]]


# -- 5. the views follow the selected environment ---------------------------------------------


def test_api_environments_view_and_switch(tmp_path):
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True))
    status, view = _call(api, "/api/environments")
    assert status == 200 and view["active"] == "dev" and view["default"]
    assert view["not_wired"] == ["intg"]
    assert [r["id"] for r in view["environments"]] == ENVS
    assert view["differences"][-1] == "  aws_s3_bucket.archive only in prod"
    status, view = _call(api, "/api/env?name=prod", "POST")
    assert status == 200 and view["active"] == "prod"
    titles = sorted(c["title"] for c in _call(api, "/api/flow")[1]["chains"])
    assert titles == sorted(f"prod-{x} (no trigger)" for x in [*LAMBDAS, "app"])
    for name in ("network", "iam"):
        status, data = _call(api, f"/api/root-view?root=.&view={name}")
        text = json.dumps(data)
        assert status == 200 and "prod-api" in text and "dev-api" not in text
    svg = api.handle("GET", "/api/diagram?name=iam&format=svg&root=.").body.decode()
    assert "prod-" in svg and "dev-api" not in svg


def test_network_and_iam_views_follow_the_environment():
    for env in ("uat", "intg"):
        m = run_analysis(FIXTURE, git=False, env=env).model
        for view in ("network", "iam"):
            text = json.dumps(build_root_view(m, ".", view))
            assert f"{env}-api" in text
            assert all(f"{other}-api" not in text for other in ENVS if other != env)


def test_start_here_opens_the_environments_view(model):
    lines = build_flows(model)["start_here"]
    assert lines[0]["view"] == "environments"
    assert lines[0]["text"] == ("Environments (4: dev, intg, prod, uat): start with their files")
    intg = next(ln for ln in lines if ln["text"].startswith("intg"))
    assert "not wired" in intg["text"] and intg["status"] == "skipped"


def test_ui_environments_tab_first(tmp_path):
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json"))
    html = api.handle("GET", "/").body.decode()
    tabs = re.findall(r'<button type="button" class="([^"]+)" data-view="(\w+)"', html)
    assert tabs[0] == ("tab active", "environments")
    assert '<div id="environments" class="view-environments">' in html
    assert '<div id="env-toc"></div>' in html
    js = api.handle("GET", "/static/app.js").body.decode()
    for hook in (
        "renderEnvironments(envs)",
        "function showInventoryCell(row, column, cell)",
        "`${known} (+${uncertain} uncertain)`",
        '"State file per environment"',
        '"Differences between environments"',
        "link(ln.text, () => setView(ln.view))",
        "selectEnv(id)",
        "shownRoot.view",
    ):
        assert hook in js, hook
    css = api.handle("GET", "/static/app.css").body.decode()
    assert "table.env-inventory td.diff" in css
