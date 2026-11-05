"""Classify Terraform / Terragrunt module source addresses (no network access).

``local`` (``./x``, ``../x``), ``registry`` (``ns/name/provider``, ``tfr://``), ``git``
(``git::``, ``git@``, ``github.com/...``, ``bitbucket.org/...``) or the go-getter kind
(``s3``, ``gcs``, ``http``, ``hg``); anything else is ``other``. The version is the
call's ``version`` argument for registry modules and the ``?ref=`` of a git URL.
"""

from __future__ import annotations

import posixpath
import re
from functools import lru_cache
from pathlib import Path
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


_SUBDIR_SOURCE_RE = re.compile(r"^(?:git::)?(?P<repo>.+?)//(?P<sub>[^?]+?)/?(?:\?.*)?$")


def _repo_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git").lower()


@lru_cache(maxsize=64)
def _origin_name(root: Path) -> str | None:
    """The name of the repository a checkout was cloned from (``origin`` of its git config)."""
    for d in [root, *list(root.parents)[:5]]:
        try:
            text = (d / ".git" / "config").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = re.search(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', text)
        return _repo_name(found.group(1)) if found else None
    return None


def same_repo_subdir(source: str, root: Path) -> str | None:
    """The directory of this repository a remote source names: ``git::ssh://host/org/repo.git
    //modules/x?ref=v1`` read from a checkout of ``repo``: the code is here (at this commit, not
    necessarily that ref). None for any other source."""
    found = _SUBDIR_SOURCE_RE.match(source.replace("://", "\0"))
    if found is None:
        return None
    url = found.group("repo").replace("\0", "://")
    origin = _origin_name(root)
    if origin is None or _repo_name(url) != origin:
        return None
    return posixpath.normpath(found.group("sub"))
