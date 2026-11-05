"""Phased discovery: crawl → hypothesis → validate → (AWS) → render, the Discover job
(progress, Hide / Cancel), docs and CI as hints only, and the grouped environment list."""

from __future__ import annotations

import json
import re
import shutil
import threading

import pytest
from conftest import FIXTURES

from tflens import tfrepo
from tflens.awscheck import NO_BOTO, AwsTarget, run_checks, target_from_profile
from tflens.db import closing
from tflens.jobs import JobBusy, JobManager
from tflens.pipeline import (
    CONFIRMED,
    CONTRADICTED,
    HINT_ONLY,
    Cancelled,
    NullProgress,
    Options,
    discover,
    tree_lines,
)
from tflens.web import create_app
from tflens.webapi import Api, ModelStore

ENV_NAMES = ("dev", "intg", "uat", "prod")
_ENV_MENTION = re.compile(r"\b(dev|intg|uat|prod|qa|staging|sandbox|perf|demo|dr|test)\b")


@pytest.fixture
def noise(tmp_path):
    """The docs_noise fixture, with tool directories that must never be walked."""
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "docs_noise", repo)
    for d in (".terraform/modules", ".venv/lib", "dist", ".git"):
        (repo / d).mkdir(parents=True, exist_ok=True)
        (repo / d / "prod.tfvars").write_text('env = "staging"\n')
    return repo


@pytest.fixture
def model(noise):
    return discover(noise, Options(git=False)).model


# -- item 7: README mentions environments ~30 times, still exactly 4 -----------------------


def test_readme_noise_still_yields_exactly_four_environments(noise, model):
    readme = (noise / "README.md").read_text()
    assert len(_ENV_MENTION.findall(readme)) >= 30
    envs = model["structure"]["environments"]
    assert len(envs) == 4
    assert sorted(e["name"] for e in envs) == sorted(ENV_NAMES)
    assert all(e["status"] == CONFIRMED and e["evidence_files"] for e in envs)
    # one inventory column per confirmed environment, nothing from the docs
    columns = [c["id"] for c in model["environment_analysis"]["columns"]]
    assert columns == list(ENV_NAMES)
    stack_envs = sorted(e for s in model["stacks"] for e in s["environments"])
    assert stack_envs == sorted(ENV_NAMES)


def test_docs_and_ci_never_create_an_environment(noise):
    # CI names envs/qa.tfvars and a matrix with staging: neither has a real file
    from tflens.detect import detect_environments
    from tflens.engine import Engine
    from tflens.loader import load_repo

    repo = load_repo(noise)
    trace: list[dict] = []
    envs = detect_environments(repo, Engine(repo), trace)
    assert {e["name"] for e in envs} == set(ENV_NAMES)
    assert all("ci" not in e["kinds"] for e in envs)
    # the CI lines are attached to the environments real files established
    dev = next(e for e in envs if e["name"] == "dev")
    assert any(u["flag"] == "-var-file" for u in dev["var_file_cli"])


# -- item 2: crawl lists every file; hints carry their source ------------------------------


def test_crawl_lists_every_file_and_skips_tool_directories(noise, model):
    crawl = model["pipeline"]["crawl"]
    tree = "\n".join(crawl["tree"])
    for f in ("README.md", "HANDOFF.md", "main.tf", "dev.tfvars", "deploy.yml", "backend-dev.hcl"):
        assert f in tree
    assert crawl["files"] == len(model["discovery"]["all_files"])
    skipped = {s["path"] for s in crawl["skipped"]}
    assert {".terraform", ".venv", "dist", ".git"} <= skipped
    assert not any(
        f.startswith((".terraform/", ".venv/", "dist/", ".git/"))
        for f in model["discovery"]["all_files"]
    )
    assert "[skipped:" in tree


def test_tree_lines_is_tree_like():
    lines = tree_lines(["a/b.tf", "a/c/d.tfvars", "e.md"], [{"path": "dist", "reason": "tool"}])
    assert lines[0] == "."
    assert "├── a/" in lines and any(ln.endswith("└── d.tfvars") for ln in lines)
    assert any("dist/  [skipped: tool]" in ln for ln in lines)


