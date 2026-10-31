"""Flask app: security (Host / CSRF / CSP), lazy database, pages, runs in the analyzer."""

from __future__ import annotations

import json
import re
import shutil

import pytest
from conftest import FIXTURES

from tflens.cli import main
from tflens.db import closing
from tflens.web import create_app, host_allowed, local_time, utc_iso
from tflens.webapi import ModelStore

FAKE_KEY_ID = "AKIAEXAMPLEEXAMPLE00"
FAKE_SECRET = "example/secret/value/for/tests/only/0000"
FAKE_TOKEN = "ghp_" + "Example0Token0For0Tests0Only0000"
FILE_PROTOCOLS = ("https", "ssh", "file")
PAGES = ("/repos", "/runs", "/accounts", "/accounts/new", "/settings", "/logs")


@pytest.fixture
def app(home):
    return create_app(home, testing=True, git_protocols=FILE_PROTOCOLS)


@pytest.fixture
def client(app):
    c = app.test_client()
    c.get("/repos")
    return c


@pytest.fixture
def repo_dir(tmp_path):
    copy = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "multi_module", copy)
    (copy / "envs").mkdir()
    (copy / "envs" / "dev.tfvars").write_text('env = "dev"\n')
    return copy


def _csrf(client) -> str:
    with client.session_transaction() as s:
        return s["csrf"]


def _post(client, url, data=None, **kw):
    data = dict(data or {})
    data["csrf_token"] = _csrf(client)
    return client.post(url, data=data, **kw)


def _paths(app):
    return app.extensions["tflens"]["paths"]


def _dump(app) -> str:
    with closing(_paths(app).db_path) as conn:
        return "\n".join(conn.iterdump())


def _log_text(app) -> str:
    path = app.extensions["tflens"]["log_dir"] / "tflens.log"
    return path.read_text() if path.exists() else ""


def _add_local_repo(client, path) -> int:
    resp = _post(client, "/repos", {"source": "local", "path": str(path)})
    assert resp.status_code == 302, resp.data
    return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])


# -- lazy database ---------------------------------------------------------------------------


def test_pages_render_without_creating_the_database(app, client):
    paths = _paths(app)
    for url in PAGES:
        assert client.get(url).status_code == 200, url
    assert client.get("/").headers["Location"].endswith("/repos")
    assert not paths.db_path.exists()
    assert not paths.key_path.exists()
    assert "no database yet" in client.get("/settings").data.decode()


def test_first_settings_save_creates_the_database(app, client, tmp_path):
    paths = _paths(app)
    assert not paths.db_path.exists()
    new_logs = tmp_path / "other-logs"
    resp = _post(
        client,
        "/settings",
        {
            "server_form": "1",
            "log_dir": str(new_logs),
            "host": "127.0.0.1",
            "port": "9000",
            "retention_runs": "3",
            "auto_regenerate": "1",
        },
    )
    assert resp.status_code == 302
    assert paths.db_path.is_file()
    s = app.extensions["tflens"]["store"].load()
    assert (s.port, s.retention_runs, s.auto_regenerate, s.allow_remote) == (9000, 3, True, False)
    assert app.extensions["tflens"]["log_dir"] == new_logs
    assert "settings saved" in (new_logs / "tflens.log").read_text()
    page = client.get("/settings").data.decode()
    assert 'value="9000"' in page and "Settings saved" in page


def test_invalid_settings_are_flashed_and_not_saved(app, client):
    resp = _post(client, "/settings", {"port": "99999"}, follow_redirects=True)
    assert "Settings not saved: the port must be" in resp.data.decode()
    assert app.extensions["tflens"]["store"].load().port == 8765


def test_dir_modes_never_create_the_data_directory(_isolated_home, tmp_path, repo_dir):
    """``tflens --dir`` and ``tflens serve --dir`` (standalone app) leave ~/.tflens alone."""
    default_home = _isolated_home / ".tflens"
    assert main(["--dir", str(repo_dir), "--out", str(tmp_path / "model.json")]) == 0
    app = create_app(store=ModelStore(repo_dir), testing=True)
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/api/graph").status_code == 200
    assert c.post("/api/regenerate", headers={"X-TFLens": "1"}).status_code == 200
    assert not default_home.exists()
    assert not (default_home / "tflens.db").exists()
    # the managed app uses it, but still creates the database only on the first save
    managed = create_app(testing=True)
    assert _paths(managed).home == default_home.resolve()
    managed.test_client().get("/settings")
    assert not (default_home / "tflens.db").exists()


