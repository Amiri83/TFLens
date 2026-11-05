"""The UI shows confirmed facts only (uncertain items go to the debug log), and one New run
button: pick a repository, follow the progress screen, land on the new run; a run happens
automatically when the TFLens code changed, without a sync."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens import tfrepo
from tflens.db import closing
from tflens.logging_setup import LOG_FILE
from tflens.pipeline import Options, discover, log_uncertain
from tflens.web import create_app
from tflens.webapi import STATIC_DIR, Api, ModelStore

TEMPLATES = Path(STATIC_DIR).parent / "templates"
# docs_noise: docs and CI name these environments; no real file defines them
UNCONFIRMED_ENVS = ("qa", "staging", "sandbox")
UNCERTAIN = re.compile(
    r"hint-only|hint_|\bhints?\b|contradicted|hypothes|mentions?\b|docs[- ]vs[- ]code|"
    r"Environments \(debug\)|env_debug|Purpose \(guess\)|handoff",
    re.I,
)


@pytest.fixture
def noise(tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "docs_noise", repo)
    (repo / "stacks" / "ghost").mkdir(parents=True)
    (repo / "stacks" / "ghost" / "notes.txt").write_text("example\n")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "stacks.md").write_text("Deploy the `stacks/ghost` stack first.\n")
    return repo


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


def _db(client):
    return client.application.extensions["tflens"]["paths"].db_path


def _jobs(client):
    return client.application.extensions["tflens"]["jobs"]


def _run_count(client) -> int:
    with closing(_db(client)) as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


# ---------------------------------------------------------------- 1. confirmed facts only


def _assert_clean(text: str, where: str) -> None:
    found = UNCERTAIN.search(text)
    assert found is None, (where, text[max(found.start() - 60, 0) : found.end() + 60])


def _pages(client, repo_id: int, run_id: int) -> list[str]:
    return [
        "/repos",
        f"/repos/{repo_id}",
        "/runs",
        f"/runs?repo={repo_id}",
        f"/runs/{run_id}/",
        f"/runs/{run_id}/changes",
        "/logs",
        "/logs?level=DEBUG",
        "/logs?q=qa",
        "/settings",
        "/accounts",
    ]


def test_no_page_renders_uncertain_content(client, noise):
    repo_id = _add(client, noise)
    run_url = _post(client, "/runs/new", {"repo_id": repo_id}).headers["Location"]
    run_id = int(run_url.strip("/").rsplit("/", 1)[1])
    for url in _pages(client, repo_id, run_id):
        resp = client.get(url)
        assert resp.status_code == 200, url
        _assert_clean(resp.data.decode(), url)


def _view_data(client, run_url: str) -> dict[str, str]:
    """Every JSON document the analyzer page renders from."""
    out = {}
    for name in (
        "summary",
        "overview",
        "flow",
        "shapes",
        "environments",
        "stack-cards",
        "env-matrix",
    ):
        resp = client.get(f"{run_url}api/{name}")
        assert resp.status_code == 200, name
        out[name] = resp.data.decode()
    shapes = json.loads(out["shapes"])
    for root in shapes["roots"]:
        for view in root["views"]:
            query = {"root": root["dir"], "view": view["view"]}
            resp = client.get(f"{run_url}api/root-view", query_string=query)
            assert resp.status_code == 200, query
            out[f"root-view {query}"] = resp.data.decode()
    return out


def test_the_analyzer_data_has_no_uncertain_content(client, noise):
    repo_id = _add(client, noise)
    run_url = _post(client, "/runs/new", {"repo_id": repo_id}).headers["Location"]
    for name, text in _view_data(client, run_url).items():
        _assert_clean(text, name)
        for env in UNCONFIRMED_ENVS:
            assert not re.search(rf'"{env}"|\b{env}\.tfvars', text), (name, env)
    # no line of a doc or CI file, and nothing built on them
    overview = json.loads(client.get(f"{run_url}api/overview").data)
    assert overview["docs"] == overview["docs_diff"] == overview["ci"] == []
    assert overview["purpose"] == [] and overview["history"] is None
    flow = json.loads(client.get(f"{run_url}api/flow").data)
    assert not [ln for ln in flow["start_here"] if ln["text"].startswith("CI: ")]
    envs = json.loads(client.get(f"{run_url}api/environments").data)
    assert sorted(r["id"] for r in envs["environments"]) == ["dev", "intg", "prod", "uat"]
    assert envs["counts"]["environments"] == 4
    for row in envs["environments"]:
        assert "var_file_cli" not in row
        assert not [w for w in row["wiring"] if w.startswith("CI")]
    summary = json.loads(client.get(f"{run_url}api/summary").data)
    _assert_clean(json.dumps(summary), "summary")


def test_the_raw_model_api_has_no_uncertain_content(client, noise):
    repo_id = _add(client, noise)
    run_url = _post(client, "/runs/new", {"repo_id": repo_id}).headers["Location"]
    model = json.loads(client.get(f"{run_url}api/model").data)
    assert not {"pipeline", "crawl", "docs_diff"} & set(model)
    assert "env_files" not in model["structure"]


def test_discovery_and_handoff_are_gone(client, noise):
    repo_id = _add(client, noise)
    run_url = _post(client, "/runs/new", {"repo_id": repo_id}).headers["Location"]
    run_id = run_url.strip("/").rsplit("/", 1)[1]
    assert client.get(f"{run_url}api/discovery").status_code == 404
    assert client.get(f"{run_url}api/handoff").status_code == 404
    assert client.get(f"/runs/{run_id}/files/handoff.md").status_code == 404
    page = client.get(run_url).data.decode()
    assert 'data-view="discovery"' not in page and "view-discovery" not in page
    assert 'id="discovery"' not in page


def test_static_files_and_templates_do_not_carry_uncertain_content():
    forbidden = re.compile(
        r"hint-only|hint_|hintSources|contradicted|hypothes|envFilesDebug|renderDiscovery|"
        r"api/discovery|api/handoff|handoff\.md|env_debug|Environments \(debug\)|Purpose \(guess\)|"
        r"ov\.docs|ov\.ci|ov\.history|var_file_cli|c\.purpose",
    )
    files = [STATIC_DIR / n for n in ("app.js", "app.css", "style.css", "discover.js")]
    files += sorted(TEMPLATES.glob("*.html"))
    for path in files:
        found = forbidden.search(path.read_text())
        assert found is None, (path.name, found and found.group())


def test_a_job_shows_no_uncertain_progress(client, noise):
    repo_id = _add(client, noise)
    job = _post(client, "/runs/new", {"repo_id": repo_id, "background": "1"}).get_json()["job"]
    final = _jobs(client).wait(job["id"], 60)
    assert final["state"] == "done", final
    _assert_clean(json.dumps(final), "job status")


def test_an_environment_no_longer_found_is_not_listed_or_counted(client, tmp_path):
    repo = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "docs_noise", repo)
    repo_id = _add(client, repo)
    _post(client, "/runs/new", {"repo_id": repo_id})
    (repo / "envs" / "uat.tfvars").unlink()
    _post(client, "/runs/new", {"repo_id": repo_id})
    with closing(_db(client)) as conn:
        repos = tfrepo.list_repos(conn)
        rows = {e["env"]: e["present"] for e in tfrepo.list_envs(conn, repo_id)}
    assert rows["uat"] == 0 and repos[0]["envs"] == sum(rows.values())
    assert "envs/uat.tfvars" not in client.get(f"/repos/{repo_id}").data.decode()


def test_uncertain_items_go_to_the_log_file_at_debug_level(client, noise):
    repo_id = _add(client, noise)
    _post(client, "/runs/new", {"repo_id": repo_id})
    log_dir = client.application.extensions["tflens"]["log_dir"]
    lines = (log_dir / LOG_FILE).read_text().splitlines()
    uncertain = [ln for ln in lines if " tflens.pipeline: " in ln]
    assert uncertain and all(" DEBUG " in ln for ln in uncertain)
    text = "\n".join(uncertain)
    assert "contradicted: env qa:" in text and "contradicted: env staging:" in text
    assert "hint-only: env sandbox:" in text
    assert "hint: env dev:" in text
    # the Logs page starts at INFO, even when asked for more
    for url in ("/logs", "/logs?level=DEBUG", "/logs?q=sandbox", "/logs?level=DEBUG&q=sandbox"):
        page = client.get(url).data.decode()
        assert "hint-only: env" not in page and "DEBUG</td>" not in page, url
        assert '<option value="DEBUG"' not in page


def test_uncertain_lines_carry_no_doc_text(noise, caplog):
    (noise / "docs" / "stacks.md").write_text("The sandbox holds example-marker-text.\n")
    model = discover(noise, Options(git=False)).model
    pipe = model["pipeline"]
    with caplog.at_level(logging.DEBUG, logger="tflens.pipeline"):
        log_uncertain(pipe["hints"], pipe["validation"])
    assert "hint: env sandbox" in caplog.text and "example-marker-text" not in caplog.text


# ---------------------------------------------------------------- 2. the New run flow


def test_one_new_run_button_on_the_runs_page_and_on_each_repo_row(client, noise, tmp_path):
    other = tmp_path / "repo-b"
    shutil.copytree(FIXTURES / "docs_noise", other)
    first, second = _add(client, noise), _add(client, other)
    runs = client.get("/runs").data.decode()
    assert runs.count(">New run</button>") == 1
    assert 'action="/runs/new"' in runs and "data-discover" in runs
    for repo_id in (first, second):
        assert f'<option value="{repo_id}"' in runs
    # the page's filter picks the repository
    assert re.search(
        rf'<option value="{second}" selected>', client.get(f"/runs?repo={second}").data.decode()
    )
    repos = client.get("/repos").data.decode()
    assert repos.count(">New run</button>") == 2
    for repo_id in (first, second):
        assert f'name="repo_id" value="{repo_id}"' in repos
    for page in (runs, repos):
        for part in ('id="discover-hide"', 'id="discover-cancel"', 'id="discover-phases"'):
            assert part in page
        assert 'data-current-url="/jobs/current"' in page
        assert 'data-status-url="/jobs/__ID__"' in page
        assert 'data-cancel-url="/jobs/__ID__/cancel"' in page


def test_runs_page_without_a_repository(client):
    page = client.get("/runs").data.decode()
    assert ">New run</button>" in page and "Add a repository" in page
    assert re.search(r'<button type="submit" class="primary" disabled>New run', page)
    resp = _post(client, "/runs/new", {"background": "1"})
    assert resp.status_code == 400 and "Pick a repository" in resp.get_json()["error"]
    assert _post(client, "/runs/new", {}).headers["Location"].endswith("/runs")
    assert _post(client, "/runs/new", {"repo_id": "999"}).status_code == 404


def test_new_run_creates_a_run_and_redirects(client, noise):
    repo_id = _add(client, noise)
    resp = _post(client, "/runs/new", {"repo_id": repo_id})
    assert resp.status_code == 302 and re.fullmatch(r"/runs/\d+/", resp.headers["Location"])
    assert _run_count(client) == 1
    run_url = resp.headers["Location"]
    assert client.get(run_url).status_code == 200
    run_id = int(run_url.strip("/").rsplit("/", 1)[1])
    assert f'href="/runs/{run_id}/"' in client.get("/runs").data.decode()


def test_new_run_in_the_background_ends_on_the_new_run(client, noise):
    repo_id = _add(client, noise)
    started = _post(client, "/runs/new", {"repo_id": repo_id, "background": "1"}).get_json()
    assert started["ok"] is True and started["job"]["auto"] is False
    job_id = started["job"]["id"]
    final = _jobs(client).wait(job_id, 60)
    assert final["state"] == "done", final
    status = client.get(f"/jobs/{job_id}").get_json()["job"]
    assert status["result"]["url"] == f"/runs/{final['result']['run_id']}/"
    assert client.get(status["result"]["url"]).status_code == 200
    assert client.get("/jobs/current").get_json()["job"] is None
    assert client.get("/jobs/nope").status_code == 404


def _hold_validate(monkeypatch):
    import tflens.pipeline as pipeline

    entered, release = threading.Event(), threading.Event()
    real = pipeline.validate

    def slow(model, hints):
        entered.set()
        release.wait(30)
        return real(model, hints)

    monkeypatch.setattr(pipeline, "validate", slow)
    return entered, release


def test_new_run_can_be_followed_and_cancelled_from_any_page(client, noise, monkeypatch):
    entered, release = _hold_validate(monkeypatch)
    repo_id = _add(client, noise)
    job = _post(client, "/runs/new", {"repo_id": repo_id, "background": "1"}).get_json()["job"]
    assert entered.wait(30)
    assert client.get("/jobs/current").get_json()["job"]["id"] == job["id"]
    busy = _post(client, "/runs/new", {"repo_id": repo_id, "background": "1"})
    assert busy.status_code == 409 and busy.get_json()["job"]["id"] == job["id"]
    cancel = _post(client, f"/jobs/{job['id']}/cancel").get_json()
    assert cancel["ok"] is True and cancel["job"]["cancel_requested"]
    release.set()
    assert _jobs(client).wait(job["id"], 30)["state"] == "cancelled"
    assert _run_count(client) == 0


def test_first_run_of_a_git_repository_needs_no_sync(tmp_path, home):
    src = tmp_path / "src-repo"
    shutil.copytree(FIXTURES / "docs_noise", src)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run([*git, "add", "."], cwd=src, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=src, check=True)
    app = create_app(home, testing=True, git_protocols=("file",))
    client = app.test_client()
    client.get("/repos")
    resp = _post(
        client,
        "/repos",
        {"source": "git", "url": src.as_uri(), "git_auth": "none", "name": "repo-b"},
    )
    repo_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    assert "the first run clones it" in client.get(f"/repos/{repo_id}").data.decode()
    run = _post(client, "/runs/new", {"repo_id": repo_id})
    assert re.fullmatch(r"/runs/\d+/", run.headers["Location"]), client.get("/runs").data.decode()
    db = app.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        row = conn.execute("SELECT commit_sha, status FROM runs").fetchone()
    assert len(row["commit_sha"]) == 40 and row["status"] == "ok"
    # no new commits: the next run does not need a sync either, and clones nothing
    clone = app.extensions["tflens"]["paths"].repos_dir / str(repo_id)
    marker = clone / "marker.txt"
    marker.write_text("example\n")
    assert re.fullmatch(
        r"/runs/\d+/", _post(client, "/runs/new", {"repo_id": repo_id}).headers["Location"]
    )
    assert marker.exists()


def test_a_failed_first_clone_is_recorded_and_reported(tmp_path, home):
    def fail(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "fatal: example failure")

    app = create_app(home, testing=True, git_runner=fail)
    client = app.test_client()
    client.get("/repos")
    resp = _post(
        client,
        "/repos",
        {"source": "git", "url": "https://git.example.com/org/repo-b.git", "git_auth": "none"},
    )
    repo_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    resp = _post(client, "/runs/new", {"repo_id": repo_id})
    assert resp.headers["Location"].endswith("/runs")
    page = client.get("/runs").data.decode()
    assert "clone failed: fatal: example failure" in page and 'class="badge err">failed' in page


# ---------------------------------------------------------------- 3. a new TFLens version


def test_the_code_version_is_the_version_and_a_hash_of_the_sources():
    version = tfrepo.code_version()
    assert re.fullmatch(r"[\w.]+\+[0-9a-f]{16}", version)
    assert version == tfrepo.code_version()


def _restart(home):
    """The same data directory, in a new server process."""
    app = create_app(home, testing=True)
    c = app.test_client()
    c.application = app
    return c


def test_each_run_records_the_code_version(client, noise):
    repo_id = _add(client, noise)
    _post(client, "/runs/new", {"repo_id": repo_id})
    with closing(_db(client)) as conn:
        assert conn.execute("SELECT code_version FROM runs").fetchone()[0] == tfrepo.code_version()
        assert tfrepo.stale_repos(conn) == []


def test_new_code_runs_every_repository_again_without_a_sync(client, noise, tmp_path, home):
    other = tmp_path / "repo-b"
    shutil.copytree(FIXTURES / "docs_noise", other)
    never = tmp_path / "repo-c"
    shutil.copytree(FIXTURES / "docs_noise", never)
    first, second = _add(client, noise), _add(client, other)
    _add(client, never)  # never run: its first run is asked for
    _post(client, "/runs/new", {"repo_id": first})
    _post(client, "/runs/new", {"repo_id": second})
    with closing(_db(client)) as conn:
        conn.execute("UPDATE runs SET code_version='0.0.0+example'")
        synced = conn.execute(
            "SELECT COUNT(*) FROM tf_repos WHERE synced_at IS NOT NULL"
        ).fetchone()
        conn.commit()
    c = _restart(home)
    assert c.get("/runs").status_code == 200  # the page is not held up by the runs
    jobs = c.application.extensions["tflens"]["jobs"]
    job = c.get("/jobs/current").get_json()["job"]
    assert job["auto"] is True
    for key in (f"repo:{first}", f"repo:{second}"):
        current = jobs.current(key)
        assert current["auto"] is True
        assert jobs.wait(current["id"], 60)["state"] == "done"
        assert jobs.current(key)["result"]["run_id"]
    db = c.application.extensions["tflens"]["paths"].db_path
    with closing(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4
        assert tfrepo.stale_repos(conn) == []
        assert (
            conn.execute("SELECT COUNT(*) FROM tf_repos WHERE synced_at IS NOT NULL").fetchone()[0]
            == synced[0]
        )
    # only once per start, and the next start has nothing to do
    c.get("/runs")
    assert c.get("/jobs/current").get_json()["job"] is None
    c2 = _restart(home)
    c2.get("/runs")
    assert c2.get("/jobs/current").get_json()["job"] is None


def test_new_code_does_not_touch_the_same_code_or_a_missing_directory(
    client, noise, home, tmp_path
):
    repo_id = _add(client, noise)
    _post(client, "/runs/new", {"repo_id": repo_id})
    c = _restart(home)
    c.get("/runs")
    assert c.get("/jobs/current").get_json()["job"] is None
    with closing(_db(client)) as conn:
        conn.execute("UPDATE runs SET code_version=''")  # a run from before versions were kept
        conn.commit()
    moved = tmp_path / "moved"
    noise.rename(moved)
    c = _restart(home)
    assert c.get("/runs").status_code == 200
    assert c.get("/jobs/current").get_json()["job"] is None


def test_an_automatic_run_can_be_cancelled(client, noise, home, monkeypatch):
    repo_id = _add(client, noise)
    _post(client, "/runs/new", {"repo_id": repo_id})
    with closing(_db(client)) as conn:
        conn.execute("UPDATE runs SET code_version='0.0.0+example'")
        conn.commit()
    entered, release = _hold_validate(monkeypatch)
    c = _restart(home)
    c.get("/runs")
    assert entered.wait(30)
    job = c.get("/jobs/current").get_json()["job"]
    assert job["auto"] is True
    with c.session_transaction() as s:
        token = s["csrf"]
    assert c.post(f"/jobs/{job['id']}/cancel", data={"csrf_token": token}).get_json()["ok"]
    release.set()
    jobs = c.application.extensions["tflens"]["jobs"]
    assert jobs.wait(job["id"], 30)["state"] == "cancelled"


# ---------------------------------------------------------------- 4. the progress screen


DRIVER = r"""
const fs = require("fs");
const vm = require("vm");
function node(id) {
  const n = { id, hidden: id !== "discover-box", dataset: {}, children: [], textContent: "", disabled: false,
    className: "", listeners: {}, scrollTop: 0, clientHeight: 0, scrollHeight: 0,
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    replaceChildren(...c) { this.children = c; }, append(...c) { this.children.push(...c); },
    querySelectorAll() { return []; } };
  return n;
}
const nodes = {};
const byId = (id) => (nodes[id] = nodes[id] || node(id));
const modal = byId("discover-modal");
Object.assign(modal.dataset, { currentUrl: "/jobs/current", statusUrl: "/jobs/__ID__", cancelUrl: "/jobs/__ID__/cancel", csrf: "x" });
const scenario = JSON.parse(process.argv[3]);
const location = { href: "", pathname: scenario.pathname };
const statusQueue = scenario.statuses.slice();
const sandbox = {
  console,
  URLSearchParams,
  FormData: class { entries() { return []; } },
  window: { get location() { return location; } },
  location,
  sessionStorage: { store: {}, getItem(k) { return this.store[k] || null; }, setItem(k, v) { this.store[k] = v; }, removeItem(k) { delete this.store[k]; } },
  setTimeout: (f) => { pending.push(f); return pending.length; },
  clearTimeout: () => {},
  document: {
    getElementById: byId,
    createElement: () => node("el"),
    addEventListener() {},
    querySelectorAll: () => [],
  },
  fetch: async (url) => ({ ok: true, status: 200,
    json: async () => url === "/jobs/current" ? { job: scenario.current } : { job: statusQueue.shift() } }),
};
const pending = [];
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), sandbox);
const settle = () => new Promise((r) => setImmediate(r));
(async () => {
  await settle();
  if (scenario.hide) nodes["discover-hide"].listeners.click[0]();
  while (pending.length) { pending.shift()(); await settle(); }
  const clickBadge = scenario.clickBadge && !nodes["discover-badge"].hidden;
  const before = { href: location.href, modalHidden: modal.hidden, badgeHidden: nodes["discover-badge"].hidden, badge: nodes["discover-badge"].textContent };
  if (clickBadge) nodes["discover-badge"].listeners.click[0]();
  console.log(JSON.stringify({ ...before, afterClick: location.href }));
})();
"""


def _job(state="running", **extra):
    return {
        "id": "abc",
        "label": "Discover repo-a",
        "auto": False,
        "running": state == "running",
        "state": state,
        "cancel_requested": False,
        "phase": "crawl",
        "step": "listing every file",
        "phases": [],
        "elapsed": 1,
        "log": [],
        "messages": [],
        "result": {},
        **extra,
    }


def _drive(tmp_path, **scenario):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    driver = tmp_path / "driver.js"
    driver.write_text(DRIVER)
    scenario.setdefault("pathname", "/runs")
    proc = subprocess.run(
        [node, str(driver), str(STATIC_DIR / "discover.js"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


DONE = _job("done", result={"run_id": 7, "url": "/runs/7/"})


def test_progress_screen_lands_on_the_new_run(tmp_path):
    out = _drive(tmp_path, current=_job(), statuses=[_job(), DONE])
    assert out["href"] == "/runs/7/"


def test_hidden_progress_screen_offers_the_run_instead_of_moving(tmp_path):
    out = _drive(tmp_path, current=_job(), statuses=[DONE], hide=True, clickBadge=True)
    assert out["href"] == "" and out["modalHidden"] is True and out["badgeHidden"] is False
    assert "open the run" in out["badge"] and out["afterClick"] == "/runs/7/"


def test_automatic_run_stays_behind_the_badge(tmp_path):
    out = _drive(
        tmp_path,
        current=_job(auto=True),
        statuses=[_job("done", auto=True, result={"run_id": 7, "url": "/runs/7/"})],
    )
    assert out["href"] == "" and out["modalHidden"] is True and out["badgeHidden"] is False


def test_cancelled_run_stays_on_the_page(tmp_path):
    out = _drive(tmp_path, current=_job(), statuses=[_job("cancelled")])
    assert out["href"] == "" and out["modalHidden"] is False


def test_failed_run_shows_why_in_the_box(tmp_path):
    out = _drive(tmp_path, current=_job(), statuses=[_job("error")])
    assert out["href"] == "" and out["modalHidden"] is False


def test_discover_js_parses_and_standalone_keeps_its_close_button(noise):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(STATIC_DIR / "discover.js")], check=True)
    index = Api(ModelStore(noise)).handle("GET", "/").body.decode()
    assert 'id="discover-close"' in index


# ---------------------------------------------------------------- 5. what the analyzer renders


RENDER_DRIVER = Path(__file__).parent / "render_analyzer.js"


def _responses(client, run_url: str) -> dict[str, str]:
    names = ("summary", "graph", "tree", "overview", "flow", "shapes", "environments")
    urls = [f"api/{n}" for n in (*names, "stack-cards", "icons", "env-matrix")]
    out = {u: client.get(run_url + u).data.decode() for u in urls}
    for root in json.loads(out["api/shapes"])["roots"]:
        for view in [None, *(v["view"] for v in root["views"])]:
            query = {"root": root["dir"], **({"view": view} if view else {})}
            resp = client.get(run_url + "api/root-view", query_string=query)
            key = "api/root-view?" + "&".join(f"{k}={query[k]}" for k in sorted(query))
            out[key] = resp.data.decode()
    return out


def test_the_rendered_analyzer_has_no_uncertain_content(client, noise, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    repo_id = _add(client, noise)
    run_url = _post(client, "/runs/new", {"repo_id": repo_id}).headers["Location"]
    data = tmp_path / "responses.json"
    data.write_text(json.dumps(_responses(client, run_url)))
    proc = subprocess.run(
        [node, str(RENDER_DRIVER), str(STATIC_DIR / "app.js"), str(data)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert not out["errors"], out["errors"]
    assert out["unanswered"] == []  # no view asks for an endpoint that is gone
    rendered = out["text"]
    # it did render: the confirmed facts of every view
    for part in (
        "Confirmed results",
        "Detected environments (4)",
        "Service inventory",
        "Start here",
    ):
        assert part in rendered, part
    for env in ("dev", "intg", "uat", "prod"):
        assert env in rendered, env
    _assert_clean(rendered, "rendered analyzer")
    for env in UNCONFIRMED_ENVS:
        assert not re.search(rf"\b{env}\b", rendered), env
    for gone in ("CI / script", "CI: ", "Documentation", "Recent commits", "Docs vs code"):
        assert gone not in rendered, gone