def test_every_hypothesis_is_a_hint_with_its_source(model):
    hints = model["pipeline"]["hints"]
    assert hints
    for h in hints:
        assert h["status"] == "hint"
        assert h["sources"] and h["mentions"] >= len(h["sources"])
        for src in h["sources"]:
            assert src["via"] in ("docs", "ci", "git") and src["file"]
            assert not src["file"].endswith((".tf", ".tfvars", ".hcl"))
    names = {h["name"] for h in hints if h["kind"] == "env"}
    assert {"dev", "prod", "qa", "staging"} <= names
    files = {s["file"] for h in hints for s in h["sources"]}
    assert {"README.md", "HANDOFF.md", ".github/workflows/deploy.yml"} <= files


# -- item 3: validate against real files; confirmed / hint-only / contradicted --------------


def test_validation_statuses_and_mismatches(model):
    v = model["pipeline"]["validation"]
    by = {e["name"]: e for e in v["envs"]}
    for name in ENV_NAMES:
        assert by[name]["status"] == CONFIRMED and by[name]["evidence_count"] >= 1
        assert by[name]["hint_mentions"] > 1  # many mentions, one item
    assert by["dev"]["evidence"] == ["backend-dev.hcl", "envs/dev.tfvars"]
    # CI / docs name files that are not in the repository
    assert by["qa"]["status"] == CONTRADICTED and "envs/qa.tfvars" in by["qa"]["reason"]
    assert by["staging"]["status"] == CONTRADICTED
    assert by["sandbox"]["status"] == HINT_ONLY and by["sandbox"]["evidence_count"] == 0
    mismatched = {m["name"] for m in v["mismatches"] if m["kind"] == "env"}
    assert {"qa", "staging", "sandbox"} <= mismatched
    assert not mismatched & set(ENV_NAMES)
    assert v["counts"]["envs"][CONFIRMED] == 4
    assert {s["name"]: s["status"] for s in v["stacks"]}["."] == CONFIRMED


def test_stack_and_link_hints_are_validated(tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "docs_noise", repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "stacks.md").write_text(
        "The `network/` stack is applied before `app/`, which reads its state.\n"
        "Deploy the `legacy/` stack first, then the `gone/` stack.\n"
    )
    # real directories, no root module in them; gone/ is not in the repository at all
    for name in ("network", "app", "legacy"):
        (repo / name).mkdir()
        (repo / name / "notes.txt").write_text("example\n")
    v = discover(repo, Options(git=False)).model["pipeline"]["validation"]
    stacks = {s["name"]: s for s in v["stacks"]}
    assert stacks["legacy"]["status"] == CONTRADICTED
    assert "holds no root module" in stacks["legacy"]["reason"]
    assert "gone" not in stacks  # Phase 1 only proposes directories the crawl found
    links = {lk["name"]: lk for lk in v["links"]}
    assert links["network -> app"]["status"] == HINT_ONLY


# -- item 6: one validation item per environment, with an evidence count ---------------------


def test_validation_has_one_item_per_environment(model):
    items = model["pipeline"]["validation"]["envs"]
    names = [e["name"] for e in items]
    assert len(names) == len(set(names))  # one per environment, not per mention
    dev = next(e for e in items if e["name"] == "dev")
    assert dev["status"] == CONFIRMED and dev["evidence_count"] == 2
    assert dev["hint_mentions"] > 5


def test_environments_view_lists_the_confirmed_ones_only(noise):
    api = Api(ModelStore(noise))
    envs = json.loads(api.handle("GET", "/api/environments").body)
    assert "env_debug" not in envs
    assert sorted(r["id"] for r in envs["environments"]) == sorted(ENV_NAMES)


# -- item 5: render from confirmed data only -----------------------------------------------


def test_render_drops_an_environment_without_real_files():
    from tflens.pipeline import confirmed_only

    model = {
        "structure": {
            "environments": [
                {"name": "dev", "root": ".", "status": CONFIRMED},
                {"name": "ghost", "root": ".", "status": HINT_ONLY},
            ]
        },
        "stacks": [{"id": ".", "environments": ["dev", "ghost"]}],
        "environment_analysis": {"columns": [{"id": "dev"}, {"id": "ghost"}]},
    }
    confirmed_only(model)
    assert [e["name"] for e in model["structure"]["environments"]] == ["dev"]
    assert model["stacks"][0]["environments"] == ["dev"]
    assert [c["id"] for c in model["environment_analysis"]["columns"]] == ["dev"]


# -- item 4: optional AWS phase ------------------------------------------------------------


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeClient:
    def __init__(self, calls):
        self.calls = calls

    def get_caller_identity(self):
        self.calls.append("sts")
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/example"}

    def head_bucket(self, Bucket):
        self.calls.append(("s3", Bucket))
        raise _Err("404")

    def get_queue_url(self, QueueName):
        self.calls.append(("sqs", QueueName))
        return {"QueueUrl": "https://example.invalid/q"}


