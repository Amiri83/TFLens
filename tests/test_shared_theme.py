"""The DadsOps shared theme and the shared environment defaults.

Every page links ``theme/dadsops-theme.css`` (an unchanged copy of the DadsOps master)
before TFLens's own app.css / style.css. ``AWS_PROFILE`` / ``AWS_REGION`` are the default
profile / region of a new account and ``DADSOPS_LOG_DIR`` the default log directory: they
only supply defaults (saved values win) and are never written anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tflens.config import default_paths
from tflens.db import closing
from tflens.web import create_app
from tflens.webapi import STATIC_DIR, Api, ModelStore, static_file

ROOT = Path(__file__).resolve().parents[1]
THEME = STATIC_DIR / "theme" / "dadsops-theme.css"
MASTER = ROOT / "reference" / "dadsops-theme.css"  # staged locally (gitignored)
VERSION_RX = re.compile(
    r"^/\* DadsOps shared theme v\d+ — master copy, tools copy this file unchanged \*/$"
)
THEME_LINK = '<link rel="stylesheet" href="/static/theme/dadsops-theme.css">'
MAIN_TF = 'resource "aws_sqs_queue" "jobs" {\n  name = "jobs"\n}\n'


def _post(client, url, data=None):
    with client.session_transaction() as s:
        token = s["csrf"]
    return client.post(url, data={**(data or {}), "csrf_token": token})


@pytest.fixture
def client(home):
    c = create_app(home, testing=True).test_client()
    c.get("/repos")  # opens the session (CSRF token)
    return c


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo-a"
    root.mkdir()
    (root / "main.tf").write_text(MAIN_TF)
    return root


@pytest.fixture
def aws_config(tmp_path, monkeypatch):
    """A shared config with the placeholder profile ``example`` (no credentials)."""
    cfg = tmp_path / "aws-config"
    cfg.write_text("[profile example]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    return cfg


def _dump(client) -> str:
    db = client.application.extensions["tflens"]["paths"].db_path
    if not db.exists():
        return ""
    with closing(db) as conn:
        return "\n".join(conn.iterdump())


def _master_version() -> str:
    return THEME.read_text(encoding="utf-8").splitlines()[0]


# ---------------------------------------------------------------- 1. the shared theme file


def test_theme_is_an_unchanged_copy_of_the_master():
    first = _master_version()
    assert VERSION_RX.match(first), first
    if MASTER.is_file():  # the staged master copy is not in git
        assert THEME.read_bytes() == MASTER.read_bytes()


def test_every_main_page_links_the_theme_before_tflens_css(client, repo):
    resp = _post(client, "/repos", {"source": "local", "path": str(repo)})
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    run_url = _post(client, f"/repos/{repo_id}/discover").headers["Location"]
    run_id = int(run_url.rstrip("/").rsplit("/", 1)[1])
    pages = [
        "/repos",
        f"/repos/{repo_id}",
        "/runs",
        f"/runs/{run_id}/",
        f"/runs/{run_id}/changes",
        "/accounts",
        "/accounts/new",
        "/settings",
        "/logs",
    ]
    for url in pages:
        resp = client.get(url)
        assert resp.status_code == 200, url
        html = resp.data.decode()
        assert html.count(THEME_LINK) == 1, url
        theme = html.index(THEME_LINK)
        assert theme < html.index('href="/static/app.css"'), url
        if url != f"/runs/{run_id}/":  # the analyzer page does not load style.css
            assert theme < html.index('href="/static/style.css"'), url
    # error pages too
    resp = client.get("/runs/999/")
    assert resp.status_code == 404 and THEME_LINK in resp.data.decode()


def test_theme_file_is_served(client, repo):
    for resp in (
        client.get("/static/theme/dadsops-theme.css"),
        create_app(store=ModelStore(repo), testing=True)
        .test_client()
        .get("/static/theme/dadsops-theme.css"),
    ):
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert body.strip() and body.splitlines()[0] == _master_version()
        assert resp.content_type.startswith("text/css")
    # the standalone API's static files (tflens --dir serve)
    api = static_file("theme/dadsops-theme.css")
    assert api.status == 200 and api.body.decode().splitlines()[0] == _master_version()
    page = Api(ModelStore(repo)).handle("GET", "/").body.decode()
    assert page.index(THEME_LINK) < page.index('href="/static/app.css"')


def test_theme_ships_with_the_package():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = data["tool"]["setuptools"]["package-data"]["tflens"]
    pkg = STATIC_DIR.parent
    assert any(THEME in set(pkg.glob(p)) for p in patterns)


def test_tflens_css_uses_the_theme_variables_the_scripts_read():
    theme = THEME.read_text()
    css = (STATIC_DIR / "app.css").read_text()
    js = (STATIC_DIR / "app.js").read_text() + (STATIC_DIR / "diagram.js").read_text()
    read = set(re.findall(r'(?:themeVar|themeColor|cssVar)\("(--[\w-]+)"', js))
    assert {"--fg", "--bg-panel", "--accent", "--sel-bg", "--sans"} <= read
    for name in read:
        # every variable the Graph canvas reads is defined by the theme or by TFLens
        assert re.search(rf"{name}:", theme) or re.search(rf"{name}:", css), name
    # the shell is the theme's: TFLens no longer restyles the top menu
    for own in (css, (STATIC_DIR / "style.css").read_text()):
        rules = re.sub(r"/\*.*?\*/", "", own, flags=re.S)
        assert ".shell-header" not in rules


# ---------------------------------------------------------------- 2. AWS_PROFILE / AWS_REGION


def _checked_mode(html: str) -> list[str]:
    return re.findall(r'name="auth_mode" value="(\w+)" data-show-group="auth" checked', html)


def test_account_form_defaults_follow_aws_env(client, aws_config, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "example")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    html = client.get("/accounts/new").data.decode()
    assert _checked_mode(html) == ["profile"]  # "Named profile"
    assert '<option value="example" selected>' in html
    assert '<option value="eu-west-1" selected>' in html
    assert '<option value="us-east-1" selected>' not in html
    assert 'from <span class="mono">AWS_PROFILE</span>' in html
    assert 'from <span class="mono">AWS_REGION</span>' in html


def test_account_form_region_only(client, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    html = client.get("/accounts/new").data.decode()
    assert _checked_mode(html) == ["env"]  # no profile set: the mode stays as today
    assert '<option value="ap-southeast-2" selected>' in html
    assert "AWS_PROFILE</span>" not in html and 'from <span class="mono">AWS_REGION</span>' in html


def test_account_form_without_env_is_unchanged(client, aws_config):
    html = client.get("/accounts/new").data.decode()
    assert _checked_mode(html) == ["env"]
    assert '<option value="us-east-1" selected>' in html
    assert '<option value="example" selected>' not in html
    assert "env-origin" not in html and "AWS_REGION</span>" not in html


def test_saved_account_wins_over_env(client, aws_config, monkeypatch):
    resp = _post(
        client,
        "/accounts/new",
        {"display_name": "example", "auth_mode": "env", "region": "us-west-2"},
    )
    assert resp.status_code == 302
    monkeypatch.setenv("AWS_PROFILE", "example")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    html = client.get("/accounts/1/edit").data.decode()
    assert _checked_mode(html) == ["env"]
    assert '<option value="us-west-2" selected>' in html
    assert '<option value="eu-west-1" selected>' not in html and "env-origin" not in html


def test_env_values_are_never_stored(client, aws_config, monkeypatch, tmp_path):
    env_logs = tmp_path / "env-logs"
    monkeypatch.setenv("AWS_PROFILE", "example")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("DADSOPS_LOG_DIR", str(env_logs))
    for url in ("/accounts/new", "/accounts", "/settings"):
        assert client.get(url).status_code == 200
    # saving the settings form as shown (log directory left empty) stores no default
    assert _post(client, "/settings", {"server_form": "1", "log_dir": ""}).status_code == 302
    dump = _dump(client)
    assert "INSERT INTO" in dump  # the settings were written
    for value in ("example", "eu-west-1", str(env_logs)):
        assert value not in dump, value
    with closing(client.application.extensions["tflens"]["paths"].db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        stored = conn.execute("SELECT value FROM settings WHERE key='log_dir'").fetchone()
    assert stored[0] == ""


# ---------------------------------------------------------------- 3. DADSOPS_LOG_DIR


def test_log_dir_follows_dadsops_log_dir(home, tmp_path, monkeypatch):
    env_logs = tmp_path / "env-logs"
    monkeypatch.setenv("DADSOPS_LOG_DIR", str(env_logs))
    paths = default_paths(home)
    assert paths.default_log_dir == env_logs
    assert paths.default_log_dir_source == "DADSOPS_LOG_DIR"
    app = create_app(home, testing=True)
    assert app.extensions["tflens"]["log_dir"] == env_logs
    assert (env_logs / "tflens.log").is_file()
    assert not (home / "logs").exists()
    c = app.test_client()
    html = c.get("/settings").data.decode()
    assert f'placeholder="{env_logs}"' in html
    assert 'Default from <span class="mono">DADSOPS_LOG_DIR</span>' in html
    assert str(env_logs / "tflens.log") in c.get("/logs").data.decode()


def test_log_dir_without_env_is_unchanged(home):
    paths = default_paths(home)
    assert paths.default_log_dir == home.resolve() / "logs"
    assert paths.default_log_dir_source == ""
    app = create_app(home, testing=True)
    assert app.extensions["tflens"]["log_dir"] == home.resolve() / "logs"
    html = app.test_client().get("/settings").data.decode()
    assert "DADSOPS_LOG_DIR" not in html and "env-origin" not in html


def test_saved_log_dir_wins_over_env(client, tmp_path, monkeypatch):
    monkeypatch.setenv("DADSOPS_LOG_DIR", str(tmp_path / "env-logs"))
    saved = tmp_path / "saved-logs"
    resp = _post(client, "/settings", {"server_form": "1", "log_dir": str(saved)})
    assert resp.status_code == 302
    assert client.application.extensions["tflens"]["log_dir"] == saved
    assert (saved / "tflens.log").is_file()
    # and on the next start
    home = client.application.extensions["tflens"]["paths"].home
    assert create_app(home, testing=True).extensions["tflens"]["log_dir"] == saved
