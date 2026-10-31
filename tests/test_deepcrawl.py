"""Deep crawl: docs / CI / lock files / git log, classification, the extended Overview,
the three diagrams (Mermaid + SVG), per-run snapshots with the Changes diff, handoff.md."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens import tfrepo
from tflens.cli import main
from tflens.db import SCHEMA_VERSION, closing, init_db, schema_version
from tflens.handoff import render_handoff
from tflens.output import listing_text, run_analysis
from tflens.overview import build_overview, purpose_text, render_overview_markdown
from tflens.snapshots import (
    TABLES,
    diff_runs,
    diff_snapshots,
    has_snapshot,
    runs_at_commit,
    snapshot,
)
from tflens.visuals import (
    DIAGRAMS,
    architecture_graph,
    env_matrix,
    export_diagrams,
    mermaid,
    module_tree,
    svg,
    validate_mermaid,
)
from tflens.web import create_app
from tflens.webapi import Api, ModelStore

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"
ANALYZABLE = ["terragrunt_only", "tf_json", "multi_root_envs", "module_only", "app_flows"]


def _root(model, d):
    return next(r for r in model["structure"]["roots"] if r["dir"] == d)


def _all_lines(ov):
    lines = ov["purpose"] + ov["summary"] + ov["environments"] + ov["docs"] + ov["ci"]
    for r in ov["roots"]:
        for s in r["sections"]:
            lines += s["lines"]
    for d in ov["resources"]:
        lines += d["lines"]
    lines += ov["history"]["commits"] + ov["history"]["hot_files"]
    return lines


# -- git helpers (a real repository in tmp_path; fixed authors and dates) ---------------------


def _git(repo: Path, *args: str, author: str = "dev-a", date: str = "2026-01-02T03:04:05+00:00"):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": author,
        "GIT_AUTHOR_EMAIL": f"{author}@example.com",
        "GIT_COMMITTER_NAME": author,
        "GIT_COMMITTER_EMAIL": f"{author}@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """app_flows as a git repository with three commits by two authors."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "app_flows", repo)
    _git(repo, "init", "-q")
    _git(repo, "add", "main.tf")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "Add queue-a and fn-a",
        "-m",
        "First cut.",
        date="2026-01-01T10:00:00+00:00",
    )
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "Add docs and CI",
        author="dev-b",
        date="2026-01-02T10:00:00+00:00",
    )
    main_tf = repo / "main.tf"
    main_tf.write_text(
        main_tf.read_text().replace('name = "queue-a"', 'name = "queue-a"\n  delay_seconds = 5')
    )
    _git(repo, "commit", "-q", "-am", "Delay queue-a", date="2026-01-03T10:00:00+00:00")
    return repo


# -- 1. crawl: docs, CI, lock files; never silent -------------------------------------------------


def test_crawl_multi_root_envs_docs_ci_and_locks():
    m = run_analysis(FIXTURES / "multi_root_envs").model
    crawled = m["crawl"]
    docs = {d["path"]: d for d in crawled["docs"]}
    assert list(docs) == ["README.md", "docs/architecture.md"]
    arch = docs["docs/architecture.md"]
    assert arch["title"] == "repo-a architecture"
    assert arch["excerpt"].startswith("Each environment root (envs/dev, envs/prod)")
    assert arch["excerpt_location"] == {
        "file": "docs/architecture.md",
        "start_line": 3,
        "end_line": 3,
    }
    assert [h["text"] for h in arch["headings"]] == ["repo-a architecture", "Network", "Alerts"]
    # .github is walked (CI), not skipped as a hidden directory
    assert ".github/workflows/terraform.yml" in m["discovery"]["all_files"]
    assert ".github" not in {s["path"] for s in m["discovery"]["skipped"]}
    (gha,) = crawled["ci"]
    assert (gha["system"], gha["name"], gha["path"]) == (
        "GitHub Actions",
        "terraform",
        ".github/workflows/terraform.yml",
    )
    cmds = [(c["tool"], c["command"], c["location"]["start_line"]) for c in gha["commands"]]
    assert cmds == [
        ("terraform", "init", 16),
        ("terraform", "plan", 17),
        ("tflint", "", 18),
        ("terraform", "apply", 26),
    ]
    (lock,) = crawled["locks"]
    assert lock["file"] == "envs/dev/.terraform.lock.hcl"
    assert lock["providers"][0]["version"] == "5.41.0"
    assert set(crawled["readable"]) >= {
        "README.md",
        "docs/architecture.md",
        ".github/workflows/terraform.yml",
    }
    assert crawled["git"]["status"] == "skipped"  # a fixture directory is not a checkout
    assert crawled["git"]["reason"] == "not a git checkout (no .git)"