class _FakeSession:
    def __init__(self):
        self.calls = []

    def client(self, service):
        return _FakeClient(self.calls)


def test_aws_phase_is_off_by_default(model):
    aws = model["pipeline"]["aws"]
    assert aws["status"] == "off" and aws["checks"] == []
    phases = {p["name"]: p["status"] for p in model["pipeline"]["phases"]}
    assert phases == {"crawl": "done", "validate": "done", "aws": "off", "render": "done"}


def test_aws_phase_degrades_gracefully(model):
    prog = NullProgress()
    none = run_checks(model, None, True, prog)
    assert none["status"] == "skipped" and "no AWS account" in none["message"]
    broken = run_checks(model, AwsTarget("example", "us-east-1", None, NO_BOTO), True, prog)
    assert broken["status"] == "skipped" and "boto3" in broken["message"]

    def no_credentials():
        raise _Err("NoCredentialsError")

    failing = run_checks(model, AwsTarget("example", "us-east-1", no_credentials), True, prog)
    assert failing["status"] == "skipped" and "cannot connect" in failing["message"]
    assert failing["checks"] == []  # never a made-up result


def test_aws_phase_read_only_checks_with_its_own_session(noise):
    session = _FakeSession()
    target = AwsTarget("example", "us-east-1", lambda: session)
    m = discover(noise, Options(git=False, env="dev", aws=target, aws_requested=True)).model
    aws = m["pipeline"]["aws"]
    assert aws["status"] == "done"
    status = {c["type"]: c["status"] for c in aws["checks"]}
    assert status == {"aws_s3_bucket": "missing", "aws_sqs_queue": "exists"}
    assert ("sqs", "dev-example-jobs") in session.calls
    assert "123456789012" not in json.dumps(m)  # the account id is not kept


def test_profile_target_without_boto3(monkeypatch):
    import tflens.awscheck as awscheck

    monkeypatch.setattr(awscheck, "boto3_available", lambda: False)
    t = target_from_profile("example")
    assert t.problem == NO_BOTO and t.session is None


# -- item 1: one Discover entry point, progress, Hide / Cancel -----------------------------


class _CancelAt:
    """A progress object that asks to cancel when a step containing ``text`` starts."""

    def __init__(self, text):
        self.text, self.steps, self.phases = text, [], []

    def phase(self, name, label):
        self.phases.append(name)

    def phase_done(self, name, status, detail=""):
        pass

    def step(self, text):
        self.steps.append(text)
        if self.text in text:
            raise Cancelled()

    def log(self, text):
        pass


def test_cancel_stops_before_anything_is_written(noise):
    written = []
    with pytest.raises(Cancelled):
        discover(noise, Options(git=False), _CancelAt("environment column"), written.append)
    assert written == []
    # the last cancellation point is just before the write
    with pytest.raises(Cancelled):
        discover(noise, Options(git=False), _CancelAt("writing the results"), written.append)
    assert written == []


def test_job_progress_and_clean_cancel(noise, tmp_path):
    out = tmp_path / "out" / "model.json"
    store = ModelStore(noise, out)
    jobs = JobManager()
    api = Api(store, jobs)
    gate, release = threading.Event(), threading.Event()

    def work(ctx):
        ctx.phase("crawl", "Crawl")
        gate.set()
        release.wait(5)
        store.discover(ctx)  # stops at its first step: cancel was requested
        return {}

    job = jobs.start(api._job_key(), "Discover repo-a", work)
    assert gate.wait(5)
    status = json.loads(api.handle("GET", "/api/job").body)["job"]
    assert status["running"] and status["phase"] == "crawl"
    assert [p["name"] for p in status["phases"]] == ["crawl", "validate", "aws", "render"]
    with pytest.raises(JobBusy):
        jobs.start(api._job_key(), "again", work)
    resp = api.handle("POST", f"/api/job/cancel?id={job['id']}")
    assert json.loads(resp.body)["ok"] is True
    release.set()
    final = jobs.wait(job["id"], 10)
    assert final["state"] == "cancelled" and not final["running"]
    assert not jobs.get(job["id"]).thread.is_alive()  # no orphan worker
    assert not out.exists()  # no half-written model
    assert not out.parent.exists() or not list(out.parent.glob("*.tmp"))
    assert "Cancelled" in final["messages"][0]["text"]


