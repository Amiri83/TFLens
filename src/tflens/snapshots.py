"""Per-run snapshots in SQLite and the diff between two runs (the Changes page).

A run's snapshot is what its model found: files, module calls (source + version),
resources (with a fingerprint of their properties and attributes), edges, environments
and backends, stored in the ``run_*`` tables (``db.SNAPSHOT_SCHEMA``) keyed by the run;
the run itself records the repository and the commit. Runs recorded before snapshots
existed are back-filled from their model.json on first use.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .snippets import location_label

TABLES = ("run_files", "run_modules", "run_resources", "run_edges", "run_envs", "run_backends")
MAX_PROPERTY_CHANGES = 12


def _fp(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def _where(loc: dict[str, Any] | None) -> str:
    return location_label(loc) if loc else ""


def snapshot(model: dict[str, Any]) -> dict[str, dict[Any, dict[str, Any]]]:
    """The rows of a model's snapshot, keyed like the tables' primary keys (pure)."""
    analyzed = set(model["files"])
    crawled = model.get("crawl") or {}
    docs = {d["path"] for d in crawled.get("docs", [])}
    ci = {c["path"] for c in crawled.get("ci", [])}
    all_files = set((model.get("discovery") or {}).get("all_files", [])) | analyzed
    files = {}
    for f in sorted(all_files):
        kind = "analyzed" if f in analyzed else "doc" if f in docs else "ci" if f in ci else "other"
        files[f] = {"path": f, "kind": kind}
    structure = model.get("structure") or {"roots": [], "environments": []}
    modules = {}
    backends = {}
    for r in structure["roots"]:
        for m in r["modules"]:
            modules[(r["dir"], m["address"])] = {
                "root": r["dir"],
                "address": m["address"],
                "kind": m["kind"],
                "source": m["source"] or "",
                "version": m["version"] or "",
                "dir": m["dir"] or "",
                "location": _where(m["location"]),
            }
        b = r["backend"]
        backends[r["dir"]] = {
            "root": r["dir"],
            "type": b["type"],
            "settings": {s["key"]: s["value"] for s in b["settings"]},
        }
    resources = {}
    for n in model["nodes"]:
        if n["kind"] not in ("resource", "data"):
            continue
        resources[n["id"]] = {
            "node_id": n["id"],
            "type": n["type"],
            "service": n["service"] or "",
            "module": n["module"] or "",
            "location": _where(n["location"]),
            "fingerprint": _fp({"p": n["properties"], "a": n["attributes"]}),
            "properties": n["properties"],
        }
    edges = {
        e["id"]: {
            "edge_id": e["id"],
            "kind": e["kind"],
            "source": e["source"],
            "target": e["target"],
            "label": e["label"],
            "fingerprint": _fp({"l": e["label"], "a": e["attributes"], "v": e["via"]}),
        }
        for e in model["edges"]
    }
    envs = {}
    for e in structure["environments"]:
        envs[(e["root"], e["name"])] = {
            "root": e["root"],
            "env": e["name"],
            "kinds": ",".join(e["kinds"]),
            "var_files": list(e["var_files"]),
        }
    return {
        "files": files,
        "modules": modules,
        "resources": resources,
        "edges": edges,
        "envs": envs,
        "backends": backends,
    }


# ------------------------------------------------------------------ storage


def store_snapshot(conn: sqlite3.Connection, run_id: int, model: dict[str, Any]) -> None:
    snap = snapshot(model)
    for table in TABLES:
        conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
    conn.executemany(
        "INSERT INTO run_files(run_id, path, kind) VALUES(?, ?, ?)",
        [(run_id, f["path"], f["kind"]) for f in snap["files"].values()],
    )
    conn.executemany(
        "INSERT INTO run_modules(run_id, root, address, kind, source, version, dir, location) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                m["root"],
                m["address"],
                m["kind"],
                m["source"],
                m["version"],
                m["dir"],
                m["location"],
            )
            for m in snap["modules"].values()
        ],
    )
    conn.executemany(
        "INSERT INTO run_resources(run_id, node_id, type, service, module, location, fingerprint, "
        "properties) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                r["node_id"],
                r["type"],
                r["service"],
                r["module"],
                r["location"],
                r["fingerprint"],
                json.dumps(r["properties"], sort_keys=True, default=str),
            )
            for r in snap["resources"].values()
        ],
    )
    conn.executemany(
        "INSERT INTO run_edges(run_id, edge_id, kind, source, target, label, fingerprint) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                e["edge_id"],
                e["kind"],
                e["source"],
                e["target"],
                e["label"],
                e["fingerprint"],
            )
            for e in snap["edges"].values()
        ],
    )
    conn.executemany(
        "INSERT INTO run_envs(run_id, root, env, kinds, var_files) VALUES(?, ?, ?, ?, ?)",
        [
            (run_id, e["root"], e["env"], e["kinds"], json.dumps(e["var_files"]))
            for e in snap["envs"].values()
        ],
    )
    conn.executemany(
        "INSERT INTO run_backends(run_id, root, type, settings) VALUES(?, ?, ?, ?)",
        [
            (run_id, b["root"], b["type"], json.dumps(b["settings"], sort_keys=True))
            for b in snap["backends"].values()
        ],
    )


def has_snapshot(conn: sqlite3.Connection, run_id: int) -> bool:
    return any(
        conn.execute(f"SELECT 1 FROM {t} WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
        for t in ("run_files", "run_resources", "run_modules")
    )


def ensure_snapshot(conn: sqlite3.Connection, run: dict[str, Any]) -> bool:
    """Back-fill the snapshot of an older run from its model.json; False if impossible."""
    if has_snapshot(conn, run["id"]):
        return True
    if not run.get("model_path"):
        return False
    try:
        model = json.loads(Path(run["model_path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    store_snapshot(conn, run["id"], model)
    return True


def load_snapshot(conn: sqlite3.Connection, run_id: int) -> dict[str, dict[Any, dict[str, Any]]]:
    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, (run_id,))]

    out: dict[str, dict[Any, dict[str, Any]]] = {}
    out["files"] = {r["path"]: r for r in rows("SELECT path, kind FROM run_files WHERE run_id=?")}
    out["modules"] = {
        (r["root"], r["address"]): r
        for r in rows(
            "SELECT root, address, kind, source, version, dir, location FROM run_modules WHERE run_id=?"
        )
    }
    out["resources"] = {}
    for r in rows(
        "SELECT node_id, type, service, module, location, fingerprint, properties "
        "FROM run_resources WHERE run_id=?"
    ):
        r["properties"] = json.loads(r["properties"] or "{}")
        out["resources"][r["node_id"]] = r
    out["edges"] = {
        r["edge_id"]: r
        for r in rows(
            "SELECT edge_id, kind, source, target, label, fingerprint FROM run_edges WHERE run_id=?"
        )
    }
    out["envs"] = {}
    for r in rows("SELECT root, env, kinds, var_files FROM run_envs WHERE run_id=?"):
        r["var_files"] = json.loads(r["var_files"] or "[]")
        out["envs"][(r["root"], r["env"])] = r
    out["backends"] = {}
    for r in rows("SELECT root, type, settings FROM run_backends WHERE run_id=?"):
        r["settings"] = json.loads(r["settings"] or "{}")
        out["backends"][r["root"]] = r
    return out


def previous_run(conn: sqlite3.Connection, run: dict[str, Any]) -> dict[str, Any] | None:
    """The latest earlier run of the same repository and environment that produced a model."""
    row = conn.execute(
        "SELECT * FROM runs WHERE repo_id=? AND env=? AND id<? AND model_path != '' "
        "ORDER BY id DESC LIMIT 1",
        (run["repo_id"], run["env"], run["id"]),
    ).fetchone()
    return dict(row) if row else None


def runs_at_commit(conn: sqlite3.Connection, repo_id: int, commit: str) -> list[dict[str, Any]]:
    """Runs of a repository at one commit (newest first)."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM runs WHERE repo_id=? AND commit_sha=? ORDER BY id DESC",
            (repo_id, commit),
        )
    ]


