"""Filesystem locations used by TFLens.

Everything lives under a single data directory (``TFLENS_HOME`` or ``~/.tflens``)
so the app is fully self-contained and easy to wipe. Only ``tflens serve`` without
``--dir`` uses it; ``tflens --dir`` and ``tflens serve --dir`` never touch it.

The DadsOps environment variables shared by every tool only supply defaults, read when
they are needed and never stored: ``DADSOPS_LOG_DIR`` is the default log directory (the
Settings page's log directory wins over it), ``AWS_PROFILE`` / ``AWS_REGION`` are the
default named profile and region of a new account (see :func:`aws_env_defaults`).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LOG_DIR_ENV = "DADSOPS_LOG_DIR"
PROFILE_ENV = "AWS_PROFILE"
REGION_ENV = "AWS_REGION"


def env_value(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The stripped value of an environment variable ('' when unset or blank)."""
    return (os.environ if environ is None else environ).get(name, "").strip()


@dataclass(frozen=True)
class AwsEnvDefaults:
    """The default named profile / region from ``AWS_PROFILE`` / ``AWS_REGION``
    ('' when unset). Only names, never credentials."""

    profile: str = ""
    region: str = ""


def aws_env_defaults(environ: Mapping[str, str] | None = None) -> AwsEnvDefaults:
    return AwsEnvDefaults(env_value(PROFILE_ENV, environ), env_value(REGION_ENV, environ))


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
        """``DADSOPS_LOG_DIR`` when set, else ``<home>/logs``; a log directory saved in the
        settings overrides it (``web.log_dir_for``)."""
        env = env_value(LOG_DIR_ENV)
        return Path(env).expanduser() if env else self.home / "logs"

    @property
    def default_log_dir_source(self) -> str:
        """The environment variable :attr:`default_log_dir` comes from ('' = built in)."""
        return LOG_DIR_ENV if env_value(LOG_DIR_ENV) else ""

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
