"""Terraform repositories, their environments, git sync and analysis runs.

A repository is a local directory or a git URL (+ branch, + sub-directory). **Sync** of a
git repository is the only network operation TFLens performs, and only on an explicit
Sync: a shallow ``git clone --depth 1`` into ``repos/<id>`` of the data directory, run
with an explicit argv (no shell), no terminal prompt and no askpass helper. Git
authentication is the SSH agent, an SSH key file, or a token: the token is stored
Fernet-encrypted, decrypted only into the child process's environment
(``GIT_CONFIG_*`` -> ``http.extraHeader``, never the argv or a config file), and never
logged or shown. Local repositories are never written to.

An environment is a list of tfvars files plus extra ``-var KEY=VALUE`` lines (stored
encrypted, key names in clear). **Analyze** runs the static analyzer read-only over the
repository and writes ``model.json`` / ``report.md`` under ``out/<repo>/<run>/``; the
``runs`` table only records the path and summary counts.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .logging_setup import redact

UTC = timezone.utc

log = logging.getLogger(__name__)

SOURCES = ("local", "git")
SOURCE_LABELS = {"local": "Local directory", "git": "Git repository"}
GIT_AUTH_MODES = ("none", "agent", "key", "token")
GIT_AUTH_LABELS = {
    "none": "None (public HTTPS)",
    "agent": "SSH agent",
    "key": "SSH key file",
    "token": "Access token (HTTPS)",
}
DEFAULT_TOKEN_USER = "x-access-token"
GIT_TIMEOUT = 300.0
# Protocols git may use for a sync (GIT_ALLOW_PROTOCOL); tests add "file".
ALLOWED_PROTOCOLS: tuple[str, ...] = ("https", "ssh")
MAX_NAME = 64
MAX_TOKEN = 4096
MAX_VAR_LINES = 500
MAX_DETAIL = 500

OK, FAILED = "ok", "failed"

_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PASSTHROUGH_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GIT_SSL_CAINFO",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_GIT_SSH_DEFAULT = "ssh -o BatchMode=yes -o ConnectTimeout=10"

Runner = Callable[..., subprocess.CompletedProcess]
Encrypt = Callable[[str], str]
Decrypt = Callable[[str], str]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# -- validation (error messages never quote a secret) --------------------------------------


def validate_repo_path(path: str) -> str:
    p = Path((path or "").strip()).expanduser()
    if not str(p) or not p.is_absolute():
        raise ValueError("enter an absolute path to a local directory")
    if not p.is_dir():
        raise ValueError("no such directory")
    return str(p.resolve())


def validate_git_url(url: str, allowed_protocols: Sequence[str] = ALLOWED_PROTOCOLS) -> str:
    """An https:// / ssh:// / ``user@host:path`` URL without embedded credentials."""
    url = (url or "").strip()
    if not url or any(c.isspace() for c in url) or url.startswith("-"):
        raise ValueError("enter a git URL (https://… or git@host:org/repo.git)")
    if _SCP_URL_RE.match(url) and "://" not in url:
        scheme = "ssh"
    else:
        scheme, sep, rest = url.partition("://")
        scheme = scheme.lower()
        if not sep or not rest:
            raise ValueError("enter a git URL (https://… or git@host:org/repo.git)")
        authority = rest.split("/", 1)[0]
        if scheme in ("http", "https") and "@" in authority:
            raise ValueError(
                "do not put credentials in the URL: choose token authentication instead"
            )
        if scheme == "ssh" and ":" in authority.rpartition("@")[0]:
            raise ValueError("do not put a password in the URL")
    if scheme not in allowed_protocols:
        raise ValueError(f"unsupported URL scheme (use {' or '.join(allowed_protocols)})")
    return url


def url_scheme(url: str) -> str:
    return "ssh" if _SCP_URL_RE.match(url) and "://" not in url else url.split("://")[0].lower()


def validate_branch(branch: str) -> str:
    branch = (branch or "").strip()
    if branch and (not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith(".lock")):
        raise ValueError("the branch name is not valid")
    return branch


def _relative(value: str, what: str) -> str:
    """A normalized relative POSIX path inside the repository ('' = the top)."""
    value = (value or "").strip().replace("\\", "/")
    if not value or value in (".", "./"):
        return ""
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or value.startswith("~"):
        raise ValueError(f"the {what} must be a relative path inside the repository")
    return str(p)


