"""The AWS check next to the New run button: its default, per-repository memory, and that the
toggle and the account chosen reach phase 3, whose calls and errors are in the run log."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import types

import pytest
from conftest import FIXTURES

from tflens.awscheck import AwsTarget, run_checks
from tflens.db import init_db
from tflens.pipeline import NullProgress
from tflens.tfrepo import aws_choice
from tflens.web import create_app

FIXTURE = FIXTURES / "backend_resolution"
SECRET = "example-secret-value"
KEY_ID = "AKIAEXAMPLEEXAMPLE00"
OFF_LINE = "not requested for this run"


class _AwsError(Exception):
    def __init__(self, code):
        super().__init__(f"{code}: signed with {KEY_ID} / {SECRET}")
        self.response = {"Error": {"Code": code}}


class _Client:
    def __init__(self, session):
        self.session = session

    def get_caller_identity(self):
        if self.session.identity_error:
            raise _AwsError(self.session.identity_error)
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/example"}

    def __getattr__(self, name):
        def call(**kwargs):
            self.session.calls.append(name)
            if self.session.denied:
                raise _AwsError("AccessDenied")
            return {}

        return call


class _Session:
    def __init__(self, identity_error="", denied=False):
        self.identity_error, self.denied = identity_error, denied
        self.region_name = "us-east-1"
        self.calls = []
        self.kwargs = {}

    def client(self, service):
        return _Client(self)


@pytest.fixture
def fake_boto3(monkeypatch):
    session = _Session()

    def build(**kwargs):
        session.kwargs = kwargs
        return session

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=build))
    return session


class _App:
    def __init__(self, home):
        self.app = create_app(home, testing=True)
        self.client = self.app.test_client()
        self.client.get("/repos")
        self.jobs = self.app.extensions["tflens"]["jobs"]

    def post(self, url, **data):
        with self.client.session_transaction() as s:
            token = s["csrf"]
        return self.client.post(url, data={**data, "csrf_token": token})

    def add_account(self, name):
        self.post(
            "/accounts/new",
            display_name=name,
            region="us-east-1",
            auth_mode="keys",
            access_key_id=KEY_ID,
            secret_access_key=f"{SECRET}-{name}",
        )
        accounts = self.app.extensions["tflens"]["accounts"].list()
        return next(a for a in accounts if a.display_name == name)

    def add_repo(self, path=FIXTURE):
        resp = self.post("/repos", source="local", path=str(path))
        return int(resp.headers["Location"].rstrip("/").rsplit("/", 1)[1])

    def page(self, repo_id):
        return self.client.get(f"/repos/{repo_id}").data.decode()

    def run(self, repo_id, **fields):
        started = self.post(f"/repos/{repo_id}/discover", background="1", **fields)
        final = self.jobs.wait(started.get_json()["job"]["id"], 120)
        assert final["state"] == "done", final
        return final


@pytest.fixture
def web(home):
    return _App(home)


def _form(page):
    """The New run form holding the AWS check on a repository page."""
    (form,) = re.findall(r"<form[^>]*data-discover.*?</form>", page, re.S)
    return form


def _checked(form):
    box = re.search(r'<input type="checkbox" name="aws"[^>]*>', form).group(0)
    return "checked" in box.split('value="1"')[1]


def _disabled(form):
    box = re.search(r'<input type="checkbox" name="aws"[^>]*>', form).group(0)
    return "disabled" in box


def _selected(form):
    found = re.search(r'<option value="(\d+)"\s+selected', form)
    return int(found.group(1)) if found else None


def _aws_phase(job):
    return next(p for p in job["phases"] if p["name"] == "aws")


# -- the form --------------------------------------------------------------------------------


def test_the_toggle_is_next_to_the_button_on_every_start_form(web):
    web.add_account("example")
    repo_id = web.add_repo()
    for url in ("/runs", "/repos", f"/repos/{repo_id}"):
        page = web.client.get(url).data.decode()
        found = re.search(
            r'name="aws".*?AWS check.*?name="account_id".*?<button type="submit"[^>]*>New run',
            page,
            re.S,
        )
        assert found, url
        assert "<form" not in found.group(0), url  # the same form, nothing else in between
        assert "<p>" not in found.group(0) and "</div>" not in found.group(0), url
        assert SECRET not in page and KEY_ID not in page, url


def test_no_account_is_off_with_the_way_to_add_one(web):
    form = _form(web.page(web.add_repo()))
    assert not _checked(form) and _disabled(form)
    assert 'name="account_id"' not in form
    assert "add one under Accounts" in form


def test_exactly_one_account_is_on_with_it_preselected(web):
    account = web.add_account("example")
    form = _form(web.page(web.add_repo()))
    assert _checked(form) and not _disabled(form)
    assert _selected(form) == account.id


def test_several_accounts_are_off_until_one_is_chosen(web):
    web.add_account("example-a")
    web.add_account("example-b")
    form = _form(web.page(web.add_repo()))
    assert not _checked(form) and not _disabled(form)
    assert "example-a" in form and "example-b" in form


def test_an_account_with_a_problem_is_offered_and_the_run_says_why(web):
    account_id = web.add_account("example").id
    store = web.app.extensions["tflens"]["accounts"]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE accounts SET access_key_id='' WHERE id=?", (account_id,))
    repo_id = web.add_repo()
    form = _form(web.page(repo_id))
    assert _checked(form) and _selected(form) == account_id
    assert "credentials are incomplete" in form
    job = web.run(repo_id, aws="1", account_id=str(account_id))
    assert _aws_phase(job)["status"] == "skipped"
    assert "credentials are incomplete" in _aws_phase(job)["detail"]


# -- remembered per repository ----------------------------------------------------------------


def test_the_last_choice_is_remembered_per_repository(web, fake_boto3, tmp_path):
    first = web.add_account("example-a")
    second = web.add_account("example-b")
    other_path = tmp_path / "other"
    other_path.mkdir()
    (other_path / "main.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "example"\n}\n')
    repo_a, repo_b = web.add_repo(), web.add_repo(other_path)

    web.run(repo_a, aws="1", account_id=str(second.id))
    form = _form(web.page(repo_a))
    assert _checked(form) and _selected(form) == second.id
    assert not _checked(_form(web.page(repo_b)))  # the other repository keeps its own

    web.run(repo_b, aws="1", account_id=str(first.id))
    assert _selected(_form(web.page(repo_b))) == first.id
    assert _selected(_form(web.page(repo_a))) == second.id

    web.run(repo_a, account_id=str(second.id))  # the box unticked
    form = _form(web.page(repo_a))
    assert not _checked(form) and _selected(form) == second.id


def test_turned_off_stays_off_with_one_account(web):
    web.add_account("example")
    repo_id = web.add_repo()
    web.run(repo_id)
    assert not _checked(_form(web.page(repo_id)))


def test_a_run_with_no_account_saved_remembers_nothing(web, fake_boto3):
    repo_id = web.add_repo()
    web.run(repo_id)
    account = web.add_account("example")
    form = _form(web.page(repo_id))
    assert _checked(form) and _selected(form) == account.id


def test_the_runs_page_form_follows_the_repository_picked(web, fake_boto3, tmp_path):
    web.add_account("example-a")
    second = web.add_account("example-b")
    other_path = tmp_path / "other"
    other_path.mkdir()
    (other_path / "main.tf").write_text('resource "aws_sqs_queue" "q" {\n  name = "example"\n}\n')
    repo_a, repo_b = web.add_repo(), web.add_repo(other_path)
    web.post(f"/repos/{repo_a}/discover", aws="1", account_id=str(second.id))
    page = web.client.get("/runs").data.decode()
    raw = re.search(r'data-aws-choices="([^"]+)"', page).group(1)
    choices = json.loads(raw.replace("&#34;", '"'))
    assert choices[str(repo_a)] == {"on": True, "account_id": second.id}
    assert choices[str(repo_b)] == {"on": False, "account_id": None}
    # a page filtered on a repository opens on its choice
    form = web.client.get(f"/runs?repo={repo_a}").data.decode()
    assert re.search(rf'<option value="{second.id}"\s+selected', form)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"aws_check": "", "aws_account": ""}, {"on": True, "account_id": 1}),
        ({"aws_check": "1", "aws_account": "1"}, {"on": True, "account_id": 1}),
        ({"aws_check": "0", "aws_account": "1"}, {"on": False, "account_id": 1}),
        # its account was deleted: back to the default, not to another account's credentials
        ({"aws_check": "1", "aws_account": "9"}, {"on": True, "account_id": 1}),
        ({"aws_check": "0", "aws_account": "9"}, {"on": False, "account_id": 1}),
    ],
)
def test_the_choice_with_one_account(row, expected):
    assert aws_choice(row, [{"id": 1}]) == expected


def test_a_deleted_account_with_several_left_does_not_pick_one():
    row = {"aws_check": "1", "aws_account": "9"}
    assert aws_choice(row, [{"id": 1}, {"id": 2}]) == {"on": False, "account_id": None}
    assert aws_choice(row, []) == {"on": False, "account_id": None}


def test_a_database_from_before_the_choice_is_upgraded(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tf_repos (id INTEGER PRIMARY KEY, path TEXT, added_at TEXT)")
    init_db(db)
    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tf_repos)")}
    assert {"aws_check", "aws_account"} <= columns


# -- it reaches phase 3 -----------------------------------------------------------------------


def test_the_toggle_reaches_phase_3_with_the_account_chosen(web, fake_boto3):
    web.add_account("example-a")
    second = web.add_account("example-b")
    repo_id = web.add_repo()
    final = web.run(repo_id, aws="1", account_id=str(second.id))
    phase = _aws_phase(final)
    assert phase["status"] == "done", phase
    assert OFF_LINE not in phase["detail"] and OFF_LINE not in "\n".join(final["log"])
    assert fake_boto3.calls  # phase 3 made its calls ...
    # ... with the chosen account's credentials, not the first's
    assert fake_boto3.kwargs["aws_secret_access_key"] == f"{SECRET}-example-b"
    assert "AWS check: account used: example-b" in final["log"]


def test_the_form_as_rendered_runs_phase_3_with_one_account(web, fake_boto3):
    """What a browser sends for the page it got, without touching the form."""
    account = web.add_account("example")
    repo_id = web.add_repo()
    form = _form(web.page(repo_id))
    fields = {"aws": "1"} if _checked(form) else {}
    fields["account_id"] = str(_selected(form))
    assert fields == {"aws": "1", "account_id": str(account.id)}
    final = web.run(repo_id, **fields)
    assert _aws_phase(final)["status"] == "done"
    assert fake_boto3.calls


def test_the_toggle_off_says_how_to_request_it(web, fake_boto3):
    web.add_account("example")
    repo_id = web.add_repo()
    final = web.run(repo_id)
    phase = _aws_phase(final)
    assert phase["status"] == "off"
    assert "AWS check next to the New run button" in phase["detail"]
    assert fake_boto3.calls == []


# -- the run log ------------------------------------------------------------------------------


class _Recording(NullProgress):
    def __init__(self):
        self.lines = []

    def log(self, text):
        self.lines.append(text)


def _candidates():
    return {
        "nodes": [
            {
                "id": "aws_sqs_queue.jobs",
                "kind": "resource",
                "type": "aws_sqs_queue",
                "location": {"file": "main.tf", "start_line": 1},
                "attributes": {"name": "example-jobs"},
            }
        ]
    }


def test_the_log_names_the_account_and_the_calls(caplog):
    prog = _Recording()
    target = AwsTarget("example", "us-east-1", _Session)
    with caplog.at_level(logging.INFO, logger="tflens"):
        run_checks(_candidates(), target, True, prog)
    assert prog.lines == [
        "AWS check: account used: example",
        "AWS check: call: sts:GetCallerIdentity",
        "AWS check: call: sqs:GetQueueUrl aws_sqs_queue.jobs: exists",
        "AWS check: 2 read-only call(s) made",
    ]
    assert "AWS check: account used: example" in caplog.text  # the log file too


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("ProfileNotFound", "bad profile"),
        ("SSOTokenLoadError", "expired SSO"),
        ("UnauthorizedSSOTokenError", "expired SSO"),
        ("TokenRetrievalError", "expired SSO"),
        ("AccessDenied", "AccessDenied"),
        ("NoRegionError", "region"),
        ("InvalidRegionError", "region"),
    ],
)
def test_the_log_names_the_error_never_its_text(caplog, code, name):
    prog = _Recording()
    target = AwsTarget("example", "us-east-1", lambda: _Session(identity_error=code))
    with caplog.at_level(logging.DEBUG, logger="tflens"):
        out = run_checks(_candidates(), target, True, prog)
    assert out["status"] == "skipped"
    assert f"AWS check: error: {name} ({code})" in prog.lines
    for text in ("\n".join(prog.lines), caplog.text, json.dumps(out)):
        assert SECRET not in text and KEY_ID not in text


def test_a_bad_profile_fails_when_the_session_is_built(caplog):
    class ProfileNotFound(Exception):
        pass

    def session():
        raise ProfileNotFound("example-profile-text")

    prog = _Recording()
    with caplog.at_level(logging.DEBUG, logger="tflens"):
        out = run_checks(_candidates(), AwsTarget("example", "", session), True, prog)
    assert out["status"] == "skipped"
    assert "AWS check: error: bad profile (ProfileNotFound)" in prog.lines
    assert "example-profile-text" not in "\n".join(prog.lines)
    assert "example-profile-text" not in caplog.text


def test_a_missing_region_is_logged_before_any_call():
    session = _Session()
    session.region_name = None
    prog = _Recording()
    out = run_checks(_candidates(), AwsTarget("example", "", lambda: session), True, prog)
    assert out["status"] == "skipped"
    assert any(line.startswith("AWS check: error: region") for line in prog.lines)
    assert session.calls == []


def test_a_denied_call_is_logged_with_the_error():
    prog = _Recording()
    target = AwsTarget("example", "us-east-1", lambda: _Session(denied=True))
    run_checks(_candidates(), target, True, prog)
    assert (
        "AWS check: call: sqs:GetQueueUrl aws_sqs_queue.jobs: denied (AccessDenied)" in prog.lines
    )


def test_the_resource_name_stays_out_of_the_log():
    prog = _Recording()
    run_checks(_candidates(), AwsTarget("example", "us-east-1", _Session), True, prog)
    assert "example-jobs" not in "\n".join(prog.lines)
