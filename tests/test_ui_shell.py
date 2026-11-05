"""One UI shell, honest stack hypotheses, environment binding: every route renders the
shared base template in the one dark theme; Phase 1 only proposes real crawled directories;
each stack is bound to its own env files and the counts agree everywhere."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens.db import closing
from tflens.envs import default_env, scope_counts, shown_env_names
from tflens.pipeline import (
    CONFIRMED,
    CONTRADICTED,
    Options,
    crawled_dirs,
    discover,
    looks_like_dir,
    stack_candidate,
)
from tflens.web import create_app
from tflens.webapi import STATIC_DIR, Api, ModelStore, environments_view

TEMPLATES = Path(STATIC_DIR).parent / "templates"
SHELL = 'data-shell="tflens"'
MENU = ("/repos", "/runs", "/accounts", "/settings", "/logs")


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


MAIN_TF = 'variable "env" {\n  default = "none"\n}\nresource "aws_sqs_queue" "jobs" {\n  name = "${var.env}-jobs"\n}\n'
BACKEND_TF = 'terraform {\n  backend "s3" {}\n}\n'


@pytest.fixture
def stacks_repo(tmp_path):
    """Two stacks with per-env tfvars / backend files of their own, a third with none."""
    return _write(
        tmp_path / "repo-a",
        {
            "stacks/api/main.tf": MAIN_TF + BACKEND_TF,
            "stacks/api/env/dev.tfvars": 'env = "dev"\n',
            "stacks/api/env/prod.tfvars": 'env = "prod"\n',
            "stacks/worker/main.tf": MAIN_TF + BACKEND_TF,
            "stacks/worker/backend/backend-uat.hcl": 'bucket = "example-state"\nkey = "uat"\n',
            "stacks/worker/backend/backend-prod.hcl": 'bucket = "example-state"\nkey = "prod"\n',
            "stacks/plain/main.tf": MAIN_TF + BACKEND_TF,
        },
    )


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


def _discovered(client, path) -> tuple[int, str]:
    """A repository added and discovered: its id and its run's URL."""
    resp = _post(client, "/repos", {"source": "local", "path": str(path)})
    repo_id = int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])
    return repo_id, _post(client, f"/repos/{repo_id}/discover").headers["Location"]


# ---------------------------------------------------------------- 1. one shell


def _assert_shell(html: str, url: str, *, menu: bool = True) -> None:
    assert html.count(SHELL) == 1 and html.count("<html") == 1, url
    assert html.count('<header class="shell-header">') == 1, url
    assert 'href="/static/app.css"' in html, url  # the one theme, on every page
    if menu:
        assert html.count('<nav class="tabs" aria-label="Main">') == 1, url
        for link in MENU:
            assert f'<a href="{link}"' in html, (url, link)


# Routes that answer with data or a download, never a page.
NOT_PAGES = {
    "static",
    "job_current",
    "job_status",
    "jobs_current",
    "jobs_status",
    "run_api",
    "run_file",
    "overview",
}


def test_every_get_route_renders_the_shared_base_template(client, stacks_repo):
    repo_id, run_url = _discovered(client, stacks_repo)
    run_id = int(run_url.rstrip("/").rsplit("/", 1)[1])
    account = _post(
        client,
        "/accounts/new",
        {"display_name": "example", "auth_mode": "env", "region": "us-east-1"},
    )
    assert account.status_code == 302, account.data
    args = {"repo_id": repo_id, "run_id": run_id, "account_id": 1}
    seen = set()
    for rule in client.application.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in NOT_PAGES:
            continue
        url = rule.build({k: args[k] for k in rule.arguments})[1]
        resp = client.get(url)
        assert resp.status_code == 200, url
        _assert_shell(resp.data.decode(), url)
        seen.add(rule.endpoint)
    # a new page cannot slip past this test unnoticed; these are the ones named by the task
    assert {
        "tfrepos_page",
        "settings_tfrepo",
        "runs_page",
        "run_view",
        "run_changes",
        "accounts_page",
        "account_new",
        "account_edit",
        "settings_page",
        "logs",
    } <= seen


def test_error_pages_use_the_shell(client):
    resp = client.get("/runs/999/")
    assert resp.status_code == 404
    _assert_shell(resp.data.decode(), "/runs/999/")