def validate_subdir(subdir: str) -> str:
    return _relative(subdir, "sub-directory")


def validate_name(name: str) -> str:
    name = " ".join((name or "").split())
    if len(name) > MAX_NAME:
        raise ValueError(f"the name must be at most {MAX_NAME} characters")
    return name


def validate_env_name(env: str) -> str:
    env = (env or "").strip()
    if not _ENV_NAME_RE.match(env):
        raise ValueError(
            "the environment name must be 1-63 letters, digits, '.', '_' or '-' "
            "(starting with a letter or digit)"
        )
    return env


def parse_var_files(text: str) -> list[str]:
    """tfvars paths (one per line or comma-separated), relative to the analyzed directory."""
    out: list[str] = []
    for item in re.split(r"[\n,]", text or ""):
        item = item.strip()
        if not item:
            continue
        rel = _relative(item, "tfvars file")
        if not (rel.endswith(".tfvars") or rel.endswith(".tfvars.json")):
            raise ValueError(f"{rel}: a tfvars file must end in .tfvars or .tfvars.json")
        if rel not in out:
            out.append(rel)
    return out


def parse_extra_vars(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines (blank lines and ``#`` comments skipped). Errors name the line
    number, never a value."""
    out: dict[str, str] = {}
    lines = (text or "").splitlines()
    if len(lines) > MAX_VAR_LINES:
        raise ValueError(f"at most {MAX_VAR_LINES} variable lines")
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not _VAR_NAME_RE.match(key):
            raise ValueError(f"line {n}: expected KEY=VALUE with a valid variable name")
        out[key] = value.strip()
    return out


# -- repositories -----------------------------------------------------------------------


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text).strip("-.")[:40] or "repo"


def default_name(source: str, path: str, url: str) -> str:
    if source == "git":
        tail = re.split(r"[/:]", url.rstrip("/"))[-1]
        return tail.removesuffix(".git") or "repo"
    return Path(path).name or "repo"


def _git_fields(
    url: str,
    branch: str,
    git_auth: str,
    ssh_key_path: str,
    token_user: str,
    allowed_protocols: Sequence[str],
) -> dict[str, str]:
    url = validate_git_url(url, allowed_protocols)
    if git_auth not in GIT_AUTH_MODES:
        raise ValueError(f"git authentication must be one of {', '.join(GIT_AUTH_MODES)}")
    scheme = url_scheme(url)
    if git_auth == "token" and scheme not in ("https", "http"):
        raise ValueError("token authentication needs an https:// URL")
    if git_auth == "key" and scheme != "ssh":
        raise ValueError("SSH key authentication needs an ssh URL (git@host:org/repo.git)")
    key = ""
    if git_auth == "key":
        key = str(Path(ssh_key_path.strip()).expanduser()) if ssh_key_path.strip() else ""
        if not key or not Path(key).is_absolute():
            raise ValueError("enter the absolute path of the SSH private key file")
        if not Path(key).is_file():
            raise ValueError("the SSH key file does not exist")
    return {
        "url": url,
        "branch": validate_branch(branch),
        "git_auth": git_auth,
        "ssh_key_path": key,
        "token_user": token_user.strip()[:128] if git_auth == "token" else "",
    }


def add_repo(
    conn: sqlite3.Connection,
    *,
    repos_dir: Path,
    source: str = "local",
    path: str = "",
    url: str = "",
    branch: str = "",
    subdir: str = "",
    name: str = "",
    git_auth: str = "agent",
    ssh_key_path: str = "",
    token_user: str = "",
    token: str = "",
    encrypt: Encrypt | None = None,
    allowed_protocols: Sequence[str] = ALLOWED_PROTOCOLS,
) -> int:
    """Add a repository; return its id. A local path already added returns that id."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    subdir = validate_subdir(subdir)
    name = validate_name(name)
    if source == "local":
        path = validate_repo_path(path)
        row = conn.execute("SELECT id FROM tf_repos WHERE path=?", (path,)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO tf_repos(path, added_at, name, source, subdir, git_auth) "
            "VALUES(?, ?, ?, 'local', ?, 'none')",
            (path, _now(), name or default_name("local", path, ""), subdir),
        )
        return int(cur.lastrowid or 0)
    fields = _git_fields(url, branch, git_auth, ssh_key_path, token_user, allowed_protocols)
    token_enc = _token_enc(fields["git_auth"], token, None, encrypt)
    cur = conn.execute(
        "INSERT INTO tf_repos(path, added_at, name, source, url, branch, subdir, git_auth, "
        "ssh_key_path, token_user, token_enc) VALUES(?, ?, ?, 'git', ?, ?, ?, ?, ?, ?, ?)",
        (
            f"pending:{uuid.uuid4().hex}",
            _now(),
            name or default_name("git", "", fields["url"]),
            fields["url"],
            fields["branch"],
            subdir,
            fields["git_auth"],
            fields["ssh_key_path"],
            fields["token_user"],
            token_enc,
        ),
    )
    repo_id = int(cur.lastrowid or 0)
    clone = repos_dir / str(repo_id)
    conn.execute("UPDATE tf_repos SET path=? WHERE id=?", (str(clone), repo_id))
    return repo_id


def _token_enc(git_auth: str, token: str, existing: str | None, encrypt: Encrypt | None) -> str:
    """The encrypted token to store: a new one, else the existing one (token auth only)."""
    if git_auth != "token":
        return ""
    token = (token or "").strip()
    if token:
        if len(token) > MAX_TOKEN or any(c.isspace() for c in token):
            raise ValueError("the token looks malformed")
        if encrypt is None:
            raise ValueError("cannot store a token without an encryption key")
        return encrypt(token)
    if existing:
        return existing
    raise ValueError("enter the access token")


def update_repo(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    name: str = "",
    path: str = "",
    url: str = "",
    branch: str = "",
    subdir: str = "",
    git_auth: str = "agent",
    ssh_key_path: str = "",
    token_user: str = "",
    token: str = "",
    encrypt: Encrypt | None = None,
    allowed_protocols: Sequence[str] = ALLOWED_PROTOCOLS,
) -> None:
    """Edit a repository. A blank ``token`` keeps the stored one (the form never
    round-trips it); switching away from token auth drops it."""
    repo = get_repo(conn, repo_id)
    if repo is None:
        raise ValueError("unknown repository")
    subdir = validate_subdir(subdir)
    name = validate_name(name) or repo["name"]
    if repo["source"] == "local":
        path = validate_repo_path(path) if path.strip() else repo["path"]
        clash = conn.execute(
            "SELECT id FROM tf_repos WHERE path=? AND id != ?", (path, repo_id)
        ).fetchone()
        if clash:
            raise ValueError("this directory is already added")
        conn.execute(
            "UPDATE tf_repos SET name=?, path=?, subdir=? WHERE id=?",
            (name, path, subdir, repo_id),
        )
        return
    fields = _git_fields(url, branch, git_auth, ssh_key_path, token_user, allowed_protocols)
    existing = repo["token_enc"] if repo["git_auth"] == "token" else None
    token_enc = _token_enc(fields["git_auth"], token, existing, encrypt)
    conn.execute(
        "UPDATE tf_repos SET name=?, url=?, branch=?, subdir=?, git_auth=?, ssh_key_path=?, "
        "token_user=?, token_enc=? WHERE id=?",
        (
            name,
            fields["url"],
            fields["branch"],
            subdir,
            fields["git_auth"],
            fields["ssh_key_path"],
            fields["token_user"],
            token_enc,
            repo_id,
        ),
    )


def public_repo(row: dict[str, Any]) -> dict[str, Any]:
    """A repository row without secret material (``has_token`` instead of the token)."""
    out = {k: v for k, v in row.items() if k != "token_enc"}
    out["has_token"] = bool(row.get("token_enc"))
    return out


def list_repos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        public_repo(dict(r))
        for r in conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM tf_repo_envs e WHERE e.repo_id = p.id) AS envs, "
            "(SELECT COUNT(*) FROM runs r WHERE r.repo_id = p.id) AS runs, "
            "(SELECT MAX(r.ts) FROM runs r WHERE r.repo_id = p.id) AS last_run "
            "FROM tf_repos p ORDER BY p.name, p.id"
        )
    ]


