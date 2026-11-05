"""The AWS phase runs when asked for, backends resolve per stack:env, unknowns get a real
resolution pass, and the run's summary says what happened.

``backend_resolution``: ``stacks/api`` has a backend file per environment, ``stacks/worker``
lacks the one of nonprod, ``stacks/batch`` resolves through a file outside its tree and a
deploy script, ``stacks/report`` has no backend file at all.
"""

from __future__ import annotations

import json
import shutil
import sys
import types

import pytest
from conftest import FIXTURES

from tflens.awscheck import (
    INVALID_CREDENTIALS,
    MISSING_CREDENTIALS,
    MISSING_PERMISSION,
    MISSING_REGION,
    AwsTarget,
    run_checks,
    target_from_account,
)
from tflens.jobs import JobBusy, JobManager
from tflens.output import run_analysis
from tflens.pipeline import NullProgress, Options, confirmed_only, discover
from tflens.sources import same_repo_subdir
from tflens.web import create_app
from tflens.webapi import Api, ModelStore

FIXTURE = FIXTURES / "backend_resolution"
API, WORKER, BATCH, REPORT = (f"stacks/{s}" for s in ("api", "worker", "batch", "report"))
UI_DATA = ("summary", "environments", "stack-cards", "env-matrix", "overview", "model", "shapes")
# what a saved account holds: never in a log, a status or a model
SECRET = "example-secret-value"
KEY_ID = "AKIAEXAMPLEEXAMPLE00"


@pytest.fixture(scope="module")
def model():
    return run_analysis(FIXTURE, git=False, auto_env=True).model


def _env(model, root, name):
    return next(
        (e for e in model["structure"]["environments"] if e["root"] == root and e["name"] == name),
        None,
    )


def _settings(env):
    return {s["key"]: s["value"] for s in env["state"]["settings"] if s["known"]}


# -- item 2: per stack:env ------------------------------------------------------------------


def test_a_stack_lacking_a_backend_file_for_an_env_loses_that_pair_only(model):
    assert _env(model, WORKER, "nonprod") is None
    assert _env(model, WORKER, "dev") is not None
    assert _env(model, API, "nonprod") is not None  # the sibling stack keeps its own
    stack = next(s for s in model["stacks"] if s["id"] == WORKER)
    assert stack["environments"] == [f"{WORKER}:dev"]
    rows = {r["id"] for r in model["environment_analysis"]["environments"]}
    assert f"{WORKER}:nonprod" not in rows and f"{API}:nonprod" in rows


def test_the_drop_is_only_in_the_debug_trace_with_why(model):
    trace = [
        t for t in model["structure"]["env_files"] if t["stack"] == WORKER and not t["accepted"]
    ]
    assert any("dropped" in t["reason"] and t["env"] == "nonprod" for t in trace)


def test_the_string_unknown_is_never_served_for_a_backend(tmp_path):
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True))
    for name in UI_DATA:
        resp = api.handle("GET", f"/api/{name}")
        assert resp.status == 200, name
        assert "UNKNOWN" not in resp.body.decode(), name
    envs = json.loads(api.handle("GET", "/api/environments").body)
    assert not [r["id"] for r in envs["environments"] if r["id"] == f"{WORKER}:nonprod"]
    matrix = json.loads(api.handle("GET", "/api/env-matrix").body)
    worker = next(r for r in matrix["rows"] if r["stack"] == WORKER)
    assert sorted(worker["cells"]) == ["dev"]


def test_the_ui_code_has_no_placeholder_for_a_backend_setting():
    from tflens.webapi import STATIC_DIR

    js = (STATIC_DIR / "app.js").read_text()
    for gone in ('"UNKNOWN");', '"UNKNOWN"),', 's.known ? String(s.value) : "UNKNOWN"'):
        assert gone not in js, gone


def test_a_backend_file_under_an_envs_dir_outside_the_stack_is_found(model):
    env = _env(model, BATCH, "nonprod")
    assert env["backend_hcl"] == ["envs/batch/backend-nonprod.hcl"]
    assert _settings(env)["dynamodb_table"] == "example-lock"  # the file's
    assert _settings(env)["key"] == "batch/terraform.tfstate"  # the script's, applied after it
    # it is this stack's: no environment of the top directory comes out of it
    assert _env(model, ".", "nonprod") is None