def test_standalone_analyzer_uses_the_shell(stacks_repo):
    c = create_app(store=ModelStore(stacks_repo), testing=True).test_client()
    for url in ("/", "/index.html"):
        _assert_shell(c.get(url).data.decode(), url, menu=False)


def test_api_index_is_the_same_page(stacks_repo):
    api = Api(ModelStore(stacks_repo))
    assert api.handle("GET", "/").body.decode().count(SHELL) == 1
    assert api.handle("GET", "/static/index.html").status == 404  # no second page


def test_run_tabs_are_a_subnav_under_the_shared_menu(client, stacks_repo):
    _, run_url = _discovered(client, stacks_repo)
    run_id = run_url.rstrip("/").rsplit("/", 1)[1]
    html = client.get(run_url).data.decode()
    assert '<a href="/runs" class="active" aria-current="page">Runs</a>' in html
    header_end, subnav = html.index("</header>"), html.index('<nav class="run-tabs"')
    assert header_end < subnav < html.index("<main>")
    tabs = html[subnav : html.index("</nav>", subnav)]
    for view in ("environments", "roots", "flow", "overview", "graph", "diagrams", "repo"):
        assert f'data-view="{view}"' in tabs, view
    assert 'data-view="discovery"' not in tabs and f'href="/runs/{run_id}/changes"' in tabs
    assert "data-view" not in html[: html.index("</header>")]  # not in the top menu
    # on Changes the same tabs link back to the analyzer's views
    changes = client.get(f"/runs/{run_id}/changes").data.decode()
    assert changes.index("</header>") < changes.index('<nav class="run-tabs"')
    assert f'href="/runs/{run_id}/?view=graph"' in changes
    js = (STATIC_DIR / "app.js").read_text()
    assert 'new URLSearchParams(location.search).get("view")' in js
    assert ".tab[data-view]" in js  # the Changes link is not a view button


def test_the_light_theme_is_gone(client):
    css = (STATIC_DIR / "app.css").read_text()
    style = (STATIC_DIR / "style.css").read_text()
    js = (STATIC_DIR / "app.js").read_text() + (STATIC_DIR / "diagram.js").read_text()
    assert "color-scheme: dark" in css and "color-scheme: light" not in css
    assert css.count(":root {") == 1
    for text in (css, style, js):
        assert "prefers-color-scheme" not in text
        assert "data-theme" not in text and "dataset.theme" not in text
        assert "theme-toggle" not in text and "tflens-theme" not in text
        assert not re.search(r"\blight\b", text, re.I)
    # every colour of the form pages comes from the shared theme
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", style)
    for name in ("--fg", "--bg-panel", "--accent"):
        assert re.search(rf"{name}: #", css) and not re.search(rf"{name}: #", style)
    for url in ("/repos", "/settings"):
        assert "theme-toggle" not in client.get(url).data.decode()
    assert not list(TEMPLATES.glob("*light*"))
    assert not (STATIC_DIR / "index.html").exists()
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text()
        if template.name != "base.html" and not template.name.startswith("_"):
            assert text.startswith('{% extends "base.html" %}'), template.name
        assert "<!doctype" not in text.lower() or template.name == "base.html", template.name


def test_every_element_app_js_looks_up_is_on_the_page(stacks_repo):
    html = Api(ModelStore(stacks_repo)).handle("GET", "/").body.decode()
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    js = (STATIC_DIR / "app.js").read_text() + (STATIC_DIR / "discover.js").read_text()
    # created by the scripts themselves, or optional (checked for null where they are used)
    dynamic = {"graph-note", "discover-aws-why"}
    missing = set(re.findall(r'getElementById\("([\w-]+)"\)', js)) - ids - dynamic
    assert not {m for m in missing if f'id: "{m}"' not in js}


def test_app_js_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for name in ("app.js", "diagram.js", "discover.js"):
        subprocess.run([node, "--check", str(STATIC_DIR / name)], check=True, capture_output=True)


# ---------------------------------------------------------------- 2. stack hypotheses


