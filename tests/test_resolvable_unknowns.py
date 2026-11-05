"""The resolution pass resolves what the repository provably sets, and the run log says why the
rest stayed unresolved.

``resolvable_unknowns/stacks/app``: a variable set per environment in tfvars, a local built on
it, a variable default and backend values in ``envs/backend-<env>.hcl``; ``var.never_set`` is set
nowhere, so it is the only thing that stays unresolved.
"""

from __future__ import annotations

import json
import logging
import shutil

import pytest
from conftest import FIXTURES

from tflens.jobs import JobManager
from tflens.output import run_analysis
from tflens.pipeline import NullProgress, Options, discover
from tflens.webapi import Api, ModelStore

FIXTURE = FIXTURES / "resolvable_unknowns"
APP = "stacks/app"
TOKEN = "example-token-value"


class Recording(NullProgress):
    def __init__(self):
        self.lines = []

    def log(self, text):
        self.lines.append(text)


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False, auto_env=True).model


def _attr(model, address, attr):
    (node,) = [n for n in model["nodes"] if n["id"] == address]
    return node["attributes"][attr]


def _row(model, env):
    rows = model["environment_analysis"]["environments"]
    return next(r for r in rows if r["id"] == env)


def _names(row, key):
    return {i["id"]: i["name"] for i in row["inventory"][key]["items"]}


def test_a_var_set_per_env_in_tfvars_resolves_for_each_environment(model):
    assert _attr(model, "aws_sqs_queue.per_env", "name") == "dev"
    assert _names(_row(model, "prod"), "sqs")["aws_sqs_queue.per_env"] == "prod"


def test_a_local_built_on_it_resolves_for_each_environment(model):
    assert _attr(model, "aws_sqs_queue.from_local", "name") == "example-dev-jobs"
    assert _names(_row(model, "prod"), "sqs")["aws_sqs_queue.from_local"] == "example-prod-jobs"


def test_a_variable_default_resolves(model):
    assert _attr(model, "aws_sns_topic.from_default", "name") == "standard"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_a_backend_value_in_envs_backend_env_hcl_resolves(model, env):
    found = next(e for e in model["structure"]["environments"] if e["name"] == env)
    assert found["backend_hcl"] == [f"{APP}/envs/backend-{env}.hcl"]
    settings = {s["key"]: s["value"] for s in found["state"]["settings"] if s["known"]}
    assert settings["bucket"] == f"example-tfstate-{env}"
    assert settings["key"] == "app/terraform.tfstate"
    assert settings["region"] == "us-east-1"
    assert found["state"]["unresolved"] == []


def test_only_what_nothing_sets_stays_unknown(model):
    assert [(u["subject"], u["attribute"]) for u in model["unknowns"]] == [
        ("aws_s3_bucket.unresolvable", "bucket")
    ]


def test_the_counts_show_what_the_pass_resolved(model):
    # the regression: 96 → 96. The first pass already had the tfvars, so with no source to follow
    # and no data source to read the two passes evaluated the same inputs.
    assert model["resolution"]["after"] == 1
    assert model["resolution"]["before"] > model["resolution"]["after"]