# ------------------------------------------------------------------ diff


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 80 else text[:79] + "…"


def _prop_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    out = []
    for k in sorted(set(old) | set(new)):
        if k not in new:
            out.append(f"{k} removed (was {_short(old[k])})")
        elif k not in old:
            out.append(f"{k} added: {_short(new[k])}")
        elif old[k] != new[k]:
            out.append(f"{k}: {_short(old[k])} → {_short(new[k])}")
    if len(out) > MAX_PROPERTY_CHANGES:
        out = out[:MAX_PROPERTY_CHANGES] + [f"… {len(out) - MAX_PROPERTY_CHANGES} more"]
    return out


def _section(old: dict[Any, Any], new: dict[Any, Any], changed) -> dict[str, list[Any]]:
    added = [new[k] for k in sorted(set(new) - set(old), key=str)]
    removed = [old[k] for k in sorted(set(old) - set(new), key=str)]
    modified = []
    for k in sorted(set(old) & set(new), key=str):
        changes = changed(old[k], new[k])
        if changes:
            modified.append(
                {
                    "key": k if isinstance(k, str) else " / ".join(k),
                    "item": new[k],
                    "changes": changes,
                }
            )
    return {"added": added, "removed": removed, "changed": modified}


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Added / removed / changed files, modules (source, version), resources, edges,
    environments and backends between two snapshots (pure; deterministic order)."""

    def resource(o: dict[str, Any], n: dict[str, Any]) -> list[str]:
        if o["fingerprint"] == n["fingerprint"] and o["type"] == n["type"]:
            return []
        return _prop_changes(o["properties"], n["properties"]) or ["attributes changed"]

    def edge(o: dict[str, Any], n: dict[str, Any]) -> list[str]:
        if o["fingerprint"] == n["fingerprint"]:
            return []
        return (
            [f"label: {o['label']} → {n['label']}"]
            if o["label"] != n["label"]
            else ["attributes changed"]
        )

    def module(o: dict[str, Any], n: dict[str, Any]) -> list[str]:
        out = []
        for k in ("version", "source", "kind", "dir"):
            if o[k] != n[k]:
                out.append(f"{k}: {o[k] or '(none)'} → {n[k] or '(none)'}")
        return out

    def env(o: dict[str, Any], n: dict[str, Any]) -> list[str]:
        out = []
        if o["kinds"] != n["kinds"]:
            out.append(f"found by: {o['kinds']} → {n['kinds']}")
        if o["var_files"] != n["var_files"]:
            out.append(
                f"var files: {', '.join(o['var_files']) or '(none)'} → {', '.join(n['var_files']) or '(none)'}"
            )
        return out

    def backend(o: dict[str, Any], n: dict[str, Any]) -> list[str]:
        out = [f"type: {o['type']} → {n['type']}"] if o["type"] != n["type"] else []
        return out + _prop_changes(o["settings"], n["settings"])

    def file(o: dict[str, Any], n: dict[str, Any]) -> list[str]:
        return [f"{o['kind']} → {n['kind']}"] if o["kind"] != n["kind"] else []

    out = {
        "resources": _section(old["resources"], new["resources"], resource),
        "edges": _section(old["edges"], new["edges"], edge),
        "modules": _section(old["modules"], new["modules"], module),
        "envs": _section(old["envs"], new["envs"], env),
        "backends": _section(old["backends"], new["backends"], backend),
        "files": _section(old["files"], new["files"], file),
    }
    out["versions"] = [
        c for c in out["modules"]["changed"] if any(x.startswith("version:") for x in c["changes"])
    ]
    out["counts"] = {
        k: {kind: len(v[kind]) for kind in ("added", "removed", "changed")}
        for k, v in out.items()
        if k not in ("versions",)
    }
    out["empty"] = not any(sum(c.values()) for c in out["counts"].values())
    return out


def diff_runs(
    conn: sqlite3.Connection, base: dict[str, Any], head: dict[str, Any]
) -> dict[str, Any]:
    """Diff of run ``head`` against run ``base`` (snapshots back-filled when needed)."""
    for run in (base, head):
        if not ensure_snapshot(conn, run):
            raise ValueError(f"run {run['id']} has no model to compare")
    out = diff_snapshots(load_snapshot(conn, base["id"]), load_snapshot(conn, head["id"]))
    out["base"] = {k: base[k] for k in ("id", "ts", "commit_sha", "env")}
    out["head"] = {k: head[k] for k in ("id", "ts", "commit_sha", "env")}
    return out


def changes_markdown(diff: dict[str, Any], title: str = "Changes") -> str:
    """The diff as Markdown (handoff.md, the CLI)."""
    lines = [f"## {title}", ""]
    if diff.get("base"):
        b, h = diff["base"], diff["head"]
        lines += [
            f"Run {h['id']} ({h['commit_sha'][:12] or 'no commit'}) compared with run {b['id']} "
            f"({b['commit_sha'][:12] or 'no commit'}).",
            "",
        ]
    if diff["empty"]:
        return "\n".join(lines + ["No changes.", ""])
    labels = {
        "resources": (
            "Resources",
            lambda r: (
                f"{r['node_id']} ({r['type']})"
                + (f" `{r['location']}`" if r.get("location") else "")
            ),
        ),
        "edges": ("Edges", lambda e: f"{e['kind']}: {e['source']} → {e['target']} — {e['label']}"),
        "modules": (
            "Modules",
            lambda m: (
                f"{m['root']}: {m['address']} — {m['kind']} {m['source']}"
                + (f" @ {m['version']}" if m["version"] else "")
                + (f" `{m['location']}`" if m.get("location") else "")
            ),
        ),
        "envs": ("Environments", lambda e: f"{e['env']} ({e['root']})"),
        "backends": ("Backends", lambda b: f"{b['root']}: {b['type']}"),
        "files": ("Files", lambda f: f"{f['path']} ({f['kind']})"),
    }
    for key, (heading, fmt) in labels.items():
        sec = diff[key]
        if not any(sec.values()):
            continue
        lines += [f"### {heading}", ""]
        for item in sec["added"]:
            lines.append(f"- added: {fmt(item)}")
        for item in sec["removed"]:
            lines.append(f"- removed: {fmt(item)}")
        for c in sec["changed"]:
            lines.append(f"- changed: {fmt(c['item'])}")
            lines += [f"  - {x}" for x in c["changes"]]
        lines.append("")
    return "\n".join(lines)