@pytest.mark.parametrize(
    "junk",
    [
        "42",
        "3.14",
        "1.2.3",
        "0644",
        "755",
        "123456789012",
        "accounts/123456789012",
        "deadbeef",
        "abc1234",
        "0x1f",
        "3f2a9c1e7b6d4f08a1c2e3d4f5a6b7c8d9e0f1a2",
        "123e4567-e89b-12d3-a456-426614174000",
        "192.0.2.1",
        "192.0.2.0/24",
        "10.0.0.0/8",
        "rwxr-xr-x",
        "drwxr-xr-x",
        ".github",
        "stacks/.terraform",
        ".env",
        "README.md",
        "stacks/main.tf",
        "archive.tar.gz",
        "u+x",
        "host:8080",
        "",
    ],
)
def test_junk_is_not_a_directory(junk):
    assert not looks_like_dir(junk)
    assert not stack_candidate(junk, {junk})  # even when a directory is called that


@pytest.mark.parametrize(
    "good", ["network", "stacks/api", "live/us-east-1/app", "app_v2", "_shared", "facade", "v1"]
)
def test_real_paths_are_directories(good):
    assert looks_like_dir(good)
    assert stack_candidate(good, {good})
    assert not stack_candidate(good, set())  # the crawl did not find it


def test_crawled_dirs_are_the_ancestors_of_files():
    assert crawled_dirs(["a/b/c.tf", "a/d.md", "e.md"]) == {"a", "a/b"}


JUNK_LINES = """
Deploy the stack from `stacks/api`, then `stacks`:

```
cd 123456789012
cd 0644
cd deadbeef
cd 192.0.2.1
cd 192.0.2.0/24
cd .github
cd README.md
cd missing/dir
cd stacks/worker
```

The state of the `42` stack, the `abc1234` root and the `notes` layer. Apply `stacks/main.tf`.
"""


def _junk_repo(root: Path) -> Path:
    # directories named like values, really there, to prove the shape check alone rejects them
    return _write(
        root,
        {
            "stacks/api/main.tf": MAIN_TF + BACKEND_TF,
            "stacks/worker/main.tf": MAIN_TF + BACKEND_TF,
            "123456789012/note.txt": "example\n",
            "deadbeef/note.txt": "example\n",
            "notes/note.txt": "example\n",
            "README.md": JUNK_LINES,
            ".github/workflows/deploy.yml": (
                "jobs:\n  a:\n    steps:\n      - run: terraform apply\n"
                "        working-directory: stacks/api\n"
                "      - run: cd 2026 && terraform plan\n"
                "        working-directory: 10.0.0.1\n"
            ),
        },
    )


def test_phase_one_proposes_only_real_directories(tmp_path):
    model = discover(_junk_repo(tmp_path / "repo-a"), Options(git=False)).model
    proposed = {h["name"] for h in model["pipeline"]["hints"] if h["kind"] == "stack"}
    assert proposed == {"stacks", "stacks/api", "stacks/worker", "notes"}
    v = model["pipeline"]["validation"]
    by = {s["name"]: s for s in v["stacks"]}
    assert set(by) == proposed and by["stacks"]["role"] == "directory"
    assert by["stacks/api"]["status"] == CONFIRMED and by["stacks/api"]["hint_mentions"] >= 2
    # a real directory without a root: what contradicted now means, honestly
    assert by["notes"]["status"] == CONTRADICTED
    assert "holds no root module" in by["notes"]["reason"]
    text = json.dumps(v["mismatches"])
    for junk in ("123456789012", "deadbeef", "192.0.2", "missing/dir", "README.md", "2026"):
        assert junk not in {s["name"] for s in v["stacks"]} and f'"name": "{junk}"' not in text


def test_git_log_never_proposes_a_stack(tmp_path):
    from tflens.loader import load_repo
    from tflens.pipeline import hypothesize

    repo = load_repo(_write(tmp_path / "repo-a", {"stacks/api/main.tf": MAIN_TF}))
    crawled = {"git": {"commits": [{"short": "abc1234", "subject": "cd stacks/api"}]}}
    assert [h for h in hypothesize(repo, crawled) if h["kind"] == "stack"] == []


# ---------------------------------------------------------------- 3. environment binding


