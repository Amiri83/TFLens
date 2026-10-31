"""Global application settings (log directory, server, runs) and shared helpers.

Settings live in the ``settings`` table of the lazily created database: while it does
not exist, :meth:`SettingsStore.load` returns the defaults and nothing is written.
Command line flags of ``tflens serve`` override the stored server settings
(:func:`effective_server`).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from .db import ACTIVE_ACCOUNT_KEY, reading, writing

# A practical list; the account form also accepts any free-text region code.
REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "sa-east-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "eu-central-2",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "ap-south-1",
    "ap-south-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-east-1",
    "me-south-1",
    "me-central-1",
    "il-central-1",
    "af-south-1",
)

MAX_DISPLAY_NAME = 64
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
# Runs kept per repository x environment (older ones and their out/ directory are deleted).
DEFAULT_RETENTION_RUNS = 20
MIN_RETENTION_RUNS, MAX_RETENTION_RUNS = 1, 1000


@dataclass
class Settings:
    log_dir: str = ""
    # tflens serve: bind address (CLI flags win; a change applies on the next start).
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_remote: bool = False
    # Analyze every environment of a repository after each successful Sync.
    auto_regenerate: bool = False
    retention_runs: int = DEFAULT_RETENTION_RUNS


def _parse_bounded(value: str | int | None, what: str, unit: str, lo: int, hi: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"the {what} must be a whole number of {unit}") from None
    if not lo <= n <= hi:
        raise ValueError(f"the {what} must be {lo}-{hi} {unit}")
    return n


def parse_port(value: str | int | None) -> int:
    """A TCP port (0 = any free port); ValueError otherwise."""
    return _parse_bounded(value, "port", "(0 = any free port)", 0, 65535)


def parse_retention_runs(value: str | int | None) -> int:
    return _parse_bounded(value, "run retention", "runs", MIN_RETENTION_RUNS, MAX_RETENTION_RUNS)


def parse_host(value: str | None) -> str:
    host = (value or "").strip()
    if not host:
        return DEFAULT_HOST
    if len(host) > 255 or any(c.isspace() or c in "/@?#" for c in host):
        raise ValueError("the host must be a host name or an IP address")
    return host


def mask_key_id(key_id: str) -> str:
    if not key_id:
        return ""
    if len(key_id) <= 8:
        return "*" * len(key_id)
    return key_id[:4] + "*" * (len(key_id) - 8) + key_id[-4:]


class SettingsStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _all(self) -> dict[str, str]:
        with reading(self.db_path) as conn:
            if conn is None:
                return {}
            return {r["key"]: r["value"] or "" for r in conn.execute("SELECT * FROM settings")}

    def _get(self, key: str) -> str:
        return self._all().get(key, "")

    def _set(self, key: str, value: str) -> None:
        with writing(self.db_path) as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def load(self) -> Settings:
        raw = self._all()
        s = Settings(log_dir=raw.get("log_dir", ""))
        # An invalid stored value (edited by hand) falls back to the default.
        with contextlib.suppress(ValueError):
            s.host = parse_host(raw.get("host"))
        with contextlib.suppress(ValueError):
            s.port = parse_port(raw["port"]) if raw.get("port") else DEFAULT_PORT
        with contextlib.suppress(ValueError):
            if raw.get("retention_runs"):
                s.retention_runs = parse_retention_runs(raw["retention_runs"])
        s.allow_remote = raw.get("allow_remote") == "1"
        s.auto_regenerate = raw.get("auto_regenerate") == "1"
        return s

    def save(
        self,
        *,
        log_dir: str = "",
        host: str | None = None,
        port: str | int | None = None,
        allow_remote: bool | None = None,
        auto_regenerate: bool | None = None,
        retention_runs: str | int | None = None,
    ) -> None:
        """Validate and store the settings; ``None`` keeps a value unchanged. Nothing is
        written when a value is invalid (ValueError)."""
        values = {"log_dir": log_dir.strip()}
        if host is not None:
            values["host"] = parse_host(host)
        if port is not None:
            values["port"] = str(parse_port(port))
        if retention_runs is not None:
            values["retention_runs"] = str(parse_retention_runs(retention_runs))
        if allow_remote is not None:
            values["allow_remote"] = "1" if allow_remote else "0"
        if auto_regenerate is not None:
            values["auto_regenerate"] = "1" if auto_regenerate else "0"
        for key, value in values.items():
            self._set(key, value)

    def active_account_id(self) -> int | None:
        value = self._get(ACTIVE_ACCOUNT_KEY)
        return int(value) if value.isdigit() else None

    def set_active_account_id(self, account_id: int | None) -> None:
        self._set(ACTIVE_ACCOUNT_KEY, "" if account_id is None else str(account_id))


def effective_server(
    settings: Settings, host: str | None, port: int | None, allow_remote: bool
) -> tuple[str, int, bool]:
    """(host, port, allow_remote) for ``tflens serve``: CLI flags override the settings
    (``None`` / ``False`` = not given on the command line)."""
    return (
        host if host is not None else settings.host,
        port if port is not None else settings.port,
        allow_remote or settings.allow_remote,
    )