def get_repo(conn: sqlite3.Connection, repo_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM tf_repos WHERE id=?", (repo_id,)).fetchone()
    return dict(row) if row else None


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _safe_rmtree(path: Path, base: Path) -> None:
    """Remove ``path`` only if it lies strictly inside ``base`` (never a user directory)."""
    if path.exists() and _inside(path, base) and path.resolve() != base.resolve():
        shutil.rmtree(path, ignore_errors=True)


def delete_repo(conn: sqlite3.Connection, repo_id: int, repos_dir: Path) -> bool:
    """Forget a repository and its environments, and delete its clone (git sources).
    Its runs stay in the history; a local repository itself is never touched."""
    repo = get_repo(conn, repo_id)
    if repo is None:
        return False
    if repo["source"] == "git":
        _safe_rmtree(Path(repo["path"]), repos_dir)
    return conn.execute("DELETE FROM tf_repos WHERE id=?", (repo_id,)).rowcount > 0


def analysis_root(repo: dict[str, Any]) -> Path:
    """The directory analyzed: the repository (or its clone) + the sub-directory."""
    base = Path(repo["path"])
    root = base / repo["subdir"] if repo["subdir"] else base
    if not _inside(root, base):
        raise ValueError("the sub-directory is outside the repository")
    return root


# -- environments -------------------------------------------------------------------------


def _env_fields(name: str, var_files: str, extra_vars: str | None, encrypt: Encrypt | None):
    env = validate_env_name(name)
    files = parse_var_files(var_files)
    if extra_vars is None:
        return env, files, None
    values = parse_extra_vars(extra_vars)
    if values and encrypt is None:
        raise ValueError("cannot store variables without an encryption key")
    enc = encrypt(json.dumps(values, sort_keys=True)) if values and encrypt else ""
    return env, files, (enc, ",".join(sorted(values)))


def add_env(
    conn: sqlite3.Connection,
    repo_id: int,
    name: str,
    var_files: str = "",
    extra_vars: str = "",
    encrypt: Encrypt | None = None,
) -> int:
    repo = get_repo(conn, repo_id)
    if repo is None:
        raise ValueError("unknown repository")
    env, files, extra = _env_fields(name, var_files, extra_vars, encrypt)
    enc, keys = extra or ("", "")
    detected = conn.execute(
        "SELECT id FROM tf_repo_envs WHERE repo_id=? AND root_rel=? AND env=? AND confirmed=0 "
        "AND kinds NOT IN ('', 'manual')",
        (repo_id, repo["subdir"] or ".", env),
    ).fetchone()
    if detected is not None:
        # the user defines an environment TFLens had detected: adopt it with their values
        conn.execute(
            "UPDATE tf_repo_envs SET var_file=?, var_files=?, extra_vars_enc=?, "
            "extra_vars_keys=?, confirmed=1 WHERE id=?",
            (files[0] if files else "", json.dumps(files), enc, keys, detected["id"]),
        )
        return int(detected["id"])
    try:
        cur = conn.execute(
            "INSERT INTO tf_repo_envs(repo_id, root_rel, root_label, env, kinds, var_file, "
            "var_files, extra_vars_enc, extra_vars_keys) VALUES(?, ?, ?, ?, 'manual', ?, ?, ?, ?)",
            (
                repo_id,
                repo["subdir"] or ".",
                repo["subdir"] or ".",
                env,
                files[0] if files else "",
                json.dumps(files),
                enc,
                keys,
            ),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"environment {env!r} already exists") from None
    return int(cur.lastrowid or 0)


def update_env(
    conn: sqlite3.Connection,
    repo_id: int,
    env_id: int,
    name: str,
    var_files: str = "",
    extra_vars: str | None = None,
    encrypt: Encrypt | None = None,
) -> None:
    """Edit an environment. ``extra_vars=None`` keeps the stored variables; '' clears."""
    env, files, extra = _env_fields(name, var_files, extra_vars, encrypt)
    try:
        # confirmed=1: a later discovery never overwrites what the user edited
        cur = conn.execute(
            "UPDATE tf_repo_envs SET env=?, var_file=?, var_files=?, confirmed=1 "
            "WHERE id=? AND repo_id=?",
            (env, files[0] if files else "", json.dumps(files), env_id, repo_id),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"environment {env!r} already exists") from None
    if cur.rowcount == 0:
        raise ValueError("unknown environment")
    if extra is not None:
        conn.execute(
            "UPDATE tf_repo_envs SET extra_vars_enc=?, extra_vars_keys=? WHERE id=?",
            (*extra, env_id),
        )


def delete_env(conn: sqlite3.Connection, repo_id: int, env_id: int) -> bool:
    return (
        conn.execute(
            "DELETE FROM tf_repo_envs WHERE id=? AND repo_id=?", (env_id, repo_id)
        ).rowcount
        > 0
    )


def _env_row(r: sqlite3.Row) -> dict[str, Any]:
    out = {k: v for k, v in dict(r).items() if k not in ("extra_vars_enc", "backend_extra_enc")}
    try:
        out["var_files"] = [f for f in json.loads(r["var_files"] or "[]") if isinstance(f, str)]
    except ValueError:
        out["var_files"] = []
    out["extra_var_keys"] = [k for k in (r["extra_vars_keys"] or "").split(",") if k]
    out["_extra_enc"] = r["extra_vars_enc"] or ""
    return out


def list_envs(conn: sqlite3.Connection, repo_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM tf_repo_envs WHERE repo_id=? ORDER BY env, root_rel", (repo_id,)
    ).fetchall()
    return [_env_row(r) for r in rows]


def get_env(conn: sqlite3.Connection, repo_id: int, env_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM tf_repo_envs WHERE id=? AND repo_id=?", (env_id, repo_id)
    ).fetchone()
    return _env_row(row) if row else None


@dataclass
class Discovery:
    added: int = 0
    updated: int = 0
    gone: int = 0
    found: int = 0


def discover_envs(conn: sqlite3.Connection, repo: dict[str, Any]) -> Discovery:
    """Create / refresh the environments detected in the repository (tfvars files,
    envs/ or env-named directories, Terragrunt layouts). Environments the user edited
    (``confirmed``) or added by hand are never changed; detected ones no longer found are
    kept but marked ``present=0``."""
    from .detect import detect_environments
    from .engine import Engine
    from .loader import load_repo

    root = analysis_root(repo)
    if not root.is_dir():
        raise ValueError(
            "the repository has not been synced yet"
            if repo["source"] == "git"
            else "the directory does not exist"
        )
    loaded = load_repo(root)
    found = detect_environments(loaded, Engine(loaded))
    result = Discovery(found=len(found))
    rows = {
        (r["root_rel"], r["env"]): r
        for r in conn.execute("SELECT * FROM tf_repo_envs WHERE repo_id=?", (repo["id"],))
    }
    seen: set[int] = set()
    for e in found:
        kinds = ",".join(e["kinds"])
        files = json.dumps(e["var_files"])
        label = e["root"] if e["root"] != "." else (repo["subdir"] or ".")
        row = rows.get((e["root"], e["name"]))
        if row is None:
            try:
                cur = conn.execute(
                    "INSERT INTO tf_repo_envs(repo_id, root_rel, root_label, env, kinds, "
                    "var_file, var_files, confirmed, present) VALUES(?, ?, ?, ?, ?, ?, ?, 0, 1)",
                    (
                        repo["id"],
                        e["root"],
                        label,
                        e["name"],
                        kinds,
                        e["var_files"][0] if e["var_files"] else "",
                        files,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
            seen.add(int(cur.lastrowid or 0))
            result.added += 1
            continue
        seen.add(int(row["id"]))
        if row["confirmed"] or row["kinds"] in ("", "manual"):
            conn.execute("UPDATE tf_repo_envs SET present=1 WHERE id=?", (row["id"],))
            continue
        if (row["kinds"], row["var_files"], row["present"]) != (kinds, files, 1):
            result.updated += 1
        conn.execute(
            "UPDATE tf_repo_envs SET kinds=?, var_file=?, var_files=?, root_label=?, present=1 "
            "WHERE id=?",
            (kinds, e["var_files"][0] if e["var_files"] else "", files, label, row["id"]),
        )
    for row in rows.values():
        if int(row["id"]) not in seen and row["kinds"] not in ("", "manual") and row["present"]:
            conn.execute("UPDATE tf_repo_envs SET present=0 WHERE id=?", (row["id"],))
            result.gone += 1
    conn.execute("UPDATE tf_repos SET discovered_at=? WHERE id=?", (_now(), repo["id"]))
    return result


def env_vars(env: dict[str, Any], decrypt: Decrypt | None) -> dict[str, str]:
    """The decrypted extra variables of an environment ({} if none / undecryptable)."""
    enc = env.get("_extra_enc") or ""
    if not enc or decrypt is None:
        return {}
    try:
        values = json.loads(decrypt(enc))
    except ValueError:
        log.warning("extra variables of environment %s cannot be decrypted", env.get("id"))
        return {}
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)}


# -- git sync -------------------------------------------------------------------------------


@dataclass
class SyncResult:
    status: str
    detail: str = ""
    commit: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


def git_env(
    repo: dict[str, Any],
    token: str = "",
    base: dict[str, str] | None = None,
    allowed_protocols: Sequence[str] = ALLOWED_PROTOCOLS,
) -> dict[str, str]:
    """Environment of a git child process: a small passthrough, no prompts, the repo's
    authentication. The result can hold the token: it is never logged."""
    base = dict(os.environ) if base is None else base
    env = {k: v for k, v in base.items() if k in _PASSTHROUGH_ENV}
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL=":".join(allowed_protocols),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_LFS_SKIP_SMUDGE="1",
    )
    ssh = _GIT_SSH_DEFAULT
    if repo["git_auth"] == "agent" and base.get("SSH_AUTH_SOCK"):
        env["SSH_AUTH_SOCK"] = base["SSH_AUTH_SOCK"]
    elif repo["git_auth"] == "key":
        ssh += f" -i {shlex.quote(repo['ssh_key_path'])} -o IdentitiesOnly=yes"
    env["GIT_SSH_COMMAND"] = ssh
    if repo["git_auth"] == "token" and token:
        user = repo.get("token_user") or DEFAULT_TOKEN_USER
        basic = base64.b64encode(f"{user}:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.extraHeader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}",
        )
    return env