def test_a_file_naming_another_stack_is_not_borrowed(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "stacks/worker/envs/backend-nonprod.hcl").unlink(missing_ok=True)
    m = run_analysis(repo, git=False, auto_env=True).model
    assert _env(m, WORKER, "nonprod") is None  # envs/batch/backend-nonprod.hcl is batch's


def test_backend_config_settings_of_a_script_resolve_with_the_environment(model):
    env = _env(model, BATCH, "dev")
    assert env["backend_hcl"] == []
    assert _settings(env) == {
        "bucket": "example-tfstate-dev",
        "key": "batch/terraform.tfstate",
        "region": "us-east-1",
    }
    location = next(s for s in env["state"]["settings"] if s["key"] == "bucket")["location"]
    assert location["file"] == "scripts/deploy.sh"
    assert env["state"]["unresolved"] == []


def test_a_setting_only_known_at_run_time_names_the_line(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "repo")
    script = tmp_path / "repo/scripts/deploy.sh"
    script.write_text(
        'cd stacks/batch && terraform init -backend-config="bucket=$STATE_BUCKET" '
        '-backend-config="key=batch.tfstate" -backend-config="region=us-east-1"\n'
    )
    env = _env(run_analysis(tmp_path / "repo", git=False, auto_env=True).model, BATCH, "dev")
    (entry,) = env["state"]["unresolved"]
    assert entry["key"] == "bucket" and "set at run time by scripts/deploy.sh:1" in entry["reason"]


def test_a_shared_partial_configuration_is_applied_to_every_environment(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "repo")
    (tmp_path / "repo/stacks/report/backend.hcl").write_text(
        'bucket = "example-shared"\nkey = "report.tfstate"\nregion = "us-east-1"\n'
    )
    m = run_analysis(tmp_path / "repo", git=False, auto_env=True).model
    for name in ("dev", "nonprod"):
        env = _env(m, REPORT, name)
        assert _settings(env)["bucket"] == "example-shared" and env["state"]["unresolved"] == []


def test_terraforms_default_resolves_and_an_optional_setting_is_left_out(tmp_path):
    (tmp_path / "main.tf").write_text('terraform {\n  backend "local" {}\n}\n')
    (tmp_path / "dev.tfvars").write_text("# dev\n")
    (tmp_path / "prod.tfvars").write_text("# prod\n")
    env = _env(run_analysis(tmp_path, git=False, auto_env=True).model, ".", "dev")
    assert _settings(env) == {"path": "terraform.tfstate"}
    assert env["state"]["unresolved"] == []
    # s3: dynamodb_table is optional: not a missing value
    assert "dynamodb_table" not in {
        s["key"]
        for s in _env(run_analysis(FIXTURE, git=False).model, API, "dev")["state"]["settings"]
    }


def test_no_file_at_all_keeps_the_pair_and_lists_why_with_file_and_line(tmp_path):
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True))
    envs = json.loads(api.handle("GET", "/api/environments").body)
    panel = [b for b in envs["unresolved_backends"] if b["env"] == f"{REPORT}:dev"]
    assert {b["key"] for b in panel} == {"bucket", "key", "region"}
    for entry in panel:
        assert "no backend-dev.hcl under an envs/ directory" in entry["reason"]
        assert entry["location"]["file"] == f"{REPORT}/main.tf"
        assert entry["location"]["start_line"] == 2


def test_confirmed_only_drops_a_pair_and_keeps_the_other_stacks(tmp_path):
    model = {
        "structure": {
            "environments": [
                {"name": "dev", "root": "a", "status": "confirmed"},
                {"name": "dev", "root": "b", "status": "hint-only"},
            ]
        },
        "stacks": [
            {"id": "a", "environments": ["a:dev"], "selected": "a:dev"},
            {"id": "b", "environments": ["b:dev"], "selected": "b:dev"},
        ],
        "environment_analysis": {
            "selected": ["a:dev", "b:dev"],
            "environments": [{"id": "a:dev"}, {"id": "b:dev"}],
            "columns": [{"id": "dev", "environments": ["a:dev", "b:dev"]}],
        },
    }
    confirmed_only(model)
    assert [e["root"] for e in model["structure"]["environments"]] == ["a"]
    assert model["stacks"][1]["environments"] == [] and model["stacks"][1]["selected"] is None
    analysis = model["environment_analysis"]
    assert analysis["columns"][0]["environments"] == ["a:dev"]
    assert [r["id"] for r in analysis["environments"]] == ["a:dev"]