def test_data_directory_is_private(app):
    import os
    import stat

    assert stat.S_IMODE(os.stat(_paths(app).home).st_mode) == 0o700


# -- security -------------------------------------------------------------------------------


def test_post_requires_csrf(client):
    assert client.post("/settings").status_code == 400
    assert client.post("/settings", data={"csrf_token": "wrong"}).status_code == 400
    assert client.post("/repos", data={"path": "/tmp"}).status_code == 400


@pytest.mark.parametrize(
    "host, port, ok",
    [
        ("127.0.0.1:8765", 8765, True),
        ("localhost:8765", 8765, True),
        ("[::1]:8765", 8765, True),
        ("localhost", None, True),
        ("localhost:9999", 8765, False),  # wrong port
        ("evil.example", None, False),
        ("evil.example:8765", 8765, False),  # DNS rebinding: attacker name, our port
        ("127.0.0.1.evil.example:8765", 8765, False),
        ("192.0.2.5:8765", 8765, False),
        ("localhost:abc", None, False),
        ("", None, False),
    ],
)
def test_host_allowed(host, port, ok):
    assert host_allowed(host, port) is ok


@pytest.mark.parametrize(
    "host, ok",
    [
        ("192.0.2.5:8765", True),  # --allow-remote: an IP address is no rebinding vector
        ("[2001:db8::5]:8765", True),
        ("localhost:8765", True),
        ("192.0.2.5:9999", False),  # the port still has to match
        ("[2001:db8::5]", False),
        ("evil.example:8765", False),
        ("192.0.2.5.evil.example:8765", False),
    ],
)
def test_host_allowed_with_ip_literals(host, ok):
    assert host_allowed(host, 8765, ip_literals=True) is ok
    if ok and not host.startswith("localhost"):
        assert host_allowed(host, 8765) is False  # local-only by default


def test_ip_host_header_is_served_only_when_remote_is_allowed(home):
    for allow in (False, True):
        app = create_app(home, testing=True, port=8765, allow_remote=allow)
        resp = app.test_client().get("/repos", headers={"Host": "192.0.2.5:8765"})
        assert (resp.status_code == 400) is not allow


