"""Classify Terraform / Terragrunt module source addresses (no network access).

``local`` (``./x``, ``../x``), ``registry`` (``ns/name/provider``, ``tfr://``), ``git``
(``git::``, ``git@``, ``github.com/...``, ``bitbucket.org/...``) or the go-getter kind
(``s3``, ``gcs``, ``http``, ``hg``); anything else is ``other``. The version is the
call's ``version`` argument for registry modules and the ``?ref=`` of a git URL.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

_REGISTRY_RE = re.compile(
    r"^(?:(?P<host>[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?)/)?"
    r"(?P<ns>[A-Za-z0-9][A-Za-z0-9_-]*)/(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)/"
    r"(?P<provider>[a-z0-9]+)(?://(?P<sub>.*))?$"
)
_FORCED_RE = re.compile(r"^(?P<getter>[a-z0-9]+)::(?P<rest>.+)$")
_GIT_HOSTS = ("github.com/", "bitbucket.org/")


def is_local(source: str) -> bool:
    return source.startswith(("./", "../")) or source in (".", "..")


def _query(source: str, key: str) -> str | None:
    query = urlsplit(source.split("::", 1)[-1]).query if "?" in source else ""
    values = parse_qs(query).get(key)
    return values[0] if values else None


def classify_source(source: str) -> dict[str, Any]:
    """``{"kind", "source", "ref"}`` of a module source address."""
    s = source.strip()
    if is_local(s):
        return {"kind": "local", "source": s, "ref": None}
    if s.startswith("tfr://"):
        return {"kind": "registry", "source": s, "ref": _query(s, "version")}
    forced = _FORCED_RE.match(s)
    if forced:
        getter = forced.group("getter")
        kind = getter if getter in ("git", "hg", "s3", "gcs") else "http"
        if getter not in ("git", "hg", "s3", "gcs", "http", "https"):
            kind = "other"
        return {"kind": kind, "source": s, "ref": _query(s, "ref")}
    if s.startswith("git@") or s.startswith(_GIT_HOSTS):
        return {"kind": "git", "source": s, "ref": _query(s, "ref")}
    if s.startswith(("http://", "https://")):
        return {"kind": "http", "source": s, "ref": None}
    if s.startswith("/"):
        return {"kind": "local", "source": s, "ref": None}
    if _REGISTRY_RE.match(s):
        return {"kind": "registry", "source": s, "ref": None}
    return {"kind": "other", "source": s, "ref": None}