def test_terragrunt_repo_crawl_and_classification():
    m = run_analysis(FIXTURES / "terragrunt_only").model
    ci = {c["system"]: c for c in m["crawl"]["ci"]}
    assert set(ci) == {"Atlantis", "GitLab CI"}
    projects = [(p["name"], p["dir"], p["workspace"]) for p in ci["Atlantis"]["projects"]]
    assert projects == [
        ("dev-vpc", "live/dev/vpc", "default"),
        ("dev-app", "live/dev/app", "default"),
        ("prod-vpc", "live/prod/vpc", "default"),
    ]
    # YAML keys (workflow: terragrunt) are not commands
    assert [c["text"] for c in ci["Atlantis"]["commands"]] == [
        "terragrunt plan -no-color -out=$PLANFILE",
        "terragrunt apply -no-color $PLANFILE",
    ]
    assert [c["command"] for c in ci["GitLab CI"]["commands"]] == ["run-all", "run-all"]
    roots = m["structure"]["roots"]
    assert {(r["kind"], r["role"]) for r in roots} == {("terragrunt", "deployable")}
    assert {r["backend"]["type"] for r in roots} == {"s3"}
    assert [(e["name"], e["root"]) for e in m["structure"]["environments"]] == [
        ("dev", "live/dev"),
        ("prod", "live/prod"),
    ]
    assert m["structure"]["module_dirs"] == []  # no .tf code: only Terragrunt units
    ov = build_overview(m, "repo-a")
    ci_line = next(ln for ln in ov["purpose"] if ln["text"].startswith("Applied by CI"))
    assert ci_line["text"] == "Applied by CI: GitLab CI (.gitlab-ci.yml); Atlantis (3 projects)"


def test_tf_json_repo_crawl_and_purpose():
    m = run_analysis(FIXTURES / "tf_json").model
    (jenkins,) = m["crawl"]["ci"]
    assert jenkins["system"] == "Jenkins"
    assert [c["text"] for c in jenkins["commands"]] == [
        "terraform init -input=false",
        "terraform plan -var-file=terraform.tfvars.json -out=tfplan",
        "terraform apply tfplan",
    ]
    (readme,) = m["crawl"]["docs"]
    # the badge line is skipped: the excerpt is the first prose paragraph
    assert readme["excerpt"].startswith("repo-a in Terraform JSON syntax")
    assert readme["excerpt_location"] == {"file": "README.md", "start_line": 5, "end_line": 6}
    assert purpose_text(m) == "deploys 1 Lambda function and 1 SQS queue"
    assert {d["dir"]: d["role"] for d in m["structure"]["module_dirs"]} == {
        ".": "root",
        "modules/queue": "child",
    }


# -- 2. classification ------------------------------------------------------------------------


def test_module_only_repo_classification():
    m = run_analysis(FIXTURES / "module_only").model
    assert not [n for n in m["nodes"] if n["kind"] == "resource"]
    dirs = {d["dir"]: d for d in m["structure"]["module_dirs"]}
    assert (dirs["."]["role"], dirs["modules/label"]["role"]) == ("root", "child")
    assert dirs["modules/label"]["called_by"] == [
        {
            "caller": ".",
            "address": "module.label",
            "location": {"file": "main.tf", "start_line": 27, "end_line": 31},
        }
    ]
    assert (dirs["modules/label"]["resources"], dirs["modules/label"]["outputs"]) == (0, 2)
    top = _root(m, ".")
    assert (top["role"], top["role_reason"]) == (
        "deployable",
        "has backend, provider configuration",
    )
    assert top["backend"]["type"] == "s3"
    lib = _root(m, "modules/naming")  # not called by anything: a root, but labelled a library
    assert lib["role"] == "library" and "reusable module" in lib["role_reason"]
    calls = {c["address"]: (c["kind"], c["version"], c["analyzed"]) for c in top["modules"]}
    assert calls == {
        "module.label": ("local", None, True),
        "module.queue": ("registry", "4.2.0", False),
        "module.table": ("git", "v2.0.1", False),
    }
    assert top["providers"][0]["locked"]["version"] == "5.42.0"
    ov = build_overview(m, "repo-a")
    texts = [ln["text"] for ln in ov["purpose"]]
    assert (
        "Guess: defines no resources itself; it composes 3 modules (1 git, 1 local, 1 registry)"
        in texts
    )
    assert "module.queue: registry terraform-aws-modules/sqs/aws @ 4.2.0" in texts
    tree = mermaid(m, "module-tree")
    assert (
        'T2["module.queue<br/>registry @ 4.2.0, not analyzed<br/>terraform-aws-modules/sqs/aws"]'
        in tree
    )
    assert "<br/>git @ v2.0.1, not analyzed<br/>git::https://git.example.com/" in tree
    assert architecture_graph(m)["nodes"] == []