def test_dns_rebinding_host_rejected(home, repo_dir):
    app = create_app(home, testing=True, port=8765)
    c = app.test_client()
    assert c.get("/repos", headers={"Host": "localhost:8765"}).status_code == 200
    assert c.get("/repos", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    for bad in ("evil.example:8765", "evil.example", "localhost:8766"):
        resp = c.get("/settings", headers={"Host": bad})
        assert resp.status_code == 400, bad
        assert b"invalid Host header" in resp.data
    # rejected before CSRF / route handling
    assert c.post("/settings", headers={"Host": "evil.example:8765"}).status_code == 400
    assert "untrusted Host header 'evil.example:8765'" in _log_text(app)
    # the standalone analyzer is guarded the same way
    standalone = create_app(store=ModelStore(repo_dir), testing=True, port=8765)
    resp = standalone.test_client().get("/api/summary", headers={"Host": "evil.example:8765"})
    assert resp.status_code == 400


def test_security_headers_and_no_external_resources(client):
    for url in (*PAGES, "/static/favicon.svg"):
        resp = client.get(url)
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp, url
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        body = resp.data.decode()
        assert not re.search(r"(src|href)=\"https?://", body), url
        assert "<script>" not in body and " onclick=" not in body and " onchange=" not in body
        resp.close()


def test_api_post_needs_the_custom_header(tmp_path, repo_dir):
    app = create_app(store=ModelStore(repo_dir), testing=True)
    c = app.test_client()
    assert c.post("/api/regenerate").status_code == 403
    resp = c.post("/api/regenerate", headers={"X-TFLens": "1"})
    assert resp.status_code == 200 and resp.get_json()["generated"] is True


# -- accounts -------------------------------------------------------------------------------


def test_account_secret_is_encrypted_masked_and_never_logged(app, client):
    resp = _post(
        client,
        "/accounts/new",
        {
            "display_name": "example-dev",
            "region": "us-east-1",
            "auth_mode": "keys",
            "access_key_id": FAKE_KEY_ID,
            "secret_access_key": FAKE_SECRET,
        },
    )
    assert resp.status_code == 302
    page = client.get("/accounts").data.decode()
    assert "example-dev" in page and "AKIA" + "*" * 12 + "LE00" in page
    (acct,) = app.extensions["tflens"]["accounts"].list()
    edit = client.get(f"/accounts/{acct.id}/edit").data.decode()
    for text in (page, edit, _log_text(app)):
        assert FAKE_SECRET not in text and FAKE_KEY_ID not in text
    # as in IPLens: the key id is stored in clear (shown masked), the secret encrypted
    assert FAKE_SECRET not in _dump(app)
    assert _paths(app).key_path.is_file()
    assert _post(client, f"/accounts/{acct.id}/delete").status_code == 302
    assert app.extensions["tflens"]["accounts"].list() == []


def test_account_validation_error_rerenders_without_secrets(client):
    resp = _post(
        client,
        "/accounts/new",
        {"region": "us-east-1", "auth_mode": "keys", "secret_access_key": FAKE_SECRET},
    )
    assert resp.status_code == 400
    assert FAKE_SECRET not in resp.data.decode()


# -- repositories, environments, runs ---------------------------------------------------------


def test_local_repo_env_analyze_and_open_run(app, client, repo_dir):
    repo_id = _add_local_repo(client, repo_dir)
    page = client.get(f"/repos/{repo_id}").data.decode()
    assert "repo-a" in page and str(repo_dir) in page

    resp = _post(
        client,
        f"/repos/{repo_id}/envs",
        {"env": "dev", "var_files": "envs/dev.tfvars", "extra_vars": f"token={FAKE_TOKEN}"},
    )
    assert resp.status_code == 302
    page = client.get(f"/repos/{repo_id}").data.decode()
    assert "envs/dev.tfvars" in page and "token" in page and FAKE_TOKEN not in page
    with closing(_paths(app).db_path) as conn:
        env_id = conn.execute("SELECT id FROM tf_repo_envs").fetchone()[0]

    resp = _post(client, f"/repos/{repo_id}/analyze", {"env_id": str(env_id), "open": "1"})
    assert resp.status_code == 302
    run_url = resp.headers["Location"]
    assert re.fullmatch(r"/runs/\d+/", run_url)
    run_id = int(run_url.split("/")[2])

    # the run opens in the analyzer, read-only
    index = client.get(run_url)
    assert index.status_code == 200 and b"/static/app.js" in index.data
    summary = client.get(run_url + "api/summary").get_json()
    assert summary["readonly"] is True and summary["title"] == f"repo-a (dev) · run {run_id}"
    assert client.get(run_url + "api/graph").get_json()["filters"]["kinds"]
    node = "module.worker.aws_lambda_function.this"
    detail = client.get(run_url + "api/node", query_string={"id": node}).get_json()
    assert detail["defined_by"]["lines"]  # snippets read from the repository
    regen = client.post(run_url + "api/regenerate", headers={"X-TFLens": "1"})
    assert regen.status_code == 405
    assert client.get(f"/runs/{run_id}").status_code in (301, 308)  # trailing slash

    model_path = _paths(app).out_dir / f"{repo_id}-repo-a" / str(run_id) / "model.json"
    assert model_path.is_file()
    assert '"nodes": [' not in _dump(app)  # the model JSON is never in the database
    assert FAKE_TOKEN not in _dump(app) and FAKE_TOKEN not in _log_text(app)

    runs = client.get("/runs").data.decode()
    assert f'href="/runs/{run_id}/"' in runs and "dev" in runs
    assert f'href="/runs/{run_id}/"' in client.get(f"/runs?repo={repo_id}").data.decode()

    assert _post(client, f"/runs/{run_id}/delete").status_code == 302
    assert not model_path.exists()
    assert client.get(run_url).status_code == 404
    assert not (repo_dir / "tflens-out").exists()  # nothing written into the repository


def test_analyze_without_env_and_retention(app, client, repo_dir):
    _post(client, "/settings", {"server_form": "1", "retention_runs": "2"})
    repo_id = _add_local_repo(client, repo_dir)
    for _ in range(3):
        assert _post(client, f"/repos/{repo_id}/analyze").status_code == 302
    with closing(_paths(app).db_path) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM runs ORDER BY id")]
    assert ids == [2, 3]
    assert client.get("/runs/1/").status_code == 404


def test_env_edit_and_delete_via_forms(app, client, repo_dir):
    repo_id = _add_local_repo(client, repo_dir)
    _post(client, f"/repos/{repo_id}/envs", {"env": "dev", "extra_vars": "region=us-east-1"})
    with closing(_paths(app).db_path) as conn:
        env_id = conn.execute("SELECT id FROM tf_repo_envs").fetchone()[0]
    resp = _post(
        client, f"/repos/{repo_id}/envs/{env_id}", {"env": "uat", "var_files": "envs/dev.tfvars"}
    )
    assert resp.status_code == 302
    with closing(_paths(app).db_path) as conn:
        row = conn.execute("SELECT env, var_files, extra_vars_keys FROM tf_repo_envs").fetchone()
    assert tuple(row) == ("uat", '["envs/dev.tfvars"]', "region")  # blank vars are kept
    bad = _post(client, f"/repos/{repo_id}/envs", {"env": "x", "extra_vars": "no equals sign"})
    assert "line 1: expected KEY=VALUE" in client.get(bad.headers["Location"]).data.decode()
    assert _post(client, f"/repos/{repo_id}/envs/{env_id}/delete").status_code == 302
    assert _post(client, f"/repos/{repo_id}/envs/{env_id}/delete").status_code == 404


def test_git_repo_with_token_never_exposes_it(app, client):
    resp = _post(
        client,
        "/repos",
        {
            "source": "git",
            "url": "https://git.example.com/org/repo-b.git",
            "branch": "main",
            "git_auth": "token",
            "token": FAKE_TOKEN,
        },
    )
    assert resp.status_code == 302
    repo_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    page = client.get(f"/repos/{repo_id}").data.decode()
    assert "stored, leave blank to keep" in page
    for text in (page, client.get("/repos").data.decode(), _dump(app), _log_text(app)):
        assert FAKE_TOKEN not in text
    # credentials in the URL are refused
    resp = _post(
        client,
        "/repos",
        {"source": "git", "url": f"https://x:{FAKE_TOKEN}@git.example.com/o/r.git"},
        follow_redirects=True,
    )
    assert "do not put credentials in the URL" in resp.data.decode()
    assert FAKE_TOKEN not in resp.data.decode() and FAKE_TOKEN not in _log_text(app)


def test_git_sync_and_auto_regenerate(app, client, repo_dir):
    import subprocess

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run([*git, "add", "."], cwd=repo_dir, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    _post(client, "/settings", {"server_form": "1", "auto_regenerate": "1"})
    resp = _post(
        client,
        "/repos",
        {"source": "git", "url": repo_dir.as_uri(), "git_auth": "none", "name": "repo-b"},
    )
    repo_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    _post(client, f"/repos/{repo_id}/envs", {"env": "dev", "var_files": "envs/dev.tfvars"})
    _post(client, f"/repos/{repo_id}/envs", {"env": "prod"})
    resp = _post(client, f"/repos/{repo_id}/sync", follow_redirects=True)
    page = resp.data.decode()
    assert "Synced repo-b at" in page and page.count("Analyzed repo-b /") == 2
    assert (_paths(app).repos_dir / str(repo_id) / "main.tf").is_file()
    with closing(_paths(app).db_path) as conn:
        envs = [r[0] for r in conn.execute("SELECT env FROM runs ORDER BY env")]
        commits = {r[0] for r in conn.execute("SELECT commit_sha FROM runs")}
    assert envs == ["dev", "prod"] and len(next(iter(commits))) == 40
    # removing the repository deletes its clone, keeps the run history
    assert _post(client, f"/repos/{repo_id}/delete").status_code == 302
    assert not (_paths(app).repos_dir / str(repo_id)).exists()
    assert "(removed)" in client.get("/runs").data.decode()
    assert repo_dir.is_dir()


def test_failed_sync_is_flashed_and_logged_without_secrets(app, client):
    resp = _post(
        client,
        "/repos",
        {
            "source": "git",
            "url": "https://git.example.com/org/repo-b.git",
            "git_auth": "token",
            "token": FAKE_TOKEN,
        },
    )
    repo_id = int(resp.headers["Location"].rsplit("/", 1)[1])

    def runner(argv, **kw):
        import subprocess

        return subprocess.CompletedProcess(argv, 128, "", f"fatal: denied {FAKE_TOKEN}")

    app.extensions["tflens"]["git_runner"] = runner
    page = _post(client, f"/repos/{repo_id}/sync", follow_redirects=True).data.decode()
    assert "Sync of repo-b failed: fatal: denied [REDACTED]" in page
    assert FAKE_TOKEN not in _log_text(app) and "sync of repo=" in _log_text(app)


def test_unknown_ids_are_404(client):
    for url in ("/repos/99", "/runs/99/", "/runs/99/api/graph", "/accounts/99/edit"):
        assert client.get(url).status_code == 404, url
    assert _post(client, "/repos/99/delete").status_code == 404


def test_logs_page_tails_the_log(app, client):
    import logging

    for i in range(5):
        logging.getLogger("tflens.test").warning("entry %d", i)
    page = client.get("/logs?limit=2").data.decode()
    assert "entry 4" in page and "entry 3" in page and "entry 2" not in page
    assert "entry 0" in client.get("/logs?level=WARNING&q=entry%200").data.decode()


def test_nav_marks_active_page(client):
    page = client.get("/runs").data.decode()
    assert '<a href="/runs" class="active" aria-current="page">Runs</a>' in page
    assert '<a href="/settings">Settings</a>' in page


# -- helpers (ported) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-07T20:30:00+00:00", "2026-10-07T20:30:00Z"),
        ("2026-10-07T22:30:00+02:00", "2026-10-07T20:30:00Z"),
        ("", None),
        ("garbage", None),
    ],
)
def test_utc_iso(value, expected):
    assert utc_iso(value) == expected


