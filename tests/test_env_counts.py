"""Environments first, per-environment counts, and no double counting (envs.py).

The ``env_counts`` fixture: one root, three environments (dev / uat / prod) through a
partial backend (``backend "s3" {}``) plus ``backend.<env>.hcl``, ``environments/<env>.tfvars``
and a CI workflow passing both. Four Lambda functions go wrapper module -> base module
(``count = var.lambda_count``); the wrapper's legacy function and the canary ECS service
have ``count`` conditionals that are 0. Per environment: 1 ECS service, 4 Lambda
functions, 0 unknowns; in total 3 and 12.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens.cli import main
from tflens.detect import backend_env
from tflens.envs import counts, unknown_category
from tflens.flow import build_flows
from tflens.output import run_analysis
from tflens.overview import build_overview, render_overview_markdown
from tflens.webapi import Api, ModelStore

FIXTURE = FIXTURES / "env_counts"
ENVS = ["dev", "prod", "uat"]


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False).model


def _env(model, name):
    return next(e for e in model["structure"]["environments"] if e["name"] == name)


def _row(model, name):
    rows = model["environment_analysis"]["environments"]
    return next(r for r in rows if r["id"] == name)


def _api(tmp_path, **kw) -> Api:
    return Api(ModelStore(FIXTURE, tmp_path / "model.json", **kw))


def _call(api, path, method="GET"):
    r = api.handle(method, path)
    return r.status, json.loads(r.body)


# -- 1. environments first ----------------------------------------------------------------


def test_environments_and_their_files_are_detected(model):
    envs = model["structure"]["environments"]
    assert [(e["name"], e["root"]) for e in envs] == [(n, ".") for n in ENVS]
    for name in ENVS:
        e = _env(model, name)
        assert e["backend_hcl"] == [f"backend.{name}.hcl"]
        assert e["var_files"] == [f"environments/{name}.tfvars"]
        uses = {(u["flag"], u["file"]) for u in e["var_file_cli"]}
        assert uses == {
            ("-backend-config", f"backend.{name}.hcl"),
            ("-var-file", f"environments/{name}.tfvars"),
        }
        for u in e["var_file_cli"]:
            assert u["location"]["file"] == ".github/workflows/deploy.yml"
    root = model["structure"]["roots"][0]
    assert root["environments"] == ENVS


def test_partial_backend_settings_come_per_environment(model):
    backend = model["structure"]["roots"][0]["backend"]
    assert backend["type"] == "s3"
    assert [f["file"] for f in backend["config_files"]] == [
        "backend.dev.hcl",
        "backend.prod.hcl",
        "backend.uat.hcl",
    ]
    bucket = next(s for s in backend["settings"] if s["key"] == "bucket")
    assert bucket["per_environment"] == {
        "dev": "example-tfstate-dev",
        "prod": "example-tfstate-prod",
        "uat": "example-tfstate-uat",
    }
    key = next(s for s in backend["settings"] if s["key"] == "key")
    assert key["value"] == "app/terraform.tfstate"  # the same in every environment


def test_start_here_begins_with_the_environments_and_their_files(model):
    lines = build_flows(model)["start_here"]
    assert lines[0]["text"] == "Environments (3: dev, prod, uat): start with their files"
    texts = [ln["text"] for ln in lines]
    dev = texts.index("dev")
    assert texts[dev + 1 : dev + 3] == [
        "backend config: backend.dev.hcl",
        "tfvars: environments/dev.tfvars",
    ]
    assert "CI: -var-file=environments/*.tfvars (.github/workflows/deploy.yml)" in texts
    # the root (and its backend.tf / main.tf) only after the environments
    root = next(i for i, t in enumerate(texts) if t.startswith("Root directory:"))
    assert root > texts.index("uat")


def test_backend_env_names():
    assert backend_env("backend.dev.hcl") == "dev"
    assert backend_env("config/prod.backend.hcl") == "prod"
    assert backend_env("backend/uat.hcl") == "uat"
    assert backend_env("envs/prod/backend.tfbackend") == "prod"
    assert backend_env("backend.hcl") is None


def test_backend_dir_and_ci_only_environments(tmp_path):
    """backend/<env>.hcl names environments by itself; a CI -var-file of a literal env
    file is attached to it."""
    (tmp_path / "main.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
    (tmp_path / "backend").mkdir()
    for env in ("dev", "prod"):
        (tmp_path / "backend" / f"{env}.hcl").write_text(f'bucket = "example-{env}"\n')
    (tmp_path / "Makefile").write_text(
        "plan:\n\tterraform init -backend-config=backend/$(ENV).hcl\n"
    )
    m = run_analysis(tmp_path, git=False).model
    envs = {e["name"]: e for e in m["structure"]["environments"]}
    assert sorted(envs) == ["dev", "prod"]
    assert envs["dev"]["kinds"] == ["backend"]
    assert envs["dev"]["backend_hcl"] == ["backend/dev.hcl"]
    (use,) = envs["prod"]["var_file_cli"]
    assert (use["flag"], use["value"], use["location"]["file"]) == (
        "-backend-config",
        "backend/*.hcl",
        "Makefile",
    )


# -- 2. per-environment analysis ----------------------------------------------------------


def test_counts_per_environment_and_total(model):
    analysis = model["environment_analysis"]
    for name in ENVS:
        r = _row(model, name)
        assert r["counts"]["aws_ecs_service"] == 1
        assert r["counts"]["aws_lambda_function"] == 4
        assert r["unknown_count"] == 0 and r["unknowns"] == []
        assert r["uncertain"] == {}
        assert [f["name"] for f in r["lambda_functions"]] == [
            f"{name}-api",
            f"{name}-notifier",
            f"{name}-scheduler",
            f"{name}-worker",
        ]
        assert [s["name"] for s in r["ecs_services"]] == [f"{name}-app"]
    total = analysis["total"]
    assert (total["ecs_services"], total["lambda_functions"], total["unknown_count"]) == (3, 12, 0)
    assert total["counts"]["aws_ecs_service"] == 3
    assert total["counts"]["aws_lambda_function"] == 12


def test_cli_env_applies_the_environment(tmp_path):
    out = tmp_path / "model.json"
    assert main(["--dir", str(FIXTURE), "--env", "uat", "--out", str(out), "--no-git"]) == 0
    m = json.loads(out.read_text())
    assert m["environment"]["name"] == "uat"
    assert m["environment"]["var_files"] == ["environments/uat.tfvars"]
    assert m["unknowns"] == []
    names = {
        n["attributes"]["function_name"] for n in m["nodes"] if n["type"] == "aws_lambda_function"
    }
    assert names == {"uat-api", "uat-notifier", "uat-scheduler", "uat-worker"}
    assert main(["--dir", str(FIXTURE), "--env", "staging", "--out", str(out)]) == 2


def test_api_environment_switcher(tmp_path):
    api = _api(tmp_path)
    status, view = _call(api, "/api/environments")
    assert status == 200 and view["active"] is None and view["switchable"]
    assert [r["id"] for r in view["environments"]] == ENVS
    assert [t["type"] for t in view["types"]][:1] == ["aws_lambda_function"]
    assert view["total"]["counts"]["aws_lambda_function"] == 12
    # selecting an environment re-analyzes with its tfvars: the counts follow
    status, view = _call(api, "/api/env?name=prod", "POST")
    assert status == 200 and view["active"] == "prod"
    assert view["current"]["counts"]["aws_lambda_function"] == 4
    assert view["current"]["unknown_count"] == 0
    assert _call(api, "/api/summary")[1]["environment"] == "prod"
    assert _call(api, "/api/summary")[1]["summary"]["unknowns"] == 0
    status, err = _call(api, "/api/env?name=nope", "POST")
    assert status == 404 and "dev, prod, uat" in err["detail"]
    assert _call(api, "/api/environments")[1]["active"] == "prod"  # the selection is kept
    status, view = _call(api, "/api/env?name=", "POST")
    assert status == 200 and view["active"] is None


def test_recorded_run_cannot_switch(tmp_path):
    model = run_analysis(FIXTURE, git=False).model
    (tmp_path / "model.json").write_text(json.dumps(model))
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", readonly=True))
    status, view = _call(api, "/api/environments")
    assert status == 200 and not view["switchable"] and len(view["environments"]) == 3
    assert _call(api, "/api/env?name=dev", "POST")[0] == 405


def test_overview_lists_counts_per_environment(model):
    ov = build_overview(model, "env-counts")
    texts = [ln["text"] for ln in ov["environment_counts"]]
    assert texts[0].startswith("dev: 4 Lambda functions · 1 ECS cluster · 1 ECS service")
    assert texts[0].endswith("0 unknowns — backend.dev.hcl, environments/dev.tfvars")
    assert texts[-1].startswith("total: 12 Lambda functions · 3 ECS clusters · 3 ECS services")
    md = render_overview_markdown(ov)
    assert "## Counts per environment" in md


# -- 3. no double counting -----------------------------------------------------------------


def test_blocks_that_are_not_created_are_left_out(model):
    ids = {n["id"] for n in model["nodes"]}
    assert not {i for i in ids if ".legacy" in i}
    assert "aws_ecs_service.canary" not in ids
    omitted = {d["subject"]: d["reason"] for d in model["not_created"]}
    assert omitted["aws_ecs_service.canary"] == "count = 0: not created"
    assert omitted["module.api.aws_lambda_function.legacy"] == "count = 0: not created"
    lambdas = [n["id"] for n in model["nodes"] if n["type"] == "aws_lambda_function"]
    assert lambdas == [
        f"module.{m}.module.base.aws_lambda_function.example"
        for m in ("api", "notifier", "scheduler", "worker")
    ]


def test_one_flow_per_real_function():
    for m in (
        run_analysis(FIXTURE, git=False).model,
        run_analysis(FIXTURE, git=False, env="dev").model,
    ):
        chains = build_flows(m)["chains"]
        entries = [c["entry"] for c in chains if c["group"] == "Lambda"]
        assert len(entries) == len(set(entries)) == 4
        assert len([c for c in chains if c["group"] == "ECS"]) == 1
    titles = sorted(c["title"] for c in chains)
    assert titles == [
        "dev-api (no trigger)",
        "dev-app (no trigger)",
        "dev-notifier (no trigger)",
        "dev-scheduler (no trigger)",
        "dev-worker (no trigger)",
    ]


WRAPPER = """
variable "name" {
  type = string
}
variable "use_base_module" {
  type = bool
}
module "base" {
  source        = "../base"
  count         = var.use_base_module ? 1 : 0
  function_name = var.name
}
resource "aws_lambda_function" "legacy" {
  count         = var.use_base_module ? 0 : 1
  function_name = var.name
  role          = "arn:aws:iam::123456789012:role/example"
  handler       = "index.handler"
  runtime       = "python3.12"
}
"""
BASE = """
variable "function_name" {
  type = string
}
resource "aws_lambda_function" "example" {
  function_name = var.function_name
  role          = "arn:aws:iam::123456789012:role/example"
  handler       = "index.handler"
  runtime       = "python3.12"
}
"""


def _wrapper_repo(tmp_path: Path) -> Path:
    (tmp_path / "modules" / "wrapper").mkdir(parents=True)
    (tmp_path / "modules" / "base").mkdir()
    (tmp_path / "modules" / "wrapper" / "main.tf").write_text(WRAPPER)
    (tmp_path / "modules" / "base" / "main.tf").write_text(BASE)
    (tmp_path / "main.tf").write_text(
        'variable "use_base_module" {\n  type = bool\n}\n'
        'module "fn" {\n  source          = "./modules/wrapper"\n'
        '  name            = "example"\n  use_base_module = var.use_base_module\n}\n'
    )
    (tmp_path / "envs").mkdir()
    (tmp_path / "envs" / "dev.tfvars").write_text("use_base_module = true\n")
    (tmp_path / "envs" / "prod.tfvars").write_text("use_base_module = false\n")
    return tmp_path


def test_wrapper_and_base_function_counted_once_per_environment(tmp_path):
    m = run_analysis(_wrapper_repo(tmp_path), git=False).model
    # without the environment's variables both blocks may exist: each is flagged uncertain
    instances, uncertain = counts(m["nodes"])
    assert uncertain == {"aws_lambda_function": 2}
    reasons = {u["reason"] for u in m["unknowns"] if u["attribute"] == "count"}
    assert reasons == {
        "conditional depends on unknown value (variable var.use_base_module has no default "
        "and no value in tfvars/--var-file)"
    }
    # with them, exactly one of the two is created
    dev, prod = (_row(m, n) for n in ("dev", "prod"))
    assert dev["counts"] == prod["counts"] == {"aws_lambda_function": 1}
    assert [f["id"] for f in dev["lambda_functions"]] == [
        "module.fn.module.base.aws_lambda_function.example"
    ]
    assert [f["id"] for f in prod["lambda_functions"]] == ["module.fn.aws_lambda_function.legacy"]
    assert dev["unknown_count"] == prod["unknown_count"] == 0


# -- 4. remaining unknowns, with why ---------------------------------------------------------


def test_unknowns_without_environment_are_reported_with_reasons(model):
    unknowns = {(u["subject"], u["attribute"]): u["reason"] for u in model["unknowns"]}
    fn = "module.api.module.base.aws_lambda_function.example"
    assert unknowns[(fn, "count")] == (
        "variable var.lambda_count has no default and no value in tfvars/--var-file"
    )
    node = next(n for n in model["nodes"] if n["id"] == fn)
    assert node["properties"]["created"] is None
    assert "lambda_count" in node["properties"]["created_unknown"]


def test_single_instance_count_index_and_each_are_known(tmp_path):
    (tmp_path / "main.tf").write_text(
        'resource "aws_sqs_queue" "q" {\n  count = 1\n  name  = "example-${count.index}"\n}\n'
        'resource "aws_sns_topic" "t" {\n  for_each = toset(["a"])\n'
        '  name     = "example-${each.key}"\n}\n'
        'resource "aws_sqs_queue" "many" {\n  count = 2\n  name  = "example-${count.index}"\n}\n'
    )
    m = run_analysis(tmp_path, git=False).model
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["aws_sqs_queue.q"]["attributes"]["name"] == "example-0"
    assert nodes["aws_sns_topic.t"]["attributes"]["name"] == "example-a"
    many = nodes["aws_sqs_queue.many"]
    assert many["properties"]["created"] == 2
    # several instances: the node shows the first one's values, and says so
    assert many["attributes"]["name"] == "example-0"
    assert many["properties"]["instance_shown"] == "values shown for count.index = 0"
    assert m["unknowns"] == []


def test_unknown_module_count_is_reported(tmp_path):
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "main.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "x"\n}\n')
    (tmp_path / "main.tf").write_text(
        'variable "on" {\n  type = bool\n}\n'
        'module "m" {\n  source = "./mod"\n  count  = var.on ? 1 : 0\n}\n'
    )
    m = run_analysis(tmp_path, git=False).model
    (u,) = [u for u in m["unknowns"] if u["subject"] == "module.m"]
    assert u["attribute"] == "count" and "var.on has no default" in u["reason"]
    q = next(n for n in m["nodes"] if n["id"] == "module.m.aws_sqs_queue.q")
    assert q["properties"]["created"] is None


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("variable var.x has no default and no value in tfvars/--var-file", "missing variable"),
        (
            "conditional depends on unknown value (variable var.x has no default)",
            "conditional without default",
        ),
        ("dynamic block is not expanded statically", "dynamic block"),
        ("count.index differs per resource instance", "per-instance value"),
        ("aws_s3_bucket.b.arn is only known after apply", "known after apply"),
        (
            "module.m uses non-local source 'x'; remote modules are not fetched",
            "module not analyzed",
        ),
        ("something else", "other"),
    ],
)
def test_unknown_categories(reason, category):
    assert unknown_category(reason) == category
