"""tflens serve: API views, model regeneration, evidence snippets, HTTP wrapper."""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest
from conftest import FIXTURES

from tflens.cli import main
from tflens.output import dumps, run_analysis
from tflens.report import render_report
from tflens.server import is_loopback, make_server, url_of
from tflens.snippets import format_snippet, snippet_lines
from tflens.webapi import Api, ModelStore

LAMBDA_ROLE_EDGE = (
    "CONFIG:module.worker.aws_lambda_function.this->module.worker.aws_iam_role.this#1"
)
S3_ACCESS_EDGE = "ACCESS:module.worker.aws_iam_role.this->module.storage.aws_s3_bucket.this#1"


@pytest.fixture
def repo(tmp_path):
    copy = tmp_path / "repo"
    shutil.copytree(FIXTURES / "multi_module", copy)
    return copy


@pytest.fixture
def api(repo):
    return Api(ModelStore(repo))


def get(api, target, status=200):
    resp = api.handle("GET", target)
    assert resp.status == status, resp.body
    return json.loads(resp.body) if resp.content_type.startswith("application/json") else resp


# ---------------------------------------------------------------- model store


def test_model_is_regenerated_when_missing(repo, api):
    path = repo / "tflens-out" / "model.json"
    assert not path.exists()
    summary = get(api, "/api/summary")
    assert path.is_file()
    assert summary["generated"] is True
    assert summary["model_path"] == str(path)
    assert json.loads(path.read_text()) == run_analysis(repo).model


def test_existing_model_is_read_and_reloaded_on_change(repo, tmp_path):
    model = run_analysis(repo).model
    model["summary"]["files"] = 99  # marker: proves the file was read, not regenerated
    path = tmp_path / "custom.json"
    path.write_text(dumps(model))
    store = ModelStore(repo, path)
    api = Api(store)
    assert get(api, "/api/summary")["summary"]["files"] == 99
    assert store.generated is False

    model["summary"]["files"] = 42
    path.write_text(dumps(model))
    import os

    os.utime(path, (1, 1))  # force a different mtime
    assert get(api, "/api/summary")["summary"]["files"] == 42

    path.unlink()
    assert get(api, "/api/summary")["summary"]["files"] == 4
    assert path.is_file() and store.generated is True


def test_unusable_model_is_regenerated(repo, tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"schema_version": "0"}))
    store = ModelStore(repo, path)
    assert store.model()["schema_version"] == "1"
    assert store.generated is True


def test_regenerate_endpoint(repo, api):
    get(api, "/api/summary")
    resp = api.handle("POST", "/api/regenerate")
    assert resp.status == 200
    assert json.loads(resp.body)["generated"] is True
    assert api.handle("POST", "/api/nothing").status == 404
    assert api.handle("DELETE", "/api/graph").status == 405


# ---------------------------------------------------------------- API views


def test_graph_endpoint(api):
    g = get(api, "/api/graph")
    ids = {n["data"]["id"] for n in g["nodes"]}
    assert "module.worker.aws_lambda_function.this" in ids
    assert "module.worker" not in ids  # module calls live in the repo view
    edge = next(e["data"] for e in g["edges"] if e["data"]["id"] == S3_ACCESS_EDGE)
    assert edge["kind"] == "ACCESS"
    assert edge["color"] == g["colors"]["kinds"]["ACCESS"]
    # only kinds present; no TRIGGER here because the subscription endpoint is unknown
    assert g["filters"]["kinds"] == ["ACCESS", "CONFIG", "RUNS_ON"]
    assert "(root)" in g["filters"]["modules"] and "module.worker" in g["filters"]["modules"]
    assert {"lambda", "s3", "sqs", "iam", "external"} <= set(g["filters"]["services"])
    lam = next(n["data"] for n in g["nodes"] if n["data"]["service"] == "lambda")
    assert lam["color"] == g["colors"]["services"]["lambda"]
    assert get(api, "/api/graph") == g  # deterministic


