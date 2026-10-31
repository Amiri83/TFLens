"""Repositories, environments, git sync and analysis runs (tfrepo.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import FIXTURES

from tflens import tfrepo
from tflens.crypto import SecretBox
from tflens.db import closing
from tflens.output import dumps, run_analysis

FAKE_TOKEN = "ghp_" + "Example0Token0For0Tests0Only0000"
FILE_PROTOCOLS = ("https", "ssh", "file")


@pytest.fixture
def box(tmp_path):
    return SecretBox.from_path(tmp_path / "secret.key")


@pytest.fixture
def repos_dir(tmp_path):
    return tmp_path / "repos"


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / "out"


@pytest.fixture
def local_repo(tmp_path):
    copy = tmp_path / "repo-a"
    shutil.copytree(FIXTURES / "multi_module", copy)
    (copy / "envs").mkdir()
    (copy / "envs" / "dev.tfvars").write_text('env = "dev"\n')
    return copy


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def git_origin(local_repo):
    """``local_repo`` as a git repository with one commit on branch ``main``."""
    _git("init", "-q", "-b", "main", cwd=local_repo)
    _git("add", ".", cwd=local_repo)
    _git("commit", "-q", "-m", "init", cwd=local_repo)
    return local_repo


def _dump(db_path) -> str:
    with closing(db_path) as conn:
        return "\n".join(conn.iterdump())


# -- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://git.example.com/org/repo-a.git",
        "ssh://git@git.example.com/org/repo-a.git",
        "git@git.example.com:org/repo-a.git",
    ],
)
def test_valid_git_urls(url):
    assert tfrepo.validate_git_url(url) == url


@pytest.mark.parametrize(
    "url, msg",
    [
        ("https://user:tok3n@git.example.com/org/repo-a.git", "credentials in the URL"),
        ("https://tok3n@git.example.com/org/repo-a.git", "credentials in the URL"),
        ("file:///srv/repo-a", "unsupported URL scheme"),
        ("ext::sh -c touch% /tmp/x", "git URL"),
        ("-uhelp", "git URL"),
        ("", "git URL"),
    ],
)
def test_invalid_git_urls(url, msg):
    with pytest.raises(ValueError, match=msg) as info:
        tfrepo.validate_git_url(url)
    assert "tok3n" not in str(info.value)


def test_paths_must_stay_inside_the_repository():
    assert tfrepo.validate_subdir(" envs/root-a/ ") == "envs/root-a"
    assert tfrepo.validate_subdir(".") == ""
    for bad in ("../x", "/etc", "a/../../b", "~/x"):
        with pytest.raises(ValueError, match="relative path"):
            tfrepo.validate_subdir(bad)
    assert tfrepo.parse_var_files("envs/dev.tfvars, common.tfvars.json\n") == [
        "envs/dev.tfvars",
        "common.tfvars.json",
    ]
    with pytest.raises(ValueError, match="must end in"):
        tfrepo.parse_var_files("main.tf")
    with pytest.raises(ValueError, match="relative path"):
        tfrepo.parse_var_files("../secrets.tfvars")


def test_parse_extra_vars_errors_never_echo_values():
    assert tfrepo.parse_extra_vars("# c\n\nregion = us-east-1\nenv=dev=x\n") == {
        "region": "us-east-1",
        "env": "dev=x",
    }
    with pytest.raises(ValueError, match="line 2") as info:
        tfrepo.parse_extra_vars(f"a=1\n{FAKE_TOKEN}\n")
    assert FAKE_TOKEN not in str(info.value)


# -- repository / environment CRUD ----------------------------------------------------------


def test_local_repo_crud(db_path, repos_dir, local_repo):
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo))
        # the same directory again: the same record
        assert tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo)) == repo_id
        repo = tfrepo.get_repo(conn, repo_id)
        assert (repo["name"], repo["source"], repo["path"]) == ("repo-a", "local", str(local_repo))
        tfrepo.update_repo(conn, repo_id, name="repo-b", path="", subdir="modules")
        repo = tfrepo.get_repo(conn, repo_id)
        assert (repo["name"], repo["subdir"]) == ("repo-b", "modules")
        assert tfrepo.analysis_root(repo) == local_repo / "modules"
        (listed,) = tfrepo.list_repos(conn)
        assert listed["envs"] == 0 and listed["runs"] == 0 and "token_enc" not in listed
        assert tfrepo.delete_repo(conn, repo_id, repos_dir)
        assert not tfrepo.delete_repo(conn, repo_id, repos_dir)
    assert local_repo.is_dir()  # a local repository is never touched
    with pytest.raises(ValueError, match="no such directory"), closing(db_path) as conn:
        tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo / "missing"))


def test_env_crud_encrypts_extra_vars(db_path, repos_dir, local_repo, box):
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo))
        env_id = tfrepo.add_env(
            conn,
            repo_id,
            "dev",
            "envs/dev.tfvars",
            f"token={FAKE_TOKEN}\nregion=us-east-1",
            box.encrypt,
        )
        with pytest.raises(ValueError, match="already exists"):
            tfrepo.add_env(conn, repo_id, "dev")
        with pytest.raises(ValueError, match="environment name"):
            tfrepo.add_env(conn, repo_id, "bad name!")
        tfrepo.add_env(conn, repo_id, "prod")
        envs = tfrepo.list_envs(conn, repo_id)
    assert [e["env"] for e in envs] == ["dev", "prod"]
    dev = envs[0]
    assert dev["var_files"] == ["envs/dev.tfvars"]
    assert dev["extra_var_keys"] == ["region", "token"]
    assert tfrepo.env_vars(dev, box.decrypt) == {"token": FAKE_TOKEN, "region": "us-east-1"}
    assert FAKE_TOKEN not in _dump(db_path)

    with closing(db_path) as conn:
        # None keeps the stored variables, '' clears them
        tfrepo.update_env(conn, repo_id, env_id, "uat", "", None, box.encrypt)
        env = tfrepo.get_env(conn, repo_id, env_id)
        assert (env["env"], env["var_files"], env["extra_var_keys"]) == (
            "uat",
            [],
            ["region", "token"],
        )
        tfrepo.update_env(conn, repo_id, env_id, "uat", "", "", box.encrypt)
        assert tfrepo.get_env(conn, repo_id, env_id)["extra_var_keys"] == []
        with pytest.raises(ValueError, match="already exists"):
            tfrepo.update_env(conn, repo_id, env_id, "prod")
        assert tfrepo.delete_env(conn, repo_id, env_id)
        assert not tfrepo.delete_env(conn, repo_id, env_id)
        assert [e["env"] for e in tfrepo.list_envs(conn, repo_id)] == ["prod"]
        # environments go with their repository
        tfrepo.delete_repo(conn, repo_id, repos_dir)
        assert conn.execute("SELECT COUNT(*) FROM tf_repo_envs").fetchone()[0] == 0


def test_git_repo_token_is_encrypted_and_kept_when_blank(db_path, repos_dir, box):
    url = "https://git.example.com/org/repo-a.git"
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(
            conn,
            repos_dir=repos_dir,
            source="git",
            url=url,
            branch="main",
            git_auth="token",
            token=FAKE_TOKEN,
            encrypt=box.encrypt,
        )
        repo = tfrepo.get_repo(conn, repo_id)
        assert repo["path"] == str(repos_dir / str(repo_id))
        assert repo["name"] == "repo-a"
        assert box.decrypt(repo["token_enc"]) == FAKE_TOKEN
        assert FAKE_TOKEN not in _dump(db_path)
        assert FAKE_TOKEN not in repr(tfrepo.list_repos(conn))
        assert tfrepo.list_repos(conn)[0]["has_token"] is True

        # a blank token keeps the stored one
        tfrepo.update_repo(conn, repo_id, url=url, git_auth="token", token="", encrypt=box.encrypt)
        assert box.decrypt(tfrepo.get_repo(conn, repo_id)["token_enc"]) == FAKE_TOKEN
        # switching to another auth mode drops it
        tfrepo.update_repo(conn, repo_id, url=url, git_auth="none", encrypt=box.encrypt)
        assert tfrepo.get_repo(conn, repo_id)["token_enc"] == ""
        with pytest.raises(ValueError, match="enter the access token"):
            tfrepo.update_repo(conn, repo_id, url=url, git_auth="token", encrypt=box.encrypt)
        with pytest.raises(ValueError, match="https"):
            tfrepo.add_repo(
                conn,
                repos_dir=repos_dir,
                source="git",
                url="git@git.example.com:org/repo-a.git",
                git_auth="token",
                token=FAKE_TOKEN,
                encrypt=box.encrypt,
            )


def test_git_env_passes_the_token_only_in_the_environment(tmp_path):
    repo = {
        "url": "https://git.example.com/org/repo-a.git",
        "branch": "main",
        "git_auth": "token",
        "token_user": "",
        "ssh_key_path": "",
    }
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "GIT_ASKPASS": "/bin/askpass", "AWS_X": "1"}
    env = tfrepo.git_env(repo, FAKE_TOKEN, base)
    argv = tfrepo.clone_argv(repo, tmp_path / "dest")
    assert argv[:7] == ["git", "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet"]
    assert argv[-3:] == ["--", repo["url"], str(tmp_path / "dest")]
    assert FAKE_TOKEN not in " ".join(argv)
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert "GIT_ASKPASS" not in env and "AWS_X" not in env
    key = dict(repo, git_auth="key", ssh_key_path="/keys/id a")
    assert (
        "-i '/keys/id a' -o IdentitiesOnly=yes" in tfrepo.git_env(key, "", base)["GIT_SSH_COMMAND"]
    )
    agent = tfrepo.git_env(dict(repo, git_auth="agent"), "", {**base, "SSH_AUTH_SOCK": "/s"})
    assert agent["SSH_AUTH_SOCK"] == "/s" and "GIT_CONFIG_COUNT" not in agent


def test_git_sync_shallow_clones_a_branch(db_path, repos_dir, git_origin):
    head = _git("rev-parse", "HEAD", cwd=git_origin)
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(
            conn,
            repos_dir=repos_dir,
            source="git",
            url=git_origin.as_uri(),
            branch="main",
            git_auth="none",
            allowed_protocols=FILE_PROTOCOLS,
        )
        repo = tfrepo.get_repo(conn, repo_id)
    result = tfrepo.sync_repo(repo, repos_dir, None, allowed_protocols=FILE_PROTOCOLS)
    assert result.ok, result.detail
    assert result.commit == head
    clone = repos_dir / str(repo_id)
    assert (clone / "main.tf").is_file()
    assert _git("rev-list", "--count", "HEAD", cwd=clone) == "1"
    assert _git("rev-parse", "--is-shallow-repository", cwd=clone) == "true"
    with closing(db_path) as conn:
        tfrepo.record_sync(conn, repo_id, result)
        synced = tfrepo.get_repo(conn, repo_id)
    assert (synced["sync_status"], synced["commit_sha"]) == ("ok", head)

    # a second sync replaces the clone; a failed one keeps it
    (git_origin / "extra.tf").write_text("# more\n")
    _git("add", ".", cwd=git_origin)
    _git("commit", "-q", "-m", "more", cwd=git_origin)
    again = tfrepo.sync_repo(repo, repos_dir, None, allowed_protocols=FILE_PROTOCOLS)
    assert again.ok and again.commit != head and (clone / "extra.tf").is_file()
    bad = dict(repo, branch="no-such-branch")
    failed = tfrepo.sync_repo(bad, repos_dir, None, allowed_protocols=FILE_PROTOCOLS)
    assert failed.status == tfrepo.FAILED and (clone / "extra.tf").is_file()
    assert not [p for p in repos_dir.iterdir() if ".tmp-" in p.name]
    # file:// is refused unless explicitly allowed
    assert tfrepo.sync_repo(repo, repos_dir, None).status == tfrepo.FAILED


def test_sync_failure_detail_never_contains_the_token(db_path, repos_dir, box):
    calls = {}

    def runner(argv, **kw):
        calls["argv"], calls["env"] = argv, kw["env"]
        msg = f"fatal: Authentication failed for 'https://x:{FAKE_TOKEN}@git.example.com/' {FAKE_TOKEN}"
        return subprocess.CompletedProcess(argv, 128, "", msg)

    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(
            conn,
            repos_dir=repos_dir,
            source="git",
            url="https://git.example.com/org/repo-a.git",
            git_auth="token",
            token=FAKE_TOKEN,
            encrypt=box.encrypt,
        )
        repo = tfrepo.get_repo(conn, repo_id)
    result = tfrepo.sync_repo(repo, repos_dir, box.decrypt, runner=runner)
    assert result.status == tfrepo.FAILED
    assert FAKE_TOKEN not in result.detail and "Authentication failed" in result.detail
    assert FAKE_TOKEN not in " ".join(calls["argv"])
    assert calls["env"]["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert not (repos_dir / str(repo_id)).exists()


def test_local_sync_writes_nothing(db_path, repos_dir, git_origin):
    before = sorted(
        p.relative_to(git_origin) for p in git_origin.rglob("*") if ".git" not in p.parts
    )
    with closing(db_path) as conn:
        repo = tfrepo.get_repo(
            conn, tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(git_origin))
        )
    result = tfrepo.sync_repo(repo, repos_dir, None)
    assert result.ok and result.commit == _git("rev-parse", "HEAD", cwd=git_origin)
    after = sorted(
        p.relative_to(git_origin) for p in git_origin.rglob("*") if ".git" not in p.parts
    )
    assert before == after and not repos_dir.exists()


# -- runs -----------------------------------------------------------------------------------


def test_run_writes_model_on_disk_and_only_the_path_in_the_db(
    db_path, repos_dir, out_dir, local_repo, box
):
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo))
        env_id = tfrepo.add_env(
            conn, repo_id, "dev", "envs/dev.tfvars", "region=us-east-1", box.encrypt
        )
        repo = tfrepo.get_repo(conn, repo_id)
        env = tfrepo.get_env(conn, repo_id, env_id)
        run_id = tfrepo.create_run(conn, repo, env, out_dir, box.decrypt)
        run = tfrepo.get_run(conn, run_id)
    model_path = Path(run["model_path"])
    assert model_path == out_dir / f"{repo_id}-repo-a" / str(run_id) / "model.json"
    assert (model_path.parent / "report.md").is_file()
    expected = run_analysis(local_repo, ["envs/dev.tfvars"], {"region": "us-east-1"}).model
    assert model_path.read_text() == dumps(expected)  # deterministic, byte-identical
    assert (run["repo"], run["env"], run["root_path"]) == ("repo-a", "dev", str(local_repo))
    assert run["summary"] == expected["summary"] and run["node_count"] > 0
    with closing(db_path) as conn:
        stored = conn.execute("SELECT summary FROM runs").fetchone()[0]
    assert len(stored) < 1000 and '"nodes"' in stored and "aws_" not in stored
    assert not (local_repo / "tflens-out").exists()  # nothing written into the repository


def test_run_refuses_missing_tfvars_and_unsynced_git(db_path, repos_dir, out_dir, local_repo):
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo))
        env_id = tfrepo.add_env(conn, repo_id, "dev", "envs/missing.tfvars")
        repo = tfrepo.get_repo(conn, repo_id)
        with pytest.raises(ValueError, match="not found"):
            tfrepo.create_run(conn, repo, tfrepo.get_env(conn, repo_id, env_id), out_dir, None)
        git_id = tfrepo.add_repo(
            conn,
            repos_dir=repos_dir,
            source="git",
            url="https://git.example.com/org/repo-b.git",
            git_auth="none",
        )
        with pytest.raises(ValueError, match="not been synced"):
            tfrepo.create_run(conn, tfrepo.get_repo(conn, git_id), None, out_dir, None)
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_prune_keeps_the_newest_runs_per_env(db_path, repos_dir, out_dir, local_repo):
    with closing(db_path) as conn:
        repo_id = tfrepo.add_repo(conn, repos_dir=repos_dir, path=str(local_repo))
        repo = tfrepo.get_repo(conn, repo_id)
        dev = tfrepo.get_env(conn, repo_id, tfrepo.add_env(conn, repo_id, "dev"))
        ids = [tfrepo.create_run(conn, repo, dev, out_dir, None) for _ in range(3)]
        other = tfrepo.create_run(conn, repo, None, out_dir, None)
        assert tfrepo.prune_runs(conn, out_dir, 2, repo_id, "dev") == 1
        remaining = [r["id"] for r in tfrepo.list_runs(conn, repo_id)]
        assert remaining == [other, ids[2], ids[1]]
        assert not (out_dir / f"{repo_id}-repo-a" / str(ids[0])).exists()
        assert tfrepo.delete_run(conn, ids[1], out_dir)
        assert not (out_dir / f"{repo_id}-repo-a" / str(ids[1])).exists()
        assert not tfrepo.delete_run(conn, ids[1], out_dir)
    assert json.loads((out_dir / f"{repo_id}-repo-a" / str(ids[2]) / "model.json").read_text())