# -- item 3: the resolution pass ------------------------------------------------------------


def test_the_pass_resolves_more_than_the_first_and_counts_both(model):
    resolution = model["resolution"]
    assert resolution["after"] == len(model["unknowns"])
    assert resolution["before"] > resolution["after"]


def test_a_stacks_values_outside_it_are_not_an_environment_of_every_stack(model):
    assert _env(model, ".", "batch") is None
    assert "." not in {s["id"] for s in model["stacks"]}
    for stack in model["stacks"]:  # each stack keeps its own, its tfvars applied
        assert stack["selected"] == f"{stack['id']}:dev"


def test_a_tfvars_file_of_no_environment_fills_a_variable_for_this_stack(model):
    env = _env(model, BATCH, "dev")
    assert env["found_var_files"] == ["envs/batch/terraform.tfvars"]
    # first, so the environment's own files win
    assert env["var_files"] == ["envs/batch/terraform.tfvars", f"{BATCH}/envs/dev.tfvars"]


def test_another_environments_tfvars_never_fill_this_one(model):
    (u,) = [
        u
        for r in model["environment_analysis"]["environments"]
        if r["id"] == f"{BATCH}:dev"
        for u in r["unknowns"]
        if "var.ticket" in u["reason"]
    ]
    assert "set only in stacks/batch/envs/nonprod.tfvars" in u["searched"]
    assert u["location"]["file"] == f"{BATCH}/main.tf"
    assert u["defined_at"]["file"] == f"{BATCH}/main.tf"
    assert "EXAMPLE-1" not in json.dumps(u)
    row = next(
        r for r in model["environment_analysis"]["environments"] if r["id"] == f"{BATCH}:nonprod"
    )
    assert not [u for u in row["unknowns"] if "var.ticket" in u["reason"]]


def test_a_data_source_is_read_from_the_provider(model):
    nodes = {n["id"].split("::")[-1]: n for n in model["nodes"]}
    assert nodes["aws_sqs_queue.jobs"]["attributes"]["name"] == "worker-dev-jobs-us-east-1"


def test_a_source_naming_this_repository_is_followed(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/config").write_text(
        '[remote "origin"]\n\turl = git@example.invalid:org/repo-a.git\n'
    )
    assert (
        same_repo_subdir(
            "git::ssh://git@example.invalid/org/repo-a.git//modules/q?ref=v1", tmp_path
        )
        == "modules/q"
    )
    assert (
        same_repo_subdir("git::ssh://git@example.invalid/org/other.git//modules/q", tmp_path)
        is None
    )
    assert same_repo_subdir("./modules/q", tmp_path) is None
    (tmp_path / "modules/q").mkdir(parents=True)
    (tmp_path / "modules/q/main.tf").write_text(
        'variable "name" {\n  type = string\n}\nresource "aws_sqs_queue" "q" {\n  name = var.name\n}\n'
    )
    (tmp_path / "main.tf").write_text(
        'module "q" {\n  source = "git::ssh://git@example.invalid/org/repo-a.git//modules/q?ref=v1"\n'
        '  name   = "example-q"\n}\n'
    )
    m = run_analysis(tmp_path, git=False, auto_env=True).model
    node = next(n for n in m["nodes"] if n["type"] == "aws_sqs_queue")
    assert node["attributes"]["name"] == "example-q"
    assert m["roots"] == ["."]  # the module is not a stack of its own
    assert m["resolution"] == {"before": 1, "after": 0}


def test_the_progress_log_and_phase_say_before_and_after(tmp_path):
    jobs = JobManager()
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True), jobs)
    final = jobs.wait(json.loads(api.handle("POST", "/api/discover").body)["job"]["id"], 120)
    resolution = api.store.model()["resolution"]
    line = f"unknowns: {resolution['before']} before resolution, {resolution['after']} after"
    assert line in final["log"]
    phase = next(p for p in final["phases"] if p["name"] == "validate")
    assert f"unknowns {resolution['before']} → {resolution['after']}" in phase["detail"]
    assert any("resolution pass" in text for text in final["log"])