def test_classification_backend_and_envs_multi_root():
    m = run_analysis(FIXTURES / "multi_root_envs").model
    roles = {
        r["dir"]: (r["role"], r["backend"]["type"], r["environments"])
        for r in m["structure"]["roots"]
    }
    assert roles == {
        "envs/dev": ("deployable", "s3", ["dev"]),
        "envs/prod": ("deployable", "s3", ["prod"]),
        "stacks/shared": ("deployable", "gcs", ["prod", "uat"]),
    }
    dirs = {d["dir"]: d for d in m["structure"]["module_dirs"]}
    assert dirs["modules/app"]["role"] == "child"
    assert [c["caller"] for c in dirs["modules/app"]["called_by"]] == ["envs/dev", "envs/prod"]
    matrix = env_matrix(m)
    assert matrix["columns"] == ["dev", "prod", "uat"]
    rows = {r["root"]: r for r in matrix["rows"]}
    assert rows["envs/dev"]["backend"] == "s3 repo-a-tfstate/envs/dev/terraform.tfstate"
    assert rows["envs/dev"]["cells"] == {"dev": "✓ dir", "prod": "", "uat": ""}
    assert rows["stacks/shared"]["cells"]["uat"] == "✓ tfvars (common.tfvars, uat.tfvars)"


# -- 3. zero analyzable files ---------------------------------------------------------------------


def test_empty_repo_fails_with_reason_and_listing(tmp_path, capsys):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "empty_repo", repo)
    (repo / ".git").mkdir()  # a broken checkout: listed as skipped, git log reported as failed
    a = run_analysis(repo)
    assert a.failed
    assert a.failure == (
        "nothing to analyze: no *.tf, *.tf.json or terragrunt.hcl file among the 3 file(s) found"
    )
    inv = a.model["discovery"]["inventory"]
    assert [e["path"] for e in inv["entries"]] == [".", ".git", "notes", "notes/todo", "scripts"]
    assert inv["by_extension"] == {".md": 1, ".sh": 1, ".txt": 1}
    text = listing_text(inv)
    assert (
        "notes/todo/" in text
        and ".git/" in text
        and "skipped: tool, VCS or hidden directory" in text
    )
    git = a.model["crawl"]["git"]
    assert git["status"] == "failed" and git["reason"]  # never silent
    ov = build_overview(a.model, "repo-a")
    assert ov["status"] == "failed" and ov["inventory"] == inv
    assert ov["purpose"][0]["text"] == "README (README.md): repo-a"  # docs are still crawled
    md = render_overview_markdown(ov, text)
    assert "**Analysis failed:**" in md and "Git history not read:" in md
    assert main(["--dir", str(repo), "--no-git"]) == 1
    err = capsys.readouterr().err
    assert "tflens: error: nothing to analyze" in err and "notes/todo/" in err


# -- 4. git log ingestion -----------------------------------------------------------------------


