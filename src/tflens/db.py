"""SQLite storage: schema, migrations and connection helpers.

The database (``AppPaths.db_path``) is created lazily: :func:`ensure_db` runs on the
first write (a settings, account or repository save). Readers check :func:`db_exists`
first and fall back to defaults, so starting ``tflens serve`` writes nothing, and
``tflens --dir`` / ``tflens serve --dir`` never open it at all.

Analysis results (model.json, report.md) stay on disk under ``out/<repo>/<run>/``;
the ``runs`` table only records their path and summary counts.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- AWS accounts (kept for parity with IPLens; TFLens itself makes no AWS calls). Secrets
-- are Fernet-encrypted (crypto.SecretBox); memory-only credentials are never written here.
CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name      TEXT NOT NULL DEFAULT '',
    region            TEXT NOT NULL,
    auth_mode         TEXT NOT NULL,          -- env | profile | keys | temporary
    profile           TEXT NOT NULL DEFAULT '',
    access_key_id     TEXT NOT NULL DEFAULT '',
    secret_enc        TEXT,                   -- encrypted secret access key
    session_token_enc TEXT,                   -- encrypted session token (temporary)
    expires_at        TEXT,                   -- ISO UTC expiry of temporary credentials
    memory_only       INTEGER NOT NULL DEFAULT 0,
    aws_account_id    TEXT,                   -- AWS account id of the first successful connect
    last_seen_account_id TEXT                 -- AWS account id of the latest successful connect
);

-- Terraform repositories (tfrepo.py). path is the directory analyzed: the local
-- directory, or the shallow clone under repos/<id> for git sources. A git token is
-- Fernet-encrypted (token_enc) and never shown, logged or written in clear.
CREATE TABLE IF NOT EXISTS tf_repos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    added_at      TEXT NOT NULL,
    discovered_at TEXT,
    name          TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'local',  -- local | git
    url           TEXT NOT NULL DEFAULT '',       -- git: clone URL (https / ssh, no userinfo)
    branch        TEXT NOT NULL DEFAULT '',       -- git: '' = the remote's default branch
    subdir        TEXT NOT NULL DEFAULT '',       -- analyzed sub-directory ('' = top)
    git_auth      TEXT NOT NULL DEFAULT 'agent',  -- none | agent | key | token
    ssh_key_path  TEXT NOT NULL DEFAULT '',       -- git_auth=key: private key file
    token_user    TEXT NOT NULL DEFAULT '',       -- git_auth=token: HTTP user name
    token_enc     TEXT NOT NULL DEFAULT '',       -- git_auth=token: encrypted token
    synced_at     TEXT,
    sync_status   TEXT NOT NULL DEFAULT '',       -- '' | ok | failed
    sync_detail   TEXT NOT NULL DEFAULT '',
    commit_sha    TEXT NOT NULL DEFAULT ''        -- HEAD of the last sync ('' = not git)
);

-- Environments of a repository. TFLens adds var_files (JSON list of tfvars paths relative
-- to the analyzed directory) and the extra -var KEY=VALUE lines (Fernet-encrypted, key
-- names in clear) to the IPLens columns.
CREATE TABLE IF NOT EXISTS tf_repo_envs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id        INTEGER NOT NULL REFERENCES tf_repos(id) ON DELETE CASCADE,
    root_rel       TEXT NOT NULL,             -- root directory relative to the repo ('.' = top)
    root_label     TEXT NOT NULL DEFAULT '',  -- root shown (env folders under their parent)
    env            TEXT NOT NULL,
    kinds          TEXT NOT NULL DEFAULT '',  -- how the env was found: dir,tfvars,workspace
    var_file       TEXT NOT NULL DEFAULT '',  -- relative to the root
    backend_config TEXT NOT NULL DEFAULT '',  -- relative to the root (-backend-config file)
    workspace      TEXT NOT NULL DEFAULT '',
    backend        TEXT NOT NULL DEFAULT '',  -- backend type: s3 | remote | cloud | local | ...
    guess_account  TEXT NOT NULL DEFAULT '',
    guess_region   TEXT NOT NULL DEFAULT '',
    account_ref    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    confirmed      INTEGER NOT NULL DEFAULT 0,
    present        INTEGER NOT NULL DEFAULT 1, -- 0: no longer found by the latest discovery
    root_name      TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',  -- '' | ok | failed
    status_detail  TEXT NOT NULL DEFAULT '',
    synced_at      TEXT,
    state_account  TEXT NOT NULL DEFAULT '',
    backend_extra_enc  TEXT NOT NULL DEFAULT '',
    backend_extra_keys TEXT NOT NULL DEFAULT '',
    var_files      TEXT NOT NULL DEFAULT '[]',
    extra_vars_enc  TEXT NOT NULL DEFAULT '',
    extra_vars_keys TEXT NOT NULL DEFAULT '',
    UNIQUE (repo_id, root_rel, env)
);

-- Analysis runs. The model JSON lives on disk at model_path (under out/), never here;
-- summary holds the model's summary counts (JSON).
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    repo_id     INTEGER REFERENCES tf_repos(id) ON DELETE SET NULL,
    repo        TEXT NOT NULL DEFAULT '',   -- repository name, frozen at run time
    env_id      INTEGER REFERENCES tf_repo_envs(id) ON DELETE SET NULL,
    env         TEXT NOT NULL DEFAULT '',   -- environment name, frozen at run time
    commit_sha  TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '{}',
    model_path  TEXT NOT NULL,              -- '' when the run failed before writing a model
    root_path   TEXT NOT NULL DEFAULT '',   -- directory analyzed (evidence snippets)
    status      TEXT NOT NULL DEFAULT 'ok', -- ok | failed
    error       TEXT NOT NULL DEFAULT ''    -- why the run failed (first line: the reason)
);
CREATE INDEX IF NOT EXISTS ix_runs_repo ON runs(repo_id, ts);

-- Applied schema migrations (see MIGRATIONS).
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Seconds a connection waits for another thread's write lock before failing.
BUSY_TIMEOUT = 30.0


def connect(db_path: Path | str) -> sqlite3.Connection:
    """A new connection. Connections are never shared between threads: every request
    opens its own."""
    conn = sqlite3.connect(str(db_path), detect_types=0, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first schema, applied to existing databases: (table, column,
# type). The TFLens tf_repos / tf_repo_envs columns are listed too, so a database with
# IPLens's original tables is upgraded in place.
_ADDED_COLUMNS = (
    ("tf_repos", "name", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "source", "TEXT NOT NULL DEFAULT 'local'"),
    ("tf_repos", "url", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "branch", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "subdir", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "git_auth", "TEXT NOT NULL DEFAULT 'agent'"),
    ("tf_repos", "ssh_key_path", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "token_user", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "token_enc", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "synced_at", "TEXT"),
    ("tf_repos", "sync_status", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "sync_detail", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repos", "commit_sha", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repo_envs", "var_files", "TEXT NOT NULL DEFAULT '[]'"),
    ("tf_repo_envs", "extra_vars_enc", "TEXT NOT NULL DEFAULT ''"),
    ("tf_repo_envs", "extra_vars_keys", "TEXT NOT NULL DEFAULT ''"),
    ("runs", "root_path", "TEXT NOT NULL DEFAULT ''"),
    ("runs", "status", "TEXT NOT NULL DEFAULT 'ok'"),
    ("runs", "error", "TEXT NOT NULL DEFAULT ''"),
)

ACTIVE_ACCOUNT_KEY = "active_account"


def _baseline(conn: sqlite3.Connection) -> None:
    """Version 1: the schema above; nothing to convert."""


# Per-run snapshot of what a run found (snapshots.py), keyed by run (and so by repository +
# commit, both recorded on the run): the Changes page diffs two runs from these rows.
SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_files (
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    path     TEXT NOT NULL,
    kind     TEXT NOT NULL,             -- analyzed | doc | ci | other
    PRIMARY KEY (run_id, path)
);
CREATE TABLE IF NOT EXISTS run_modules (
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    root     TEXT NOT NULL,
    address  TEXT NOT NULL,
    kind     TEXT NOT NULL,             -- local | registry | git | ...
    source   TEXT NOT NULL DEFAULT '',
    version  TEXT NOT NULL DEFAULT '',
    dir      TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',  -- file:line of the call
    PRIMARY KEY (run_id, root, address)
);
CREATE TABLE IF NOT EXISTS run_resources (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_id     TEXT NOT NULL,
    type        TEXT NOT NULL,
    service     TEXT NOT NULL DEFAULT '',
    module      TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,          -- sha256 of properties + attributes (not the location)
    properties  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS run_edges (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    edge_id     TEXT NOT NULL,          -- KIND:source->target#n
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (run_id, edge_id)
);
CREATE TABLE IF NOT EXISTS run_envs (
    run_id    INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    root      TEXT NOT NULL,
    env       TEXT NOT NULL,
    kinds     TEXT NOT NULL DEFAULT '',
    var_files TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, root, env)
);
CREATE TABLE IF NOT EXISTS run_backends (
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    root     TEXT NOT NULL,
    type     TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, root)
);
CREATE INDEX IF NOT EXISTS ix_runs_repo_commit ON runs(repo_id, commit_sha);
"""