def clone_argv(repo: dict[str, Any], dest: Path, git_bin: str = "git") -> list[str]:
    argv = [git_bin, "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet"]
    if repo["branch"]:
        argv += ["--branch", repo["branch"]]
    return [*argv, "--", repo["url"], str(dest)]


def _clean(text: str, secrets: Sequence[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    lines = [ln for ln in redact(text).strip().splitlines() if ln.strip()]
    return " / ".join(lines[-3:])[:MAX_DETAIL]


def head_commit(path: Path, runner: Runner = subprocess.run) -> str:
    """HEAD of a git checkout ('' if it is not one or git is unavailable). Local only."""
    if not (path / ".git").exists():
        return ""
    try:
        proc = runner(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            env={k: v for k, v in os.environ.items() if k in _PASSTHROUGH_ENV},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else ""


def sync_repo(
    repo: dict[str, Any],
    repos_dir: Path,
    decrypt: Decrypt | None,
    *,
    runner: Runner = subprocess.run,
    timeout: float = GIT_TIMEOUT,
    allowed_protocols: Sequence[str] = ALLOWED_PROTOCOLS,
) -> SyncResult:
    """Sync one repository. git: a fresh shallow clone into ``repos/<id>`` (the previous
    clone is replaced only once the new one succeeded). local: check the directory and
    read its HEAD commit; nothing is written."""
    if repo["source"] == "local":
        path = Path(repo["path"])
        if not path.is_dir():
            return SyncResult(FAILED, "the directory no longer exists")
        return SyncResult(OK, "local directory", head_commit(path, runner))
    token = ""
    if repo["git_auth"] == "token":
        if not repo.get("token_enc") or decrypt is None:
            return SyncResult(FAILED, "no access token stored")
        try:
            token = decrypt(repo["token_enc"])
        except ValueError as exc:
            return SyncResult(FAILED, str(exc))
    try:
        validate_git_url(repo["url"], allowed_protocols)
    except ValueError as exc:
        return SyncResult(FAILED, str(exc))
    dest = Path(repo["path"])
    if not _inside(dest, repos_dir) or dest.resolve() == repos_dir.resolve():
        return SyncResult(FAILED, "the clone directory is outside the data directory")
    repos_dir.mkdir(parents=True, exist_ok=True)
    tmp = repos_dir / f".{dest.name}.tmp-{uuid.uuid4().hex[:8]}"
    env = git_env(repo, token, allowed_protocols=allowed_protocols)
    secrets = [token, env.get("GIT_CONFIG_VALUE_0", "").rpartition(" ")[2]]
    try:
        proc = runner(
            clone_argv(repo, tmp),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(repos_dir),
            check=False,
        )
    except subprocess.TimeoutExpired:
        _safe_rmtree(tmp, repos_dir)
        return SyncResult(FAILED, f"git clone timed out after {timeout:.0f}s")
    except OSError as exc:
        _safe_rmtree(tmp, repos_dir)
        return SyncResult(FAILED, f"cannot run git ({type(exc).__name__})")
    if proc.returncode != 0:
        _safe_rmtree(tmp, repos_dir)
        return SyncResult(FAILED, _clean(proc.stderr or "git clone failed", secrets))
    commit = head_commit(tmp, runner)
    _safe_rmtree(dest, repos_dir)
    tmp.rename(dest)
    return SyncResult(
        OK, "cloned" + (f" branch {repo['branch']}" if repo["branch"] else ""), commit
    )


def record_sync(conn: sqlite3.Connection, repo_id: int, result: SyncResult) -> None:
    conn.execute(
        "UPDATE tf_repos SET synced_at=?, sync_status=?, sync_detail=?, "
        "commit_sha = CASE WHEN ? THEN ? ELSE commit_sha END WHERE id=?",
        (_now(), result.status, result.detail, int(result.ok), result.commit, repo_id),
    )


# -- analysis runs -----------------------------------------------------------------------


def run_dir_for(out_dir: Path, repo: dict[str, Any], run_id: int) -> Path:
    return out_dir / f"{repo['id']}-{_slug(repo['name'])}" / str(run_id)


def env_scope(env: dict[str, Any] | None) -> str | None:
    """The directory an environment covers: detected environments are bound to their
    root(s); manual ones (and '.') cover the whole analyzed directory."""
    if not env or env.get("kinds", "manual") in ("", "manual"):
        return None
    scope = env.get("root_rel") or "."
    return None if scope == "." else scope


def create_run(
    conn: sqlite3.Connection,
    repo: dict[str, Any],
    env: dict[str, Any] | None,
    out_dir: Path,
    decrypt: Decrypt | None,
) -> int:
    """Analyze ``repo`` (with ``env``'s tfvars and variables) read-only; write model.json,
    report.md and overview.md to ``out/<repo>/<run>/`` and record the run. Returns the run
    id. A run whose discovery finds nothing to analyze is recorded as failed, with the
    reason and a listing of what the repository contains (its model is still written, so
    the run opens and shows every file). Precondition errors raise ValueError."""
    from .handoff import render_handoff
    from .output import dumps, listing_text, run_analysis
    from .overview import build_overview, render_overview_markdown
    from .report import render_report
    from .snapshots import diff_runs, previous_run, store_snapshot
    from .visuals import export_diagrams

    root = analysis_root(repo)
    if not root.is_dir():
        raise ValueError(
            "the repository has not been synced yet"
            if repo["source"] == "git"
            else "the directory does not exist"
        )
    var_files = list(env["var_files"]) if env else []
    for f in var_files:
        if not (root / f).is_file() or not _inside(root / f, root):
            raise ValueError(f"tfvars file {f} not found in the repository")
    analysis = run_analysis(
        root,
        var_files,
        env_vars(env, decrypt) if env else {},
        scope=env_scope(env),
        git_root=Path(repo["path"]),
    )
    listing = listing_text(analysis.model["discovery"]["inventory"])
    error = f"{analysis.failure}\n{listing}" if analysis.failed else ""
    run_id = _insert_run(
        conn,
        repo,
        env,
        summary=analysis.model["summary"],
        root=root,
        status=FAILED if analysis.failed else OK,
        error=error,
    )
    run_dir = run_dir_for(out_dir, repo, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    model_path = run_dir / "model.json"
    model_path.write_text(dumps(analysis.model), encoding="utf-8")
    title = repo["name"] + (f" ({env['env']})" if env else "")
    (run_dir / "report.md").write_text(
        render_report(analysis.model, title, analysis.repo.lines), encoding="utf-8"
    )
    (run_dir / "overview.md").write_text(
        render_overview_markdown(
            build_overview(analysis.model, title, analysis.repo.lines), listing
        ),
        encoding="utf-8",
    )
    conn.execute("UPDATE runs SET model_path=? WHERE id=?", (str(model_path), run_id))
    store_snapshot(conn, run_id, analysis.model)
    run = get_run(conn, run_id)
    assert run is not None
    prev = previous_run(conn, run)
    changes = None
    if prev is not None:
        try:
            changes = diff_runs(conn, prev, run)
        except ValueError as exc:  # the previous run's model is gone: no diff, say so
            log.warning("run %s: no diff against run %s: %s", run_id, prev["id"], exc)
    export_diagrams(analysis.model, run_dir / "diagrams", title)
    (run_dir / "handoff.md").write_text(
        render_handoff(analysis.model, title, analysis.repo.lines, changes, run["commit_sha"]),
        encoding="utf-8",
    )
    return run_id


def _insert_run(
    conn: sqlite3.Connection,
    repo: dict[str, Any],
    env: dict[str, Any] | None,
    *,
    summary: dict[str, Any],
    root: Path | None,
    status: str,
    error: str,
) -> int:
    commit = repo.get("commit_sha", "")
    if repo["source"] == "local":
        commit = head_commit(Path(repo["path"])) if Path(repo["path"]).is_dir() else ""
    cur = conn.execute(
        "INSERT INTO runs(ts, repo_id, repo, env_id, env, commit_sha, summary, model_path, "
        "root_path, status, error) VALUES(?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
        (
            _now(),
            repo["id"],
            repo["name"],
            env["id"] if env else None,
            env["env"] if env else "",
            commit,
            json.dumps(summary, sort_keys=True),
            str(root) if root else "",
            status,
            error,
        ),
    )
    return int(cur.lastrowid or 0)


def record_failed_run(
    conn: sqlite3.Connection, repo: dict[str, Any], env: dict[str, Any] | None, reason: str
) -> int:
    """Record a run that failed before producing a model (no files on disk)."""
    try:
        root: Path | None = analysis_root(repo)
    except ValueError:
        root = None
    return _insert_run(conn, repo, env, summary={}, root=root, status=FAILED, error=reason)


def _run_row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    try:
        out["summary"] = json.loads(r["summary"] or "{}")
    except ValueError:
        out["summary"] = {}
    s = out["summary"]
    out["node_count"] = sum((s.get("nodes") or {}).values())
    out["edge_count"] = sum((s.get("edges") or {}).values())
    error = out.get("error") or ""
    out["reason"], _, out["error_detail"] = error.partition("\n")
    out["status"] = out.get("status") or OK
    return out


def list_runs(
    conn: sqlite3.Connection, repo_id: int | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM runs", []
    if repo_id is not None:
        sql += " WHERE repo_id=?"
        args.append(repo_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [_run_row(r) for r in conn.execute(sql, args)]


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_row(row) if row else None


def _remove_run_files(model_path: str, out_dir: Path) -> None:
    if model_path:
        _safe_rmtree(Path(model_path).parent, out_dir)


def delete_run(conn: sqlite3.Connection, run_id: int, out_dir: Path) -> bool:
    run = get_run(conn, run_id)
    if run is None:
        return False
    _remove_run_files(run["model_path"], out_dir)
    return conn.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount > 0


def prune_runs(conn: sqlite3.Connection, out_dir: Path, keep: int, repo_id: int, env: str) -> int:
    """Keep the newest ``keep`` runs of one repository x environment; delete the others
    with their out/ directory. Returns the number deleted."""
    old = conn.execute(
        "SELECT id, model_path FROM runs WHERE repo_id=? AND env=? ORDER BY id DESC LIMIT -1 "
        "OFFSET ?",
        (repo_id, env, max(keep, 1)),
    ).fetchall()
    for r in old:
        _remove_run_files(r["model_path"], out_dir)
        conn.execute("DELETE FROM runs WHERE id=?", (r["id"],))
    return len(old)