def test_nodes_and_edges_endpoints(api):
    nodes = get(api, "/api/nodes")
    assert [n["id"] for n in nodes] == sorted(n["id"] for n in nodes)
    assert any(n["id"] == "aws_sqs_queue.jobs" and n["module"] == "(root)" for n in nodes)
    edges = get(api, "/api/edges")
    assert S3_ACCESS_EDGE in {e["id"] for e in edges}


def test_node_detail_has_evidence_snippets_definition_and_unknowns(api):
    d = get(api, "/api/node?id=" + quote("module.worker.aws_lambda_function.this"))
    assert d["summary"]["description"] == "Lambda function `example-dev-worker`"
    assert d["summary"]["module"] == "module.worker"
    definer = d["defined_by"]
    assert (definer["file"], definer["start_line"], definer["end_line"]) == (
        "modules/worker/main.tf",
        45,
        51,
    )
    assert definer["lines"][0] == {
        "line": 45,
        "text": 'resource "aws_lambda_function" "this" {',
        "highlight": True,
    }
    role = next(e for e in d["evidence"] if e["edge"] == LAMBDA_ROLE_EDGE)
    assert role["lines"] == [
        {"line": 45, "text": 'resource "aws_lambda_function" "this" {', "highlight": False},
        {"line": 46, "text": "  function_name = var.name", "highlight": False},
        {"line": 47, "text": "  role          = aws_iam_role.this.arn", "highlight": True},
        {"line": 48, "text": '  runtime       = "python3.12"', "highlight": False},
        {"line": 49, "text": '  handler       = "app.handler"', "highlight": False},
    ]
    assert {e["kind"] for e in d["edges"]["outgoing"]} >= {"ACCESS", "CONFIG", "RUNS_ON"}

    sub = get(api, "/api/node?id=" + quote("aws_sns_topic_subscription.alerts_email"))
    assert sub["unknowns"] == [
        {
            "subject": "aws_sns_topic_subscription.alerts_email",
            "attribute": "endpoint",
            "reason": "variable var.alert_email has no default and no value in tfvars/--var-file",
            "location": {"file": "main.tf", "start_line": 55, "end_line": 55},
        }
    ]
    # a module call that could not be analyzed has no graph node, but still a detail
    remote = get(api, "/api/node?id=module.network")
    assert remote["summary"]["kind"] == "module"
    assert (remote["defined_by"]["start_line"], remote["defined_by"]["end_line"]) == (38, 42)
    assert "non-local source" in remote["unknowns"][0]["reason"]
    ext = get(api, "/api/node?id=" + quote("external:runtime:python3.12"))
    assert ext["defined_by"] is None


def test_edge_detail_has_highlighted_evidence(api):
    d = get(api, "/api/edge?id=" + quote(S3_ACCESS_EDGE))
    assert d["summary"]["label"] == "Allow: s3:GetObject, s3:PutObject"
    (ev,) = d["evidence"]
    assert (ev["file"], ev["start_line"], ev["end_line"]) == ("modules/worker/main.tf", 24, 27)
    highlighted = [(ln["line"], ln["text"]) for ln in ev["lines"] if ln["highlight"]]
    assert highlighted == [
        (24, "  statement {"),
        (25, '    actions   = ["s3:GetObject", "s3:PutObject"]'),
        (26, '    resources = ["${var.bucket_arn}/*"]'),
        (27, "  }"),
    ]
    assert [ln["line"] for ln in ev["lines"] if not ln["highlight"]] == [22, 23, 28, 29]
    assert [x["node"] for x in d["defined_by"]] == ["module.worker.aws_iam_role_policy.this"]
    assert d["source"]["id"] == "module.worker.aws_iam_role.this"