def test_standalone_discover_api(noise, tmp_path):
    out = tmp_path / "model.json"
    api = Api(ModelStore(noise, out), JobManager())
    resp = api.handle("POST", "/api/discover")
    started = json.loads(resp.body)
    assert started["ok"] is True
    final = api.jobs.wait(started["job"]["id"], 60)
    assert final["state"] == "done", final
    assert all(p["status"] in ("done", "off") for p in final["phases"])
    assert out.is_file() and json.loads(out.read_text())["pipeline"]["validation"]
    assert api.handle("GET", "/api/discovery").status == 404
    assert json.loads(api.handle("GET", "/api/overview").body)["aws"] is None  # it was off
    job = json.loads(api.handle("GET", "/api/job").body)
    assert job["aws"]["available"] is False and "no AWS account" in job["aws"]["reason"]
    # a recorded run is read-only
    ro = Api(ModelStore(noise, out, readonly=True))
    assert ro.handle("POST", "/api/discover").status == 405


def test_index_has_one_discover_entry_point_with_progress(noise):
    index = Api(ModelStore(noise)).handle("GET", "/").body.decode()
    assert 'id="discover"' in index and "data-discover" in index
    for part in (
        'id="discover-hide"',
        'id="discover-cancel"',
        'id="discover-log"',
        'id="discover-phases"',
    ):
        assert part in index
    assert 'data-view="discovery"' not in index
    assert 'id="regenerate"' not in index


# -- managed app ---------------------------------------------------------------------------


@pytest.fixture
def client(home):
    app = create_app(home, testing=True)
    c = app.test_client()
    c.get("/repos")
    c.application = app
    return c


def _post(client, url, data=None):
    with client.session_transaction() as s:
        token = s["csrf"]
    return client.post(url, data={**(data or {}), "csrf_token": token})


def test_managed_discover_job(client, noise):
    resp = _post(client, "/repos", {"source": "local", "path": str(noise)})
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    page = client.get(f"/repos/{repo_id}").data.decode()
    assert (
        "data-discover" in page and 'id="discover-cancel"' in page and 'id="discover-hide"' in page
    )
    assert "/analyze" not in page  # no separate Analyze entry points
    assert 'name="aws"' in page and "disabled" in page  # no account: AWS phase off
    resp = _post(client, f"/repos/{repo_id}/discover", {"background": "1"})
    started = resp.get_json()
    assert started["ok"] is True
    job_id = started["job"]["id"]
    jobs = client.application.extensions["tflens"]["jobs"]
    final = jobs.wait(job_id, 60)
    assert final["state"] == "done", final
    status = client.get(f"/repos/{repo_id}/jobs/{job_id}").get_json()["job"]
    assert status["result"]["url"] == f"/runs/{final['result']['run_id']}/"
    assert client.get(f"/repos/{repo_id}/jobs/current").get_json()["job"]["id"] == job_id
    assert client.get(f"/repos/{repo_id + 1}/jobs/{job_id}").status_code == 404
    run_url = status["result"]["url"]
    envs = client.get(run_url + "api/environments").get_json()
    assert len(envs["environments"]) == 4 and envs["counts"]["environments"] == 4
    db = client.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        names = sorted(e["env"] for e in tfrepo.list_envs(conn, repo_id))
    assert names == sorted(ENV_NAMES)
    # nothing half-written next to the run
    out = client.application.extensions["tflens"]["paths"].out_dir
    assert not list(out.rglob("*.partial"))


def test_managed_cancel_records_nothing(client, noise, monkeypatch):
    import tflens.pipeline as pipeline

    resp = _post(client, "/repos", {"source": "local", "path": str(noise)})
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    entered, release = threading.Event(), threading.Event()
    real = pipeline.validate

    def slow_validate(model, hints):
        entered.set()
        release.wait(10)
        return real(model, hints)

    monkeypatch.setattr(pipeline, "validate", slow_validate)
    job = _post(client, f"/repos/{repo_id}/discover", {"background": "1"}).get_json()["job"]
    assert entered.wait(30)
    cancel = _post(client, f"/repos/{repo_id}/jobs/{job['id']}/cancel").get_json()
    assert cancel["ok"] is True and cancel["job"]["cancel_requested"]
    release.set()
    jobs = client.application.extensions["tflens"]["jobs"]
    final = jobs.wait(job["id"], 30)
    assert final["state"] == "cancelled"
    assert {p["name"]: p["status"] for p in final["phases"]}["render"] == "not run"
    db = client.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    out = client.application.extensions["tflens"]["paths"].out_dir
    assert not out.exists() or not any(out.rglob("model.json"))