# -- item 1: the AWS phase ------------------------------------------------------------------


class _AwsError(Exception):
    def __init__(self, code):
        super().__init__(f"{code}: request signed with {KEY_ID} / {SECRET}")
        self.response = {"Error": {"Code": code}}


class _Client:
    def __init__(self, session, service):
        self.session, self.service = session, service

    def get_caller_identity(self):
        if self.session.identity_error:
            raise _AwsError(self.session.identity_error)
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/example"}

    def get_queue_url(self, QueueName):
        self.session.calls.append(QueueName)
        if self.session.denied:
            raise _AwsError("AccessDenied")
        return {"QueueUrl": "https://example.invalid/q"}

    def get_topic_attributes(self, TopicArn):
        self.session.calls.append(TopicArn)
        raise _AwsError("NotFound")


class _Session:
    def __init__(self, identity_error="", denied=False, region_name="us-east-1"):
        self.identity_error, self.denied, self.region_name = identity_error, denied, region_name
        self.calls = []
        self.kwargs = {}

    def client(self, service):
        return _Client(self, service)


@pytest.fixture
def fake_boto3(monkeypatch):
    session = _Session()

    def build(**kwargs):
        session.kwargs = kwargs
        return session

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=build))
    return session


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("NoCredentialsError", MISSING_CREDENTIALS),
        ("ProfileNotFound", MISSING_CREDENTIALS),
        ("InvalidClientTokenId", INVALID_CREDENTIALS),
        ("ExpiredToken", INVALID_CREDENTIALS),
        ("AccessDenied", MISSING_PERMISSION),
        ("NoRegionError", MISSING_REGION),
    ],
)
def test_why_it_cannot_run_is_a_category_never_a_value(model, caplog, code, reason):
    target = AwsTarget("example", "us-east-1", lambda: _Session(identity_error=code))
    with caplog.at_level("DEBUG"):
        out = run_checks(model, target, True, NullProgress())
    assert out["status"] == "skipped" and out["reason"] == reason
    assert reason in out["message"] and code in out["message"]
    assert out["checks"] == []
    for text in (json.dumps(out), caplog.text):
        assert SECRET not in text and KEY_ID not in text


def test_a_missing_region_is_said_before_any_call(model):
    session = _Session(region_name=None)
    out = run_checks(model, AwsTarget("example", "", lambda: session), True, NullProgress())
    assert out["status"] == "skipped" and out["reason"] == MISSING_REGION
    assert session.calls == []


def test_a_denied_check_is_counted_with_the_category(model, caplog):
    session = _Session(denied=True)
    target = AwsTarget("example", "us-east-1", lambda: session)
    with caplog.at_level("DEBUG"):
        out = run_checks(model, target, True, NullProgress())
    assert out["status"] == "done" and out["counts"]["denied"] > 0
    assert MISSING_PERMISSION in out["message"]
    assert SECRET not in json.dumps(out) and SECRET not in caplog.text


def test_incomplete_or_expired_accounts_are_reported_by_category(model):
    from tflens.accounts import Account

    no_key = target_from_account(Account(display_name="example", auth_mode="keys"))
    out = run_checks(model, no_key, True, NullProgress())
    assert out["status"] == "skipped" and out["reason"] == MISSING_CREDENTIALS


def test_the_phase_is_live_while_it_runs(tmp_path):
    seen = {}
    jobs = JobManager()
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True), jobs)

    class Live(_Session):
        def client(self, service):
            # the progress screen's view while phase 3 runs
            seen["status"] = jobs.current(api._job_key())
            return super().client(service)

    api.store.aws = AwsTarget("example", "us-east-1", Live)
    resp = api.handle("POST", "/api/discover?aws=1")
    final = jobs.wait(json.loads(resp.body)["job"]["id"], 120)
    assert final["state"] == "done", final
    status = seen["status"]
    assert status["phase"] == "aws"
    phase = next(p for p in status["phases"] if p["name"] == "aws")
    assert phase["status"] == "running"
    assert any("connecting to AWS (example, read-only)" in text for text in status["log"])
    done = next(p for p in final["phases"] if p["name"] == "aws")
    assert done["status"] == "done" and "exist" in done["detail"]
    assert any("aws_sqs_queue" in text and ": exists" in text for text in final["log"])