def test_the_first_pass_is_the_code_without_the_environments_files(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    for f in (repo / APP / "envs").glob("*.tfvars"):
        f.unlink()
    without = run_analysis(repo, git=False, auto_env=True).model["resolution"]
    with_files = run_analysis(FIXTURE, git=False, auto_env=True).model["resolution"]
    # same code, same first pass; only the pass that reads the tfvars differs
    assert without["before"] == without["after"] == with_files["before"]


def test_a_value_set_in_a_ci_script_is_resolved_by_the_pass_not_before_it(tmp_path):
    (tmp_path / "main.tf").write_text(
        'variable "name" {\n  type = string\n}\n'
        'resource "aws_sqs_queue" "q" {\n  name = var.name\n}\n'
    )
    (tmp_path / "deploy.sh").write_text('terraform apply -var "name=example-q"\n')
    model = run_analysis(tmp_path, git=False, auto_env=True).model
    assert model["resolution"] == {"before": 1, "after": 0}


# -- the split environment -------------------------------------------------------------------


def _split_repo(tmp_path, *tfvars):
    (tmp_path / "main.tf").write_text(
        'terraform {\n  backend "s3" {}\n}\nvariable "env_name" {\n  type = string\n}\n'
        'resource "aws_sqs_queue" "q" {\n  name = var.env_name\n}\n'
    )
    (tmp_path / "envs").mkdir()
    (tmp_path / "envs/backend-dev.hcl").write_text(
        'bucket = "example-tfstate"\nkey = "app.tfstate"\nregion = "us-east-1"\n'
    )
    for name in tfvars:
        (tmp_path / "envs" / name).write_text('env_name = "example"\n')
    return run_analysis(tmp_path, git=False, auto_env=True).model


@pytest.mark.parametrize("name", ["dev-us-east-1.tfvars", "app-dev.tfvars", "dev_app.tfvars"])
def test_tfvars_and_a_backend_file_named_differently_are_one_environment(tmp_path, name):
    model = _split_repo(tmp_path, name)
    (env,) = model["structure"]["environments"]
    assert env["name"] == "dev"
    assert env["var_files"] == [f"envs/{name}"]
    assert env["backend_hcl"] == ["envs/backend-dev.hcl"]
    assert model["unknowns"] == []


def test_several_tfvars_naming_one_environment_stay_apart(tmp_path):
    model = _split_repo(tmp_path, "dev-us-east-1.tfvars", "dev-eu-west-1.tfvars")
    names = {e["name"] for e in model["structure"]["environments"]}
    assert names == {"dev", "dev-us-east-1", "dev-eu-west-1"}


def test_non_prod_tfvars_are_not_prod(tmp_path):
    (tmp_path / "main.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
    (tmp_path / "envs").mkdir()
    (tmp_path / "envs/backend-prod.hcl").write_text('bucket = "example"\n')
    (tmp_path / "envs/non-prod.tfvars").write_text('env_name = "example"\n')
    model = run_analysis(tmp_path, git=False, auto_env=True).model
    prod = next(e for e in model["structure"]["environments"] if e["name"] == "prod")
    assert prod["var_files"] == []


# -- the run log -----------------------------------------------------------------------------


def _unresolved_lines(lines):
    start = next(i for i, line in enumerate(lines) if line.startswith("unresolved: "))
    return lines[start:]


def test_the_run_log_says_why_each_unknown_stayed_unresolved(caplog):
    prog = Recording()
    with caplog.at_level(logging.INFO, logger="tflens"):
        discover(FIXTURE, Options(git=False, auto_env=True), prog)
    lines = _unresolved_lines(prog.lines)
    assert lines[0] == "unresolved: 1 value(s), grouped by reason"
    assert lines[1] == "  missing variable (1):"
    assert lines[2].startswith("    aws_s3_bucket.unresolvable bucket: variable var.never_set")
    assert f"[{APP}/main.tf:36]" in lines[2] and f"defined at {APP}/main.tf:15" in lines[2]
    assert f"looked in {APP}/envs/dev.tfvars" in lines[2]
    assert "var.never_set" in caplog.text  # the log file too


def test_the_log_groups_by_reason_largest_first(tmp_path):
    (tmp_path / "main.tf").write_text(
        'terraform {\n  backend "s3" {}\n}\n'
        'variable "a" {\n  type = string\n}\nvariable "b" {\n  type = string\n}\n'
        'resource "aws_sqs_queue" "one" {\n  name = var.a\n}\n'
        'resource "aws_sqs_queue" "two" {\n  name = var.b\n}\n'
        'resource "aws_sns_topic" "workspace" {\n  name = terraform.workspace\n}\n'
    )
    prog = Recording()
    discover(tmp_path, Options(git=False, auto_env=True), prog)
    lines = _unresolved_lines(prog.lines)
    assert lines[0] == "unresolved: 3 value(s), grouped by reason"
    headers = [line.strip() for line in lines[1:] if line.startswith("  ") and line[2] != " "]
    assert headers[:2] == ["missing variable (2):", "workspace (1):"]
    per_unknown = [line for line in lines[1:] if line.startswith("    ")]
    assert len(per_unknown) == 3


def test_a_credential_in_a_source_url_never_reaches_the_log(tmp_path, caplog):
    (tmp_path / "main.tf").write_text(
        f'module "q" {{\n  source = "git::https://example:{TOKEN}@example.invalid/org/q.git"\n}}\n'
        'resource "aws_sqs_queue" "q" {\n  name = module.q.name\n}\n'
    )
    prog = Recording()
    with caplog.at_level(logging.DEBUG, logger="tflens"):
        discover(tmp_path, Options(git=False, auto_env=True), prog)
    assert TOKEN not in "\n".join(prog.lines) and TOKEN not in caplog.text


def test_nothing_unresolved_logs_no_group(tmp_path):
    (tmp_path / "main.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "example"\n}\n')
    prog = Recording()
    discover(tmp_path, Options(git=False, auto_env=True), prog)
    assert not [line for line in prog.lines if line.startswith("unresolved: ")]


def test_the_progress_screen_shows_the_groups(tmp_path):
    jobs = JobManager()
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True), jobs)
    final = jobs.wait(json.loads(api.handle("POST", "/api/discover").body)["job"]["id"], 120)
    assert "  missing variable (1):" in final["log"]
    assert final["state"] == "done"