def test_each_stack_binds_its_own_env_files_and_defaults_to_the_first(client, stacks_repo):
    _, run_url = _discovered(client, stacks_repo)
    view = client.get(run_url + "api/environments").get_json()
    by = {s["id"]: s for s in view["stacks"]}
    # ids are qualified only where two stacks share a name (prod); a stack with only
    # backend files binds them too, and defaults to its first
    assert set(by["stacks/api"]["environments"]) == {"dev", "stacks/api:prod"}
    assert by["stacks/api"]["selected"] == "dev" and by["stacks/api"]["default"] is True
    assert by["stacks/worker"]["selected"] == "stacks/worker:prod"
    assert set(by["stacks/worker"]["environments"]) == {"stacks/worker:prod", "uat"}
    assert by["stacks/plain"]["environments"] == [] and by["stacks/plain"]["selected"] is None
    assert "no candidates" in by["stacks/plain"]["no_env_reason"]
    # nothing bound to an env file is left on the variable defaults
    overview = client.get(run_url + "api/overview").get_json()
    text = json.dumps(overview)
    assert "environment shown none" not in text and "environment shown dev (default)" in text
    # the stack's own tfvars were applied: the name resolves
    names = {i["name"] for i in view["current"]["inventory"]["sqs"]["items"]}
    assert "dev-jobs" in names and "none-jobs" in names  # dev for api, defaults for plain


def test_the_default_is_the_first_confirmed_environment():
    ghost = {"name": "dev", "status": "hint-only"}
    uat, prod = {"name": "uat", "status": "confirmed"}, {"name": "prod", "status": "confirmed"}
    assert default_env([ghost, uat, prod]) is uat
    assert default_env([ghost]) is None
    # before validation: real files are what makes one confirmed
    empty = {"name": "dev", "var_files": [], "backend_hcl": [], "evidence": []}
    real = {"name": "qa", "var_files": ["env/qa.tfvars"], "backend_hcl": [], "evidence": []}
    assert default_env([empty, real]) is real


def test_runs_table_fills_the_environment_column(client, stacks_repo):
    repo_id, run_url = _discovered(client, stacks_repo)
    run_id = int(run_url.rstrip("/").rsplit("/", 1)[1])
    db = client.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        row = conn.execute("SELECT env, summary FROM runs WHERE id=?", (run_id,)).fetchone()
    # retention and the previous run stay keyed on the environment asked for: none
    assert row["env"] == ""
    assert json.loads(row["summary"])["environments_shown"] == ["dev", "prod"]
    for url in ("/runs", f"/repos/{repo_id}"):
        page = client.get(url).data.decode()
        assert '<td class="mono run-env">dev, prod</td>' in page, url


def test_runs_table_without_any_environment_shows_a_dash(client, tmp_path):
    repo = _write(tmp_path / "repo-a", {"main.tf": MAIN_TF + BACKEND_TF})
    _discovered(client, repo)
    assert '<td class="mono run-env">—</td>' in client.get("/runs").data.decode()


def test_older_runs_without_the_field_still_render(client, stacks_repo):
    _, run_url = _discovered(client, stacks_repo)
    run_id = int(run_url.rstrip("/").rsplit("/", 1)[1])
    db = client.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        conn.execute("UPDATE runs SET summary='{}' WHERE id=?", (run_id,))
        conn.commit()
    assert '<td class="mono run-env">—</td>' in client.get("/runs").data.decode()


def test_shown_env_names_are_unique_and_in_promotion_order():
    model = {
        "stacks": [
            {"id": "a", "selected": "a:prod"},
            {"id": "b", "selected": "b:prod"},
            {"id": "c", "selected": "dev"},
            {"id": "d", "selected": None},
        ]
    }
    assert shown_env_names(model) == ["dev", "prod"]
    assert shown_env_names({"environment": {"name": "uat"}, "stacks": []}) == ["uat"]
    assert shown_env_names({}) == []


# ---------------------------------------------------------------- 3. the counts agree


def _agreement(model: dict) -> dict:
    """The stack / environment numbers of every section, from what each one is built on."""
    view = environments_view(model)
    v = model["pipeline"]["validation"]
    return {
        "counts": scope_counts(model),
        "header_and_tables": view["counts"],
        "validation_envs": v["counts"]["envs"][CONFIRMED],
        "validation_stacks": v["counts"]["stacks"][CONFIRMED],
        "stack_rows": [s for s in v["stacks"] if s.get("role") != "directory"],
        "stacks_listed": [s for s in view["stacks"] if not s.get("library")],
        "env_rows": view["environments"],
        "columns": [c for c in view["columns"] if not c.get("no_environment")],
    }


