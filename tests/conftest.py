from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import pytest

from tflens.output import run_analysis

FIXTURES = Path(__file__).parent / "fixtures"


@cache
def _model(name: str) -> dict[str, Any]:
    return run_analysis(FIXTURES / name).model


class Model:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.nodes = {n["id"]: n for n in data["nodes"]}

    def edges(
        self, kind: str, source: str | None = None, target: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            e
            for e in self.data["edges"]
            if e["kind"] == kind
            and (source is None or e["source"] == source)
            and (target is None or e["target"] == target)
        ]

    def edge(self, kind: str, source: str, target: str) -> dict[str, Any]:
        found = self.edges(kind, source, target)
        assert len(found) == 1, f"expected one {kind} {source} -> {target}, got {found}"
        return found[0]

    def unknowns(self, subject: str) -> list[dict[str, Any]]:
        return [u for u in self.data["unknowns"] if u["subject"] == subject]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Never touch the real ~/.tflens: HOME and TFLENS_HOME point into a temp dir."""
    user_home = tmp_path_factory.mktemp("user-home")
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.delenv("TFLENS_HOME", raising=False)
    monkeypatch.delenv("TFLENS_SECRET_KEY", raising=False)
    return user_home


@pytest.fixture
def home(tmp_path):
    """A TFLens data directory (not created yet)."""
    return tmp_path / "tflens-home"


@pytest.fixture
def db_path(tmp_path):
    from tflens.db import init_db

    path = tmp_path / "tflens.db"
    init_db(path)
    return path


@pytest.fixture
def load():
    def _load(name: str) -> Model:
        return Model(_model(name))

    return _load


def span(evidence: dict[str, Any]) -> tuple[str, int, int]:
    return evidence["file"], evidence["start_line"], evidence["end_line"]