def test_repo_tree_and_file_listing(api):
    tree = get(api, "/api/tree")
    assert tree["modules"] == ["(root)"]
    names = [c["name"] for c in tree["children"]]
    assert names == ["modules", "main.tf", "terraform.tfvars"]
    worker = tree["children"][0]["children"][1]
    assert worker["path"] == "modules/worker"
    assert worker["modules"] == ["module.worker"]
    assert worker["children"] == [
        {"name": "main.tf", "path": "modules/worker/main.tf", "type": "file", "resources": 5}
    ]
    f = get(api, "/api/file?path=" + quote("modules/worker/main.tf"))
    assert f["modules"] == ["module.worker"]
    assert [r["id"] for r in f["resources"]] == [
        "module.worker.data.aws_iam_policy_document.assume",
        "module.worker.data.aws_iam_policy_document.access",
        "module.worker.aws_iam_role.this",
        "module.worker.aws_iam_role_policy.this",
        "module.worker.aws_lambda_function.this",
    ]
    root_file = get(api, "/api/file?path=main.tf")
    network = next(r for r in root_file["resources"] if r["id"] == "module.network")
    assert network["kind"] == "module" and network["analyzed"] is False
    assert any(u["attribute"] == "endpoint" for u in root_file["unknowns"])


def test_remote_module_in_repo_view(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "remote_module", repo)
    api = Api(ModelStore(repo))
    f = get(api, "/api/file?path=" + quote(".terraform/modules/queues/main.tf"))
    assert f["modules"] == ["module.queues"]
    assert [r["id"] for r in f["resources"]] == [
        "module.queues.module.dlq",
        "module.queues.aws_sqs_queue.this",
    ]
    d = get(api, "/api/node?id=module.queues.aws_sqs_queue.this")
    assert d["defined_by"]["file"] == ".terraform/modules/queues/main.tf"
    assert [u["attribute"] for u in d["unknowns"]] == ["kms_master_key_id"]


@pytest.mark.parametrize(
    "target",
    [
        "/api/node?id=aws_sqs_queue.nope",
        "/api/node",
        "/api/edge?id=nope",
        "/api/file?path=../../etc/passwd",
        "/api/file?path=missing.tf",
        "/api/unknown",
        "/nope",
        "/static/../webapi.py",
        "/static/missing.js",
    ],
)
def test_not_found(api, target):
    assert api.handle("GET", target).status == 404


def test_source_reads_only_listed_files(repo):
    store = ModelStore(repo)
    (repo / "secret.txt").write_text("x\n")
    assert store.lines("secret.txt") is None
    assert store.lines("../repo/main.tf") is None
    assert store.lines("main.tf")[0] == 'variable "env" {'


def test_index_and_static_assets(api):
    index = api.handle("GET", "/")
    assert index.status == 200 and index.content_type.startswith("text/html")
    html = index.body.decode()
    assert 'src="static/vendor/cytoscape.min.js"' in html
    assert 'src="static/app.js"' in html
    assert "http://" not in html and "https://" not in html  # no external requests
    js = api.handle("GET", "/static/app.js")
    assert js.status == 200 and "javascript" in js.content_type
    assert "/api/graph" in js.body.decode()


# ---------------------------------------------------------------- snippets / report


def test_snippet_highlighting_is_deterministic():
    lines = ["a", "b", "c", "d", "e", "f"]
    ev = {"file": "x.tf", "start_line": 3, "end_line": 4, "snippet": "c\nd"}
    out = snippet_lines(ev, lambda f: lines)
    assert [(ln["line"], ln["highlight"]) for ln in out] == [
        (1, False),
        (2, False),
        (3, True),
        (4, True),
        (5, False),
        (6, False),
    ]
    assert format_snippet(out) == "  1 | a\n  2 | b\n> 3 | c\n> 4 | d\n  5 | e\n  6 | f"
    # without the source file only the evidence lines are shown
    assert format_snippet(snippet_lines(ev)) == "> 3 | c\n> 4 | d"
    truncated = {
        "file": "x.tf",
        "start_line": 1,
        "end_line": 50,
        "snippet": "a\n... (49 more lines)",
    }
    assert snippet_lines(truncated, lambda f: lines)[-1] == {
        "line": None,
        "text": "... (49 more lines)",
        "highlight": False,
    }