def test_git_log_ingestion(git_repo):
    a = run_analysis(git_repo)
    git = a.model["crawl"]["git"]
    assert git["status"] == "ok"
    head = _git(git_repo, "rev-parse", "HEAD")
    assert git["head"] == head
    commits = git["commits"]
    assert [(c["author"], c["date"], c["subject"]) for c in commits] == [
        ("dev-a", "2026-01-03T10:00:00+00:00", "Delay queue-a"),
        ("dev-b", "2026-01-02T10:00:00+00:00", "Add docs and CI"),
        ("dev-a", "2026-01-01T10:00:00+00:00", "Add queue-a and fn-a"),
    ]
    assert commits[2]["body"] == "First cut."
    assert commits[0]["files"] == ["main.tf"]
    assert commits[1]["files"] == [".github/workflows/deploy.yml", "README.md"]
    assert git["hot_files"][0] == {"path": "main.tf", "commits": 2, "exists": True}
    assert git["authors"] == [{"name": "dev-a", "commits": 2}, {"name": "dev-b", "commits": 1}]
    assert not git["truncated"]
    ov = build_overview(a.model, "repo-a")
    hist = ov["history"]
    assert hist["summary"][0]["text"].startswith(
        f"3 commits read (HEAD {head[:8]}); authors: dev-a (2), dev-b (1)"
    )
    first = hist["commits"][0]
    assert first["text"] == f"{head[:8]} 2026-01-03 dev-a: Delay queue-a (1 file)"
    assert first["location"] == {"file": "main.tf", "start_line": 1, "end_line": 1}
    assert first["commit"] == head
    assert hist["hot_files"][0]["text"] == "main.tf: changed in 2 commits"
    # the file a commit touched is readable through the API (crawled), others are not
    store = ModelStore(git_repo)
    assert store.lines("README.md") is not None
    # deterministic: same commit, same model
    assert run_analysis(git_repo).model == a.model


def test_git_log_of_a_subdirectory_is_relative(git_repo):
    infra = git_repo / "infra"
    infra.mkdir()
    (infra / "main.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "queue-b"\n}\n')
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "Add infra", date="2026-01-04T10:00:00+00:00")
    git = run_analysis(infra, git_root=git_repo).model["crawl"]["git"]
    assert [c["subject"] for c in git["commits"]] == ["Add infra"]  # only commits touching infra/
    assert git["commits"][0]["files"] == ["main.tf"]  # relative to the analyzed directory
    # without git_root the sub-directory is not a checkout itself
    assert run_analysis(infra).model["crawl"]["git"]["status"] == "skipped"
    assert (
        run_analysis(git_repo, git=False).model["crawl"]["git"]["reason"] == "git history disabled"
    )


# -- 5. overview -----------------------------------------------------------------------------------


def test_overview_purpose_text_and_drilldowns():
    a = run_analysis(FIXTURES / "app_flows")
    m = a.model
    assert (
        purpose_text(m)
        == "deploys 2 Lambda functions, 1 SQS queue, 1 DynamoDB table and 1 S3 bucket"
    )
    ov = build_overview(m, "repo-a", a.repo.lines)
    texts = [ln["text"] for ln in ov["purpose"]]
    assert texts[0] == "README (README.md): repo-a order pipeline"
    assert texts[1].startswith("Uploads to bucket-a are processed by fn-b")
    assert texts[2] == (
        "Guess: this repository deploys 2 Lambda functions, 1 SQS queue, 1 DynamoDB table "
        "and 1 S3 bucket, with 2 IAM roles."
    )
    assert "Applied by CI: GitHub Actions (deploy)" in texts
    drill = {d["title"]: d for d in ov["resources"]}
    fn_a = drill["Lambda function fn-a"]
    lines = [ln["text"] for ln in fn_a["lines"]]
    assert lines[0] == "What: Lambda function fn-a"
    assert "Triggered by: queue-a — event source mapping" in lines
    assert any(t.startswith("Can access: table-a — Allow: dynamodb:PutItem") for t in lines)
    assert fn_a["snippet"][0] == {
        "line": 63,
        "text": 'resource "aws_lambda_function" "worker" {',
        "highlight": True,
    }
    table = [ln["text"] for ln in drill["DynamoDB table table-a"]["lines"]]
    assert any(t.startswith("Accessed by: fn-a") for t in table)
    bucket = [ln["text"] for ln in drill["S3 bucket bucket-a"]["lines"]]
    assert any(t.startswith("Triggers: fn-b") for t in bucket)


@pytest.mark.parametrize("name", ANALYZABLE)
def test_every_overview_line_links_to_file_line(name):
    a = run_analysis(FIXTURES / name)
    ov = build_overview(a.model, "repo-a", a.repo.lines)
    lines = _all_lines(ov)
    assert lines and all(ln["location"] for ln in lines), [
        ln["text"] for ln in lines if not ln["location"]
    ]
    md = render_overview_markdown(ov)
    assert md == render_overview_markdown(build_overview(a.model, "repo-a", a.repo.lines))