def test_local_time_markup_and_fallback():
    html = str(local_time("2026-10-07T20:30:00+00:00"))
    assert 'datetime="2026-10-07T20:30:00Z"' in html and "2026-10-07 20:30:00 UTC" in html
    assert local_time(None) == ""


def test_favicon_and_index_assets(client, repo_dir):
    resp = client.get("/static/favicon.svg")
    assert resp.status_code == 200 and resp.mimetype == "image/svg+xml"
    assert b"<svg" in resp.data and b"href" not in resp.data
    resp.close()
    app = create_app(store=ModelStore(repo_dir), testing=True)
    html = app.test_client().get("/").data.decode()
    assert 'src="/static/app.js"' in html and 'href="/static/favicon.svg"' in html
    assert json.loads(app.test_client().get("/api/summary").data)["readonly"] is False


def test_architecture_diagram_panel_hooks(repo_dir):
    """The Diagrams tab shows each SVG at natural size in a scrolling viewport, with zoom
    buttons and a full-size link; the served SVG itself is the exported one, unchanged."""
    from tflens.visuals import svg

    store = ModelStore(repo_dir)
    c = create_app(store=store, testing=True).test_client()
    js = c.get("/static/app.js").get_data(as_text=True)
    css = c.get("/static/app.css").get_data(as_text=True)
    assert '["architecture", "Architecture (flows)"' in js and "diagramPanel(name, title)" in js
    # wrapper + viewport classes, zoom via CSS transform, never below 100%
    for hook in (
        'class: "diagram-panel"',
        'class: "diagram"',
        'class: "diagram-sizer"',
        'class: "diagram-zoom"',
        "transform = `scale(${scale})`",
        "Math.max(1, s)",
    ):
        assert hook in js, hook
    for zoom in ("in", "out", "fit"):
        assert f'"data-zoom": "{zoom}"' in js
    # fit-to-width only when the natural width fits the panel
    assert "w <= room ? room / w : 1" in js
    # full-size SVG in a new tab (both the image and the toolbar link)
    assert js.count('href: url, target: "_blank", rel: "noopener"') == 2
    assert 'const url = `api/diagram?name=${name}&format=svg${query || ""}`' in js
    rules = dict(re.findall(r"^#diagrams (\.[\w\- ]+?) \{ (.*?) \}$", css, re.M))
    assert "overflow: auto" in rules[".diagram"]
    assert "max-width: none" in rules[".diagram img"]  # natural size, not shrunk
    assert "transform-origin: 0 0" in rules[".diagram-zoom"]
    assert "max-width: 100%" not in css.split("#diagrams", 1)[1]
    resp = c.get("/api/diagram?name=architecture&format=svg")
    assert resp.mimetype == "image/svg+xml"
    assert resp.get_data(as_text=True) == svg(store.model(), "architecture", store.title)