def test_report_embeds_evidence_snippets(tmp_path):
    a = run_analysis(FIXTURES / "multi_module")
    text = render_report(a.model, "multi_module", a.repo.lines)
    assert "## Evidence" in text
    edge_line = next(
        ln
        for ln in text.splitlines()
        if ln.startswith("- `module.storage.aws_s3_bucket.this` — Allow: s3:GetObject")
    )
    n = edge_line.rsplit("[E", 1)[1].split("]", 1)[0]
    section = text.split(f'<a id="evidence-e{n}"></a>')[1].split("<a id=")[0]
    assert (
        f"### E{n}: ACCESS `module.worker.aws_iam_role.this` → "
        "`module.storage.aws_s3_bucket.this`" in section
    )
    assert "`modules/worker/main.tf:24-27` — policy statement" in section
    assert (
        "```hcl\n"
        "  22 |\n"
        '  23 | data "aws_iam_policy_document" "access" {\n'
        "> 24 |   statement {\n"
        '> 25 |     actions   = ["s3:GetObject", "s3:PutObject"]\n'
        '> 26 |     resources = ["${var.bucket_arn}/*"]\n'
        "> 27 |   }\n"
        "  28 |\n"
        "  29 |   statement {\n"
        "```"
    ) in section
    assert render_report(a.model, "multi_module", a.repo.lines) == text


def test_cli_report_contains_snippets(tmp_path):
    rep = tmp_path / "report.md"
    assert main(["--dir", str(FIXTURES / "lambda"), "--report", str(rep)]) == 0
    text = rep.read_text()
    assert "## Evidence" in text and "```hcl\n" in text and "\n> " in text


# ---------------------------------------------------------------- HTTP wrapper / CLI


def test_http_server_roundtrip(repo):
    server = make_server(Api(ModelStore(repo)), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = url_of(server)
        with urllib.request.urlopen(base + "api/graph", timeout=10) as r:
            assert r.status == 200
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]
            assert json.loads(r.read())["filters"]["kinds"]
        with urllib.request.urlopen(base + "api/edge?id=" + quote(S3_ACCESS_EDGE), timeout=10) as r:
            assert json.loads(r.read())["evidence"][0]["start_line"] == 24
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(base + "api/node?id=missing", timeout=10)
        assert err.value.code == 404
        # POST without the custom header is refused (cross-site request guard)
        req = urllib.request.Request(base + "api/regenerate", method="POST", data=b"")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 403
        req.add_header("X-TFLens", "1")
        with urllib.request.urlopen(req, timeout=10) as r:
            assert json.loads(r.read())["generated"] is True
        # DNS-rebinding guard: foreign Host header is refused
        req = urllib.request.Request(base + "api/summary", headers={"Host": "evil.example"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        assert err.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_non_loopback_host_requires_allow_remote(repo, capsys):
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.0.2.10")
    with pytest.raises(ValueError, match="--allow-remote"):
        make_server(Api(ModelStore(repo)), "0.0.0.0", 0)
    assert main(["serve", "--dir", str(repo), "--host", "0.0.0.0", "--no-browser"]) == 2
    assert "--allow-remote" in capsys.readouterr().err
    assert main(["serve", "--dir", str(repo / "missing"), "--no-browser"]) == 2


def test_serve_subcommand_starts_and_keeps_dir_cli(repo, monkeypatch):
    calls = {}

    def fake_serve_forever(server, open_browser):
        calls["url"] = url_of(server)
        calls["open_browser"] = open_browser
        server.server_close()

    monkeypatch.setattr("tflens.server.serve_forever", fake_serve_forever)
    assert main(["serve", "--dir", str(repo), "--port", "0", "--no-browser"]) == 0
    assert calls["url"].startswith("http://127.0.0.1:")
    assert calls["open_browser"] is False
    assert (repo / "tflens-out" / "model.json").is_file()