# -- 6. visuals ------------------------------------------------------------------------------------


def _check_svg(text: str) -> ET.Element:
    root = ET.fromstring(text)
    assert root.tag == f"{SVG_NS}svg"
    assert float(root.get("width")) > 0 and float(root.get("height")) > 0
    assert root.get("viewBox") == f"0 0 {root.get('width')} {root.get('height')}"
    symbols = {s.get("id") for s in root.iter(f"{SVG_NS}symbol")}
    for use in root.iter(f"{SVG_NS}use"):
        assert use.get(XLINK)[1:] in symbols  # every icon is embedded: the file is standalone
    assert "<script" not in text and "http://" not in text.replace("http://www.w3.org/", "")
    return root


@pytest.mark.parametrize("name", [*ANALYZABLE, "empty_repo"])
def test_visual_exporters_produce_valid_mmd_and_svg(name, tmp_path):
    m = run_analysis(FIXTURES / name, git=False).model
    for diagram in DIAGRAMS:
        text = mermaid(m, diagram)
        assert validate_mermaid(text) == [], text
        assert text == mermaid(m, diagram)  # deterministic
        _check_svg(svg(m, diagram, "repo-a"))
    out = export_diagrams(m, tmp_path / "d", "repo-a")
    for diagram in DIAGRAMS:
        assert (tmp_path / "d" / f"{diagram}.mmd").read_text() == mermaid(m, diagram)
        _check_svg((tmp_path / "d" / f"{diagram}.svg").read_text())
    assert out["files"][:6] == [f"{d}.{x}" for d in DIAGRAMS for x in ("mmd", "svg")]
    assert out["png"]  # either the renderer used or why there is no PNG


def test_architecture_view_flows_and_icons():
    m = run_analysis(FIXTURES / "app_flows").model
    g = architecture_graph(m)
    tiers = {n["label"]: n["tier"] for n in g["nodes"]}
    assert tiers == {
        "queue-a": "trigger",
        "bucket-a": "trigger",
        "fn-a": "compute",
        "fn-b": "compute",
        "table-a": "data",
    }
    flows = {
        (e["kind"], e["source"].split(".")[-1], e["target"].split(".")[-1]) for e in g["edges"]
    }
    assert flows == {
        ("TRIGGER", "jobs", "worker"),
        ("TRIGGER", "uploads", "ingest"),
        ("ACCESS", "worker", "jobs"),
        ("ACCESS", "worker", "orders"),
        ("ACCESS", "ingest", "uploads"),
    }
    text = mermaid(m, "architecture")
    assert text.startswith("flowchart LR\n") and 'subgraph R0["./ root"]' in text
    assert '-->|"TRIGGER: event source mapping"|' in text
    root = _check_svg(svg(m, "architecture", "repo-a"))
    ids = {s.get("id") for s in root.iter(f"{SVG_NS}symbol")}
    assert {"icon-lambda", "icon-sqs", "icon-dynamodb", "icon-s3"} <= ids
    # multi-root: one subgraph per root, modules nested inside
    multi = mermaid(run_analysis(FIXTURES / "multi_root_envs").model, "architecture")
    assert 'subgraph R0["envs/dev/"]' in multi and 'subgraph R0M0["module.app"]' in multi


def test_module_tree_rows():
    rows = module_tree(run_analysis(FIXTURES / "multi_root_envs").model)
    dev = [(r["depth"], r["label"], r["detail"]) for r in rows[:5]]
    assert dev == [
        (0, "envs/dev/", "terraform root, s3 backend"),
        (
            1,
            "module.alarms",
            "git git::https://git.example.com/org/repo-a-modules.git//alarms?ref=v0.3.1 @ v0.3.1 (not analyzed)",
        ),
        (1, "module.app", "local ../../modules/app"),
        (1, "module.label", "registry cloudposse/label/null @ 0.25.0 (not analyzed)"),
        (1, "module.network", "local ../../modules/network"),
    ]


# -- 7. persistence and Changes ---------------------------------------------------------------


def test_database_has_snapshot_tables(tmp_path):
    p = tmp_path / "t.db"
    init_db(p)
    with closing(p) as c:
        assert schema_version(c) == SCHEMA_VERSION == 2
        names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names