def _snapshots(conn: sqlite3.Connection) -> None:
    """Version 2: per-run snapshot tables (files, modules, resources, edges, envs, backends)."""
    conn.executescript(SNAPSHOT_SCHEMA)


# Versioned migrations, applied once each and in order: version -> function(conn).
# Add new ones at the end; never change an applied one.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _baseline, 2: _snapshots}
SCHEMA_VERSION = max(MIGRATIONS)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def init_db(db_path: Path | str) -> None:
    """Create or upgrade the database (idempotent)."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for table, column, ddl in _ADDED_COLUMNS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        current = schema_version(conn)
        for version in sorted(v for v in MIGRATIONS if v > current):
            MIGRATIONS[version](conn)
            conn.execute("INSERT INTO schema_version(version) VALUES(?)", (version,))
    # Holds encrypted secrets: owner-only, like the key file.
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.chmod(f"{db_path}{suffix}", 0o600)


def db_exists(db_path: Path | str) -> bool:
    return Path(db_path).is_file()


_ready: set[str] = set()
_ready_lock = threading.Lock()


def ensure_db(db_path: Path | str) -> Path:
    """Create / upgrade the database on first use in this process; return its path."""
    key = str(Path(db_path).resolve())
    with _ready_lock:
        if key not in _ready or not db_exists(db_path):
            init_db(db_path)
            _ready.add(key)
    return Path(db_path)


@contextmanager
def closing(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def reading(db_path: Path | str) -> Iterator[sqlite3.Connection | None]:
    """A connection for reads, or None while the database does not exist yet (it is
    never created by a read)."""
    if not db_exists(db_path):
        yield None
        return
    ensure_db(db_path)  # upgrade a database written by an older version
    with closing(db_path) as conn:
        yield conn


@contextmanager
def writing(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """A connection for writes; creates the database on the first one."""
    ensure_db(db_path)
    with closing(db_path) as conn:
        yield conn
