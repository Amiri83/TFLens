"""Schema creation, migrations (version table + added columns) and lazy creation."""

import sqlite3

from tflens.db import (
    SCHEMA_VERSION,
    closing,
    db_exists,
    ensure_db,
    init_db,
    reading,
    schema_version,
    writing,
)

TABLES = {"settings", "accounts", "tf_repos", "tf_repo_envs", "runs", "schema_version"}


def _tables(path) -> set[str]:
    with closing(path) as c:
        return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_has_all_tables_and_current_version(tmp_path):
    p = tmp_path / "fresh.db"
    init_db(p)
    assert _tables(p) >= TABLES
    with closing(p) as c:
        assert schema_version(c) == SCHEMA_VERSION
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    init_db(p)  # idempotent: no second version row
    with closing(p) as c:
        assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == SCHEMA_VERSION


def test_migrates_an_iplens_style_database_in_place(tmp_path):
    """A database with IPLens's original tf_repos / tf_repo_envs (no TFLens columns, no
    runs, no version table) is upgraded without losing rows."""
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE tf_repos (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
            added_at TEXT NOT NULL, discovered_at TEXT);
        CREATE TABLE tf_repo_envs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id INTEGER NOT NULL REFERENCES tf_repos(id) ON DELETE CASCADE,
            root_rel TEXT NOT NULL, env TEXT NOT NULL, UNIQUE (repo_id, root_rel, env));
        INSERT INTO settings VALUES ('log_dir', '/srv/example-logs');
        INSERT INTO tf_repos(path, added_at) VALUES ('/srv/repo-a', '2026-01-01T00:00:00+00:00');
        INSERT INTO tf_repo_envs(repo_id, root_rel, env) VALUES (1, '.', 'dev');
        """
    )
    conn.commit()
    conn.close()

    init_db(p)
    init_db(p)  # idempotent

    assert _tables(p) >= TABLES
    with closing(p) as c:
        repo = c.execute("SELECT * FROM tf_repos").fetchone()
        env = c.execute("SELECT * FROM tf_repo_envs").fetchone()
        assert (repo["path"], repo["source"], repo["git_auth"], repo["token_enc"]) == (
            "/srv/repo-a",
            "local",
            "agent",
            "",
        )
        assert (env["env"], env["var_files"], env["extra_vars_keys"]) == ("dev", "[]", "")
        assert c.execute("SELECT value FROM settings").fetchone()[0] == "/srv/example-logs"
        assert schema_version(c) == SCHEMA_VERSION


def test_reads_never_create_the_database(tmp_path):
    p = tmp_path / "lazy" / "tflens.db"
    with reading(p) as conn:
        assert conn is None
    assert not p.exists() and not p.parent.exists()
    with writing(p) as conn:
        conn.execute("INSERT INTO settings VALUES ('k', 'v')")
    assert db_exists(p)
    with reading(p) as conn:
        assert conn.execute("SELECT value FROM settings").fetchone()[0] == "v"


def test_ensure_db_recreates_a_deleted_database(tmp_path):
    p = tmp_path / "tflens.db"
    ensure_db(p)
    p.unlink()
    ensure_db(p)
    assert _tables(p) >= TABLES


def test_runs_keep_history_when_a_repo_is_deleted(db_path):
    with closing(db_path) as c:
        c.execute("INSERT INTO tf_repos(path, added_at, name) VALUES ('/r', 'now', 'repo-a')")
        c.execute(
            "INSERT INTO runs(ts, repo_id, repo, env, model_path) "
            "VALUES ('now', 1, 'repo-a', 'dev', '/x/model.json')"
        )
        c.execute("DELETE FROM tf_repos WHERE id=1")
        row = c.execute("SELECT repo_id, repo, env FROM runs").fetchone()
    assert tuple(row) == (None, "repo-a", "dev")