MODULES_TF = """module "queue" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.2.0"
  name    = "queue-c"
}
"""


def test_changes_diff_against_previous_run(tmp_path, db_path):
    repo_dir = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "app_flows", repo_dir)
    (repo_dir / "modules.tf").write_text(MODULES_TF)
    out_dir = tmp_path / "out"
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=tmp_path / "repos", path=str(repo_dir))
        repo = tfrepo.get_repo(conn, repo_id)
        first = tfrepo.create_run(conn, repo, None, out_dir, None)
        assert has_snapshot(conn, first)
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE run_id=?", (first,)).fetchone()[0]
            for t in TABLES
        }
        first_dir = Path(tfrepo.get_run(conn, first)["model_path"]).parent
    assert (
        counts["run_resources"] == 11 and counts["run_modules"] == 1 and counts["run_backends"] == 1
    )
    assert counts["run_edges"] > 0 and counts["run_files"] == 4
    assert "Changes since the previous run" not in (first_dir / "handoff.md").read_text()
    # edit the repository: add a queue, drop the table, rename the bucket, bump a module
    main_tf = repo_dir / "main.tf"
    text = main_tf.read_text().replace('bucket = "bucket-a"', 'bucket = "bucket-b"')
    start = text.index('resource "aws_dynamodb_table"')
    end = text.index('resource "aws_s3_bucket"')
    text = text[:start] + text[end:] + 'resource "aws_sqs_queue" "extra" {\n  name = "queue-b"\n}\n'
    main_tf.write_text(text)
    (repo_dir / "modules.tf").write_text(MODULES_TF.replace("4.2.0", "4.3.0"))
    with closing(db_path) as conn:
        second = tfrepo.create_run(conn, repo, None, out_dir, None)
        runs = {r["id"]: r for r in tfrepo.list_runs(conn, repo_id)}
        diff = diff_runs(conn, runs[first], runs[second])
        # an older run without snapshot rows is back-filled from its model.json
        for t in TABLES:
            conn.execute(f"DELETE FROM {t} WHERE run_id=?", (first,))
        assert diff_runs(conn, runs[first], runs[second])["counts"] == diff["counts"]
        assert [r["id"] for r in runs_at_commit(conn, repo_id, "")] == [second, first]
    res = diff["resources"]
    assert [r["node_id"] for r in res["added"]] == ["aws_sqs_queue.extra"]
    assert [r["node_id"] for r in res["removed"]] == ["aws_dynamodb_table.orders"]
    changed = {c["key"]: c["changes"] for c in res["changed"]}
    assert changed["aws_s3_bucket.uploads"] == ["bucket: bucket-a → bucket-b"]
    assert any(e["target"] == "aws_dynamodb_table.orders" for e in diff["edges"]["removed"])
    assert diff["modules"]["changed"][0]["changes"] == ["version: 4.2.0 → 4.3.0"]
    assert diff["versions"] == diff["modules"]["changed"]
    assert not diff["empty"] and diff["counts"]["resources"] == {
        "added": 1,
        "removed": 1,
        "changed": len(res["changed"]),
    }
    handoff = (Path(runs[second]["model_path"]).parent / "handoff.md").read_text()
    assert "## Changes since the previous run" in handoff
    assert "- added: aws_sqs_queue.extra (aws_sqs_queue)" in handoff
    assert "  - version: 4.2.0 → 4.3.0" in handoff
    diagrams = Path(runs[second]["model_path"]).parent / "diagrams"
    assert {p.name for p in diagrams.glob("*.mmd")} == {f"{d}.mmd" for d in DIAGRAMS}


def test_diff_snapshots_is_pure_and_symmetric():
    old = snapshot(run_analysis(FIXTURES / "tf_json").model)
    assert diff_snapshots(old, old)["empty"]
    new = snapshot(run_analysis(FIXTURES / "app_flows").model)
    fwd, back = diff_snapshots(old, new), diff_snapshots(new, old)
    assert fwd["counts"]["resources"]["added"] == back["counts"]["resources"]["removed"]


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


