"""Zero-config onboarding: discovery, .tf.json / Terragrunt, structure detection,
environments, the Overview, and failed runs with their reason."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens import tfrepo
from tflens.cli import main
from tflens.db import closing
from tflens.jsonloc import tf_json, tfvars_json
from tflens.output import listing_text, run_analysis
from tflens.overview import build_overview, render_overview_markdown
from tflens.web import create_app
from tflens.webapi import Api, ModelStore, repo_tree


def _root(model, d):
    return next(r for r in model["structure"]["roots"] if r["dir"] == d)


def _settings(root):
    return {s["key"]: s["value"] for s in root["backend"]["settings"]}


def _envs(model):
    return [(e["name"], e["root"], e["kinds"]) for e in model["structure"]["environments"]]


def _walk(tree):
    yield tree
    for c in tree.get("children", []):
        yield from _walk(c)


# -- empty repository: the run fails, with the reason and a listing -----------------------------


def test_repo_without_terraform_fails_with_reason_and_listing():
    a = run_analysis(FIXTURES / "no_terraform")
    assert a.failed
    assert a.failure.startswith("nothing to analyze: no *.tf, *.tf.json or terragrunt.hcl")
    assert "5 file(s)" in a.failure
    inv = a.model["discovery"]["inventory"]
    assert inv["total_files"] == 5
    assert inv["by_extension"] == {".md": 2, ".py": 2, ".txt": 1}
    by_path = {e["path"]: e for e in inv["entries"]}
    assert set(by_path) == {".", "docs", "docs/img", "src"}  # top two levels only
    assert by_path["docs"]["by_extension"] == {".md": 1, ".txt": 1}  # sub-directories included
    text = listing_text(inv)
    assert "docs/img/" in text and ".md 2, .py 2, .txt 1" in text
    # the repository tree still lists every file, none analyzed
    tree = repo_tree(a.model)
    files = [n for n in _walk(tree) if n["type"] == "file"]
    assert len(files) == 5 and not any(f["analyzed"] for f in files)


def test_cli_fails_on_a_repo_without_terraform(tmp_path, capsys):
    rc = main(["--dir", str(FIXTURES / "no_terraform"), "--overview", str(tmp_path / "o.md")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tflens: error: nothing to analyze" in err and "docs/img/" in err
    overview = (tmp_path / "o.md").read_text()
    assert "**Analysis failed:**" in overview and "src/" in overview


def test_failed_run_is_recorded_with_reason_and_opens(tmp_path, db_path):
    repo_dir = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "no_terraform", repo_dir)
    out_dir = tmp_path / "out"
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=tmp_path / "repos", path=str(repo_dir))
        run_id = tfrepo.create_run(conn, tfrepo.get_repo(conn, repo_id), None, out_dir, None)
        run = tfrepo.get_run(conn, run_id)
    assert run["status"] == "failed"
    assert run["reason"].startswith("nothing to analyze")
    assert "docs/img/" in run["error_detail"]  # the listing is kept with the run
    model_path = Path(run["model_path"])
    assert model_path.is_file() and (model_path.parent / "overview.md").is_file()


# -- unreadable directories are listed, never silently skipped -----------------------------------


@pytest.fixture
def unreadable_repo(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read a chmod 000 directory")
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "unreadable", repo)
    (repo / ".git").mkdir()
    locked = repo / "locked"
    locked.chmod(0)
    yield repo
    locked.chmod(0o755)


def test_unreadable_directory_is_reported(unreadable_repo, capsys):
    a = run_analysis(unreadable_repo)
    skipped = {s["path"]: s for s in a.model["discovery"]["skipped"]}
    assert skipped["locked"]["kind"] == "unreadable"
    assert "Permission denied" in skipped["locked"]["reason"]
    assert skipped[".git"]["kind"] == "ignored"
    assert not a.failed  # main.tf is still analyzed
    assert "aws_sqs_queue.jobs" in {n["id"] for n in a.model["nodes"]}
    assert "aws_sns_topic.hidden" not in {n["id"] for n in a.model["nodes"]}
    tree = repo_tree(a.model)
    locked = next(n for n in _walk(tree) if n["path"] == "locked")
    assert locked["skipped_kind"] == "unreadable"
    ov = build_overview(a.model)
    line = next(ln for ln in ov["discovery"] if "locked" in ln["text"])
    assert line["status"] == "failed"
    assert main(["--dir", str(unreadable_repo), "--out", os.devnull]) == 0
    assert "warning: skipped locked/" in capsys.readouterr().err


# -- .tf.json ------------------------------------------------------------------------------------


def test_tf_json_repo_is_analyzed_with_exact_lines():
    m = run_analysis(FIXTURES / "tf_json").model
    nodes = {n["id"]: n for n in m["nodes"]}
    fn = nodes["aws_lambda_function.fn"]
    assert fn["location"] == {"file": "main.tf.json", "start_line": 49, "end_line": 58}
    assert fn["properties"]["function_name"] == "fn-a-dev"  # var.env: terraform.tfvars.json
    queue = nodes["module.queue.aws_sqs_queue.this"]
    assert queue["location"]["file"] == "modules/queue/main.tf.json"
    kinds = {(e["kind"], e["source"], e["target"]) for e in m["edges"]}
    assert ("TRIGGER", "module.queue.aws_sqs_queue.this", "aws_lambda_function.fn") in kinds
    assert ("ACCESS", "aws_iam_role.fn", "module.queue.aws_sqs_queue.this") in kinds
    root = _root(m, ".")
    assert root["backend"]["type"] == "s3"
    assert _settings(root)["key"] == "tf-json/terraform.tfstate"
    assert _settings(root)["dynamodb_table"] == "UNKNOWN"
    aws = root["providers"][0]
    assert (aws["source"], aws["constraints"][0]["version"]) == ("hashicorp/aws", "~> 5.40")
    assert root["required_version"][0]["value"] == ">= 1.6.0"
    call = next(c for c in root["modules"] if c["address"] == "module.queue")
    assert call["kind"] == "local" and call["dir"] == "modules/queue"
    assert call["inputs"] == [
        {
            "name": "name",
            "value": "repo-a-dev-jobs",
            "location": {"file": "main.tf.json", "start_line": 32, "end_line": 32},
        }
    ]


def test_tf_json_syntax_details(tmp_path):
    # label levels may be arrays; "//" comments are dropped
    data, loc = tf_json('{"resource": [{"aws_sqs_queue": [{"q": {"name": "a", "//": "c"}}]}]}')
    assert data["resource"][0] == {"aws_sqs_queue": {"q": {"__is_block__": True, "name": "a"}}}
    assert loc.children[0].labels == ("aws_sqs_queue", "q")
    # tfvars.json strings are literals: ${...} is not evaluated
    values = tfvars_json('{\n  "a": "${var.x}",\n  "b": [1, 2]\n}')
    assert values["a"][1:] == (2, 2) and values["b"] == ([1, 2], 3, 3)
    (tmp_path / "terraform.tfvars.json").write_text('{"name": "${x}"}')
    (tmp_path / "main.tf").write_text(
        'variable "name" {}\nresource "aws_sqs_queue" "q" {\n  name = var.name\n}\n'
    )
    (tmp_path / "bad.tf.json").write_text('{"resource": {')
    m = run_analysis(tmp_path).model
    q = next(n for n in m["nodes"] if n["id"] == "aws_sqs_queue.q")
    assert q["properties"]["name"] == "${x}"
    assert m["parse_errors"][0]["file"] == "bad.tf.json"
    assert "JSON syntax error" in m["parse_errors"][0]["message"]


# -- Terragrunt ----------------------------------------------------------------------------------


def test_terragrunt_only_repo():
    m = run_analysis(FIXTURES / "terragrunt_only").model
    assert m["discovery"]["status"] == "ok"
    assert "terragrunt.hcl" in m["discovery"]["analyzable"]
    roots = m["structure"]["roots"]
    assert [r["dir"] for r in roots] == [
        "live/dev/app",
        "live/dev/vpc",
        "live/prod/app",
        "live/prod/vpc",
    ]
    assert all(r["kind"] == "terragrunt" and not r["analyzed"] for r in roots)
    vpc = _root(m, "live/dev/vpc")
    assert "terragrunt downloads it at run time" in vpc["status"]
    # remote_state comes from the included root terragrunt.hcl, key per unit
    assert vpc["backend"]["origin"] == "terragrunt remote_state (included from terragrunt.hcl)"
    assert _settings(vpc) == {
        "bucket": "repo-a-tfstate",
        "key": "live/dev/vpc/terraform.tfstate",
        "region": "us-east-1",
        "dynamodb_table": "repo-a-tflock",
    }
    source = vpc["modules"][0]
    assert (source["address"], source["kind"], source["version"]) == (
        "terraform.source",
        "git",
        "v1.2.0",
    )
    inputs = {i["name"]: i for i in source["inputs"]}
    assert inputs["env"]["value"] == "dev"
    assert inputs["project"]["location"]["file"] == "terragrunt.hcl"  # merged from the parent
    assert inputs["cidr"]["location"] == {
        "file": "live/dev/vpc/terragrunt.hcl",
        "start_line": 11,
        "end_line": 11,
    }
    app = _root(m, "live/dev/app")
    assert (app["modules"][0]["kind"], app["modules"][0]["version"]) == ("registry", "7.2.0")
    vpc_id = next(i for i in app["modules"][0]["inputs"] if i["name"] == "vpc_id")
    assert "only known when terragrunt runs" in vpc_id["value"]
    assert app["providers"][0]["note"].startswith("provider configuration generated by terragrunt")
    assert _envs(m) == [
        ("dev", "live/dev", ["terragrunt"]),
        ("prod", "live/prod", ["terragrunt"]),
    ]
    ov = build_overview(m, "repo-a")
    assert any("No resources are defined" in ln["text"] for ln in ov["summary"])


def test_terragrunt_unit_with_local_module(tmp_path):
    (tmp_path / "modules" / "queue").mkdir(parents=True)
    (tmp_path / "modules" / "queue" / "main.tf").write_text(
        'variable "name" {}\nresource "aws_sqs_queue" "q" {\n  name = var.name\n}\n'
    )
    (tmp_path / "root.hcl").write_text('inputs = {\n  prefix = "repo-a"\n}\n')
    unit = tmp_path / "live" / "uat" / "queue"
    unit.mkdir(parents=True)
    (unit / "terragrunt.hcl").write_text(
        'include "root" {\n  path = find_in_parent_folders("root.hcl")\n}\n'
        'terraform {\n  source = "${get_repo_root()}/modules//queue"\n}\n'
        'inputs = {\n  name = "repo-a-uat-jobs"\n}\n'
    )
    m = run_analysis(tmp_path).model
    assert m["roots"] == ["live/uat/queue"]  # the module is not a root of its own
    q = next(n for n in m["nodes"] if n["type"] == "aws_sqs_queue")
    assert q["properties"]["name"] == "repo-a-uat-jobs"  # terragrunt inputs -> variables
    root = _root(m, "live/uat/queue")
    assert root["analyzed"] and root["module_dir"] == "modules/queue"
    assert root["modules"][0]["source"] == "<repo>/modules//queue"
    assert "root.hcl" in m["files"]  # the included file is read and linkable
    var = next(v for v in m["variables"] if v["name"] == "name")
    assert var["source"] == "terragrunt inputs (live/uat/queue/terragrunt.hcl)"


# -- multi-root repository with an envs/ layout ------------------------------------------------


def test_multi_root_envs_structure():
    m = run_analysis(FIXTURES / "multi_root_envs").model
    assert m["roots"] == ["envs/dev", "envs/prod", "stacks/shared"]
    dev = _root(m, "envs/dev")
    assert [s["signal"] for s in dev["signals"]] == [
        'backend "s3" block',
        "terraform block",
        'provider "aws" block',
        "not called as a module by any other directory",
    ]
    assert _settings(dev) == {
        "bucket": "repo-a-tfstate",
        "key": "envs/dev/terraform.tfstate",
        "region": "us-east-1",
        "dynamodb_table": "repo-a-tflock",
    }
    assert dev["backend"]["settings"][0]["location"] == {
        "file": "envs/dev/main.tf",
        "start_line": 5,
        "end_line": 5,
    }
    aws = dev["providers"][0]
    assert aws["locked"]["version"] == "5.41.0"
    assert [c["version"] for c in aws["constraints"]] == ["~> 5.40", ">= 5.0"]
    assert dev["provider_configs"][0]["region"] == "us-east-1"
    calls = {c["address"]: c for c in dev["modules"]}
    assert (calls["module.label"]["kind"], calls["module.label"]["version"]) == (
        "registry",
        "0.25.0",
    )
    assert (calls["module.alarms"]["kind"], calls["module.alarms"]["version"]) == ("git", "v0.3.1")
    assert calls["module.network"]["dir"] == "modules/network"
    # prod: key / dynamodb_table come from the partial configuration file
    prod = _root(m, "envs/prod")
    key = next(s for s in prod["backend"]["settings"] if s["key"] == "key")
    assert key["value"] == "envs/prod/terraform.tfstate"
    assert key["location"]["file"] == "envs/prod/backend.tfbackend"
    shared = _root(m, "stacks/shared")
    assert shared["backend"]["type"] == "gcs"
    assert shared["provider_configs"][0]["region"] == "UNKNOWN"
    assert _envs(m) == [
        ("dev", "envs/dev", ["dir"]),
        ("prod", "envs/prod", ["dir"]),
        ("prod", "stacks/shared", ["tfvars"]),
        ("uat", "stacks/shared", ["tfvars"]),
    ]
    uat = m["structure"]["environments"][3]
    assert uat["var_files"] == ["stacks/shared/vars/common.tfvars", "stacks/shared/vars/uat.tfvars"]


def test_environment_scope_limits_the_roots():
    a = run_analysis(
        FIXTURES / "multi_root_envs",
        ["stacks/shared/vars/common.tfvars", "stacks/shared/vars/uat.tfvars"],
        scope="stacks/shared",
    )
    assert a.model["roots"] == ["stacks/shared"]
    assert [r["dir"] for r in a.model["structure"]["roots"]] == ["stacks/shared"]
    topic = next(n for n in a.model["nodes"] if n["type"] == "aws_sns_topic")
    assert topic["properties"]["name"] == "repo-a-uat-alerts"
    missing = run_analysis(FIXTURES / "multi_root_envs", scope="nowhere")
    assert missing.failure == "no root module or terragrunt unit under nowhere/"


def test_overview_lines_all_link_to_a_location():
    m = run_analysis(FIXTURES / "multi_root_envs").model
    ov = build_overview(m, "repo-a")
    lines = ov["summary"] + ov["environments"]
    for r in ov["roots"]:
        for s in r["sections"]:
            lines += s["lines"]
    assert lines and all(ln["location"] for ln in lines)
    dev = ov["roots"][0]
    rel = next(s for s in dev["sections"] if s["title"] == "Key relationships")
    kinds = [ln["text"].split(":")[0] for ln in rel["lines"]]
    assert {"ACCESS", "TRIGGER", "NETWORK"} <= set(kinds)
    assert all(ln.get("edge") for ln in rel["lines"])
    md = render_overview_markdown(ov)
    assert md == render_overview_markdown(build_overview(m, "repo-a"))  # deterministic
    assert "- bucket = repo-a-tfstate (`envs/dev/main.tf:5`)" in md


# -- managed app: environments detected, failed runs shown with their reason -------------------


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


def _add(client, path) -> int:
    resp = _post(client, "/repos", {"source": "local", "path": str(path)})
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])


def test_environments_are_detected_and_edits_are_kept(client, tmp_path):
    repo_dir = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "multi_root_envs", repo_dir)
    repo_id = _add(client, repo_dir)
    page = client.get(f"/repos/{repo_id}").data.decode()
    assert "Environments detected: 4 found" in page
    assert "stacks/shared/vars/uat.tfvars" in page and "envs/prod/" in page
    db = client.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        envs = {(e["env"], e["root_rel"]): e for e in tfrepo.list_envs(conn, repo_id)}
    uat = envs[("uat", "stacks/shared")]
    assert uat["kinds"] == "tfvars" and not uat["confirmed"]
    # analyzing a detected environment covers its root only
    resp = _post(client, f"/repos/{repo_id}/analyze", {"env_id": str(uat["id"]), "open": "1"})
    run_url = resp.headers["Location"]
    summary = client.get(run_url + "api/summary").get_json()
    assert summary["roots"] == ["stacks/shared"] and summary["status"] == "ok"
    overview = client.get(run_url + "api/overview").get_json()
    assert overview["roots"][0]["dir"] == "stacks/shared"
    src = client.get(
        run_url + "api/source", query_string={"file": "stacks/shared/main.tf", "start": 3}
    ).get_json()
    assert any(ln["highlight"] and "repo-a-state" in ln["text"] for ln in src["lines"])
    # an edited environment survives the next detection; a removed one is marked
    _post(
        client,
        f"/repos/{repo_id}/envs/{uat['id']}",
        {"env": "uat", "var_files": "stacks/shared/vars/uat.tfvars"},
    )
    (repo_dir / "stacks" / "shared" / "vars" / "prod.tfvars").unlink()
    _post(client, f"/repos/{repo_id}/discover")
    with closing(db) as conn:
        envs = {(e["env"], e["root_rel"]): e for e in tfrepo.list_envs(conn, repo_id)}
    assert envs[("uat", "stacks/shared")]["var_files"] == ["stacks/shared/vars/uat.tfvars"]
    assert envs[("uat", "stacks/shared")]["confirmed"] == 1
    assert envs[("prod", "stacks/shared")]["present"] == 0
    assert "no longer found" in client.get(f"/repos/{repo_id}").data.decode()


def test_failed_runs_are_listed_with_their_reason(client, tmp_path):
    empty = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "no_terraform", empty)
    repo_id = _add(client, empty)
    resp = _post(client, f"/repos/{repo_id}/analyze", {"open": "1"})
    run_url = resp.headers["Location"]
    assert re.fullmatch(r"/runs/\d+/", run_url)  # the failed run opens: the repository is shown
    tree = client.get(run_url + "api/tree").get_json()
    assert {n["path"] for n in _walk(tree) if n["type"] == "file"} >= {"README.md", "src/app.py"}
    assert client.get(run_url + "api/summary").get_json()["status"] == "failed"
    # a precondition failure is recorded too (no model)
    _post(client, f"/repos/{repo_id}/envs", {"env": "dev", "var_files": "envs/missing.tfvars"})
    db = client.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        env_id = conn.execute("SELECT id FROM tf_repo_envs WHERE env='dev'").fetchone()[0]
    _post(client, f"/repos/{repo_id}/analyze", {"env_id": str(env_id)})
    runs = client.get("/runs").data.decode()
    assert runs.count('class="badge err">failed') == 2
    assert "nothing to analyze: no *.tf, *.tf.json or terragrunt.hcl" in runs
    assert "tfvars file envs/missing.tfvars not found in the repository" in runs
    assert "What is in the repository" in runs and "docs/img/" in runs
    with closing(db) as conn:
        failed = conn.execute("SELECT id FROM runs WHERE model_path=''").fetchone()[0]
    resp = client.get(f"/runs/{failed}/")
    assert resp.status_code == 404 and b"envs/missing.tfvars not found" in resp.data


def test_standalone_analyzer_overview(tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "tf_json", repo)
    api = Api(ModelStore(repo))
    index = api.handle("GET", "/").body.decode()
    assert 'data-view="overview"' in index
    import json

    ov = json.loads(api.handle("GET", "/api/overview").body)
    assert ov["status"] == "ok" and ov["roots"][0]["dir"] == "."
    readme = json.loads(api.handle("GET", "/api/file?path=main.tf.json").body)
    assert readme["analyzed"] is True
    bad = api.handle("GET", "/api/source?file=../etc/passwd&start=1")
    assert bad.status == 404