def _assert_agree(a: dict, stacks: int, envs: int, rows: int) -> None:
    assert a["counts"] == a["header_and_tables"]
    assert (a["counts"]["stacks"], a["counts"]["environments"]) == (stacks, envs)
    assert a["counts"]["stack_environments"] == rows == len(a["env_rows"])
    assert a["validation_envs"] == len(a["columns"]) == envs
    assert a["validation_stacks"] == len(a["stack_rows"]) == len(a["stacks_listed"]) == stacks


def test_counts_agree_per_env_files(stacks_repo):
    model = discover(stacks_repo, Options(git=False, auto_env=True)).model
    # dev | uat | prod; prod is in two stacks: 3 names, 4 rows
    _assert_agree(_agreement(model), stacks=3, envs=3, rows=4)
    v = model["pipeline"]["validation"]["envs"]
    prod = next(e for e in v if e["name"] == "prod")
    assert prod["stacks"] == ["stacks/api", "stacks/worker"]


def test_counts_agree_for_environment_directories(tmp_path):
    repo = _write(
        tmp_path / "repo-a",
        {
            "envs/dev/main.tf": MAIN_TF + BACKEND_TF,
            "envs/prod/main.tf": MAIN_TF + BACKEND_TF,
            "docs/deploy.md": "Each stack is applied from `envs/dev`, `envs/prod` and `envs`.\n",
        },
    )
    model = discover(repo, Options(git=False, auto_env=True)).model
    a = _agreement(model)
    # one stack (envs/*) with two roots, however often the docs name its directories
    _assert_agree(a, stacks=1, envs=2, rows=2)
    assert [s["name"] for s in a["stack_rows"]] == ["envs/*"]
    v = {s["name"]: s for s in model["pipeline"]["validation"]["stacks"]}
    assert v["envs/*"]["roots"] == ["envs/dev", "envs/prod"]
    assert v["envs/*"]["hint_mentions"] >= 2
    assert v["envs"]["role"] == "directory"
    dev = next(e for e in model["pipeline"]["validation"]["envs"] if e["name"] == "dev")
    assert dev["stacks"] == ["envs/*"]


def test_reusable_modules_are_not_counted_as_stacks(tmp_path):
    repo = _write(
        tmp_path / "repo-a",
        {
            "stacks/api/main.tf": MAIN_TF + BACKEND_TF,
            "stacks/api/env/dev.tfvars": 'env = "dev"\n',
            "lib/widget/main.tf": 'variable "name" {}\noutput "name" {\n  value = var.name\n}\n',
        },
    )
    model = discover(repo, Options(git=False, auto_env=True)).model
    a = _agreement(model)
    _assert_agree(a, stacks=1, envs=1, rows=1)
    assert a["counts"]["library_stacks"] == 1
    assert {s["name"] for s in a["stack_rows"]} == {"stacks/api"}


def test_counts_agree_without_any_environment(tmp_path):
    repo = _write(tmp_path / "repo-a", {"main.tf": MAIN_TF + BACKEND_TF})
    _assert_agree(_agreement(discover(repo, Options(git=False, auto_env=True)).model), 1, 0, 0)


def test_overview_counts_environment_names_like_the_header(stacks_repo):
    model = discover(stacks_repo, Options(git=False, auto_env=True)).model
    api = Api(ModelStore(stacks_repo))
    ov = json.loads(api.handle("GET", "/api/overview").body)
    assert "3 environments: " in json.dumps(ov) and scope_counts(model)["environments"] == 3


def test_header_and_sections_read_the_same_counts():
    js = (STATIC_DIR / "app.js").read_text()
    bar = js.split("function renderSummaryBar(summary, envs) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const { environments: envCount, stacks } = envs.counts;" in bar
    view = js.split("function renderEnvironments(envs) {", 1)[1].split("\n  }\n", 1)[0]
    assert "`Stacks (${counts.stacks})`" in view
    assert "`Detected environments (${counts.environments})`" in view


def test_fixture_with_many_mentions_keeps_its_counts(tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "docs_noise", repo)
    model = discover(repo, Options(git=False, auto_env=True)).model
    _assert_agree(_agreement(model), stacks=1, envs=4, rows=4)
