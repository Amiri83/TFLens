"""Filesystem locations used by TFLens.

Everything lives under a single data directory (``TFLENS_HOME`` or ``~/.tflens``)
so the app is fully self-contained and easy to wipe. Only ``tflens serve`` without
``--dir`` uses it; ``tflens --dir`` and ``tflens serve --dir`` never touch it.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "tflens.db"

    @property
    def key_path(self) -> Path:
        return self.home / "secret.key"

    @property
    def flask_secret_path(self) -> Path:
        return self.home / "flask.secret"

    @property
    def default_log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def repos_dir(self) -> Path:
        """Shallow clones of git repositories (``repos/<id>``)."""
        return self.home / "repos"

    @property
    def out_dir(self) -> Path:
        """Analysis runs: ``out/<repo>/<run>/model.json`` (never stored in the DB)."""
        return self.home / "out"

    def ensure(self) -> AppPaths:
        self.home.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.home, 0o700)
        return self


def default_paths(home: str | os.PathLike[str] | None = None) -> AppPaths:
    if home is None:
        home = os.environ.get("TFLENS_HOME") or Path.home() / ".tflens"
    return AppPaths(Path(home).expanduser().resolve())
