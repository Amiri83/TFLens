"""Saved canvas layouts: where the user left each node of the Graph and of each diagram.

One row per (run, view) in the ``run_layouts`` table: ``positions`` is a JSON object
``{node id: {"x": .., "y": ..}}`` (model units). A recorded run's layouts live in the
app's database (``run_id`` = the run); ``tflens serve --dir`` keeps them in a database
next to its model.json (``run_id`` 0), created on the first save only.

Positions are UI state, not analysis results: a read-only (recorded) run still saves
them, and a node no longer in the model is simply never drawn.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db

LAYOUT_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_layouts (
    run_id     INTEGER NOT NULL,
    view       TEXT NOT NULL,           -- graph | architecture | module-tree | flow-3@root ...
    positions  TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, view)
);
"""
MAX_NODES = 20000
MAX_ID = 512
MAX_COORD = 1e7
VIEW_RE = re.compile(r"[\w.:@/\-]{1,200}")


class LayoutError(ValueError):
    """A layout that cannot be saved (bad view name or positions)."""


def check_view(view: str) -> str:
    if not VIEW_RE.fullmatch(view or ""):
        raise LayoutError(f"bad view name {view[:40]!r}")
    return view


def clean_positions(raw: Any) -> dict[str, dict[str, float]]:
    """``{id: {x, y}}`` with finite numbers only (rounded to 0.1)."""
    if not isinstance(raw, dict):
        raise LayoutError("positions must be an object")
    if len(raw) > MAX_NODES:
        raise LayoutError(f"too many positions (max {MAX_NODES})")
    out: dict[str, dict[str, float]] = {}
    for key, p in raw.items():
        if not isinstance(key, str) or not key or len(key) > MAX_ID:
            raise LayoutError("bad node id")
        if not isinstance(p, dict):
            raise LayoutError(f"bad position for {key[:40]!r}")
        xy = []
        for axis in ("x", "y"):
            v = p.get(axis)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise LayoutError(f"bad {axis} for {key[:40]!r}")
            if abs(v) > MAX_COORD:
                raise LayoutError(f"{axis} out of range for {key[:40]!r}")
            xy.append(round(float(v), 1))
        out[key] = {"x": xy[0], "y": xy[1]}
    return out


class SqliteLayouts:
    """The layouts of one run in a SQLite database.

    ``managed``: the app database (created / migrated by :mod:`tflens.db`, the table
    comes with its migrations); otherwise a small database of its own, created on the
    first save."""

    def __init__(self, db_path: Path | str, run_id: int = 0, *, managed: bool = False) -> None:
        self.db_path = Path(db_path)
        self.run_id = run_id
        self.managed = managed

    def _ensure(self) -> None:
        if self.managed:
            db.ensure_db(self.db_path)
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with db.closing(self.db_path) as conn:
            conn.executescript(LAYOUT_SCHEMA)

    def get(self, view: str) -> dict[str, dict[str, float]] | None:
        check_view(view)
        if not db.db_exists(self.db_path):
            return None
        if self.managed:
            db.ensure_db(self.db_path)
        with db.closing(self.db_path) as conn:
            try:
                row = conn.execute(
                    "SELECT positions FROM run_layouts WHERE run_id = ? AND view = ?",
                    (self.run_id, view),
                ).fetchone()
            except db.sqlite3.OperationalError:  # no table yet: nothing saved
                return None
        if row is None:
            return None
        try:
            return clean_positions(json.loads(row["positions"]))
        except (ValueError, LayoutError):
            return None

    def save(self, view: str, positions: Any) -> dict[str, dict[str, float]]:
        check_view(view)
        clean = clean_positions(positions)
        self._ensure()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with db.closing(self.db_path) as conn:
            conn.execute(
                "INSERT INTO run_layouts(run_id, view, positions, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(run_id, view) DO UPDATE SET positions = excluded.positions, "
                "updated_at = excluded.updated_at",
                (self.run_id, view, json.dumps(clean, sort_keys=True), now),
            )
        return clean

    def reset(self, view: str) -> bool:
        check_view(view)
        if not db.db_exists(self.db_path):
            return False
        with db.closing(self.db_path) as conn:
            try:
                cur = conn.execute(
                    "DELETE FROM run_layouts WHERE run_id = ? AND view = ?", (self.run_id, view)
                )
            except db.sqlite3.OperationalError:
                return False
            return cur.rowcount > 0

    def views(self) -> list[str]:
        if not db.db_exists(self.db_path):
            return []
        with db.closing(self.db_path) as conn:
            try:
                rows = conn.execute(
                    "SELECT view FROM run_layouts WHERE run_id = ? ORDER BY view", (self.run_id,)
                ).fetchall()
            except db.sqlite3.OperationalError:
                return []
        return [r["view"] for r in rows]