def test_it_runs_with_a_saved_account_from_every_new_run_form(home, fake_boto3):
    app = create_app(home, testing=True)
    client = app.test_client()
    client.get("/repos")

    def post(url, **data):
        with client.session_transaction() as s:
            token = s["csrf"]
        return client.post(url, data={**data, "csrf_token": token})

    post(
        "/accounts/new",
        display_name="example",
        region="us-east-1",
        auth_mode="keys",
        access_key_id=KEY_ID,
        secret_access_key=SECRET,
    )
    (account,) = app.extensions["tflens"]["accounts"].list()
    resp = post("/repos", source="local", path=str(FIXTURE))
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    jobs = app.extensions["tflens"]["jobs"]

    for url in ("/runs", "/repos", f"/repos/{repo_id}"):
        page = client.get(url).data.decode()
        assert 'name="aws"' in page and 'name="account_id"' in page, url
        assert SECRET not in page and KEY_ID not in page, url

    # the toggle with no account chosen: the saved one, not "no account configured"
    for fields in ({}, {"account_id": str(account.id)}):
        fake_boto3.calls.clear()
        started = post("/runs/new", repo_id=str(repo_id), background="1", aws="1", **fields)
        final = jobs.wait(started.get_json()["job"]["id"], 120)
        assert final["state"] == "done", final
        phase = next(p for p in final["phases"] if p["name"] == "aws")
        assert phase["status"] == "done", phase
        assert fake_boto3.calls, fields
        assert fake_boto3.kwargs["aws_secret_access_key"] == SECRET  # its own credentials
        text = json.dumps(final)
        assert SECRET not in text and KEY_ID not in text

    # the run's summary: the row of the Runs page and the analyzer's header
    run_url = final["result"]["url"]
    summary = client.get(run_url + "api/summary").get_json()
    run = summary["run"]
    assert run["aws_status"] == "done" and run["aws_validated"] > 0
    assert run["unknowns_before"] > run["unknowns_after"]
    page = client.get("/runs").data.decode()
    assert f"{run['aws_validated']} AWS-validated" in page
    assert f"{run['unknowns_before']} → {run['unknowns_after']} unknowns" in page
    with open(summary["model_path"], encoding="utf-8") as f:
        written = f.read()
    assert SECRET not in written and KEY_ID not in written


def test_the_toggle_off_runs_no_check_and_says_so(home, fake_boto3):
    d = discover(FIXTURE, Options(git=False, auto_env=True))
    assert d.model["pipeline"]["aws"]["status"] == "off"
    assert fake_boto3.calls == []
    assert d.model["summary"]["aws"]["status"] == "off"


def test_a_skipped_phase_shows_its_reason_in_the_summary(tmp_path):
    api = Api(ModelStore(FIXTURE, tmp_path / "model.json", auto_env=True))
    api.store.aws = AwsTarget("example", "us-east-1", lambda: _Session("ExpiredToken"))
    api.jobs.wait(json.loads(api.handle("POST", "/api/discover?aws=1").body)["job"]["id"], 120)
    run = json.loads(api.handle("GET", "/api/summary").body)["run"]
    assert run["aws_status"] == "skipped" and run["aws_reason"] == INVALID_CREDENTIALS
    assert INVALID_CREDENTIALS in run["aws_message"] and run["aws_validated"] == 0


def test_a_requested_run_replaces_an_automatic_one():
    import threading

    jobs = JobManager()
    started, ran = threading.Event(), []

    def slow(ctx):
        started.set()
        while True:
            ctx.check()
            threading.Event().wait(0.01)

    auto = jobs.start("repo:1", "auto", slow, auto=True)
    assert started.wait(5)
    asked = jobs.start("repo:1", "asked", lambda ctx: ran.append(1))
    assert jobs.wait(asked["id"], 10)["state"] == "done"
    assert jobs.status(auto["id"])["state"] == "cancelled" and ran == [1]
    # somebody's own run is never replaced
    gate = threading.Event()
    mine = jobs.start("repo:2", "mine", lambda ctx: gate.wait(5) and None)
    with pytest.raises(JobBusy):
        jobs.start("repo:2", "again", lambda ctx: None)
    gate.set()
    jobs.wait(mine["id"], 10)