def test_changes_page_and_run_files(client, tmp_path):
    repo_dir = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "module_only", repo_dir)
    resp = _post(client, "/repos", {"source": "local", "path": str(repo_dir)})
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    _post(client, f"/repos/{repo_id}/analyze")
    main_tf = repo_dir / "main.tf"
    main_tf.write_text(main_tf.read_text().replace('"4.2.0"', '"4.3.0"'))
    run_url = _post(client, f"/repos/{repo_id}/analyze", {"open": "1"}).headers["Location"]
    run_id = int(run_url.strip("/").rsplit("/", 1)[1])
    page = client.get(f"/runs/{run_id}/changes").data.decode()
    assert "Module version changes" in page and "version: 4.2.0 → 4.3.0" in page
    assert client.get(f"/runs/{run_id}/changes?base=999999").status_code == 404
    runs_page = client.get("/runs").data.decode()
    assert (
        f"/runs/{run_id}/changes" in runs_page and f"/runs/{run_id}/files/handoff.md" in runs_page
    )
    handoff = client.get(f"/runs/{run_id}/files/handoff.md")
    assert handoff.status_code == 200 and b"## Changes since the previous run" in handoff.data
    assert client.get(f"/runs/{run_id}/files/diagrams/module-tree.svg").status_code == 200
    assert client.get(f"/runs/{run_id}/files/../../tflens.db").status_code == 404
    # the run's analyzer serves the recorded handoff and the diagrams
    api_handoff = client.get(run_url + "api/handoff").data
    assert api_handoff == handoff.data
    mmd = client.get(run_url + "api/diagram?name=module-tree&format=mmd")
    assert mmd.status_code == 200 and validate_mermaid(mmd.data.decode()) == []


# -- 8. handoff.md ---------------------------------------------------------------------------------


def _mermaid_blocks(text: str) -> list[str]:
    return re.findall(r"```mermaid\n(.*?)```", text, re.S)


def test_handoff_contains_overview_and_mermaid(tmp_path):
    out = tmp_path / "handoff.md"
    diagrams = tmp_path / "diagrams"
    rc = main(
        ["--dir", str(FIXTURES / "app_flows"), "--handoff", str(out), "--diagrams", str(diagrams)]
    )
    assert rc == 0
    text = out.read_text()
    assert text.startswith("# TFLens handoff: app_flows\n")
    assert "## How to use this file with Claude Code" in text
    assert "## Overview: app_flows" in text and "### Purpose (guess)" in text
    assert "deploys 2 Lambda functions, 1 SQS queue, 1 DynamoDB table and 1 S3 bucket" in text
    assert "#### Lambda function fn-a (`main.tf:63-69`)" in text  # drill-down with snippet
    blocks = _mermaid_blocks(text)
    assert len(blocks) == 3 and [b.split("\n", 1)[0] for b in blocks] == [
        "flowchart LR",
        "flowchart TD",
        "flowchart LR",
    ]
    assert all(validate_mermaid(b) == [] for b in blocks)
    assert {p.name for p in diagrams.iterdir()} >= {
        f"{d}.{x}" for d in DIAGRAMS for x in ("mmd", "svg")
    }
    # standalone analyzer: the same handoff and diagrams through the API
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "app_flows", repo)
    api = Api(ModelStore(repo))
    resp = api.handle("GET", "/api/handoff?download=1")
    assert (
        resp.status == 200
        and resp.headers["Content-Disposition"] == 'attachment; filename="handoff.md"'
    )
    assert len(_mermaid_blocks(resp.body.decode())) == 3
    svg_resp = api.handle("GET", "/api/diagram?name=architecture&format=svg")
    assert svg_resp.content_type.startswith("image/svg+xml")
    _check_svg(svg_resp.body.decode())
    assert api.handle("GET", "/api/diagram?name=nope").status == 404
    model = json.loads(api.handle("GET", "/api/model").body)
    assert render_handoff(model, "repo-a").count("```mermaid") == 3


def test_source_api_serves_crawled_files_only(tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "multi_root_envs", repo)
    (repo / "secrets.txt").write_text("not for the UI\n")
    api = Api(ModelStore(repo))
    ok = api.handle("GET", "/api/source?file=.github/workflows/terraform.yml&start=16")
    assert ok.status == 200 and "terraform -chdir" in ok.body.decode()
    assert api.handle("GET", "/api/source?file=secrets.txt&start=1").status == 404
    detail = json.loads(api.handle("GET", "/api/file?path=docs/architecture.md").body)
    assert detail["readable"] is True and detail["analyzed"] is False
