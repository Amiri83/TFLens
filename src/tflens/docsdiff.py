"""Docs vs code: what the documentation names that the code does not declare, and the
resources the code declares that no documentation mentions.

Every Markdown file of the repository is read (READMEs, ``docs/`` and any other ``.md``
file, at any depth). A mention is a Terraform address (``aws_s3_bucket.logs``,
``data.aws_iam_policy_document.read``, ``module.network``), a block header in a code
sample (``resource "aws_s3_bucket" "logs"``) or a resource type written as inline code
(`` `aws_sqs_queue` ``). Only types of the providers the code uses count (their prefix:
``aws_``, ``google_``, ...), so prose words are not taken for resources. The code side is
every block declared in the repository's own Terraform (example / test / fixture code and
modules fetched by ``terraform init`` are not part of it), whatever the environment: a
resource only some environments create is still in the code.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from .loader import RepoSource

DOC_EXTENSIONS = (".md", ".markdown")
MAX_DOC_FILES = 200
MAX_LISTED = 300
_TYPE = r"[a-z][a-z0-9]*_[a-z0-9_]*[a-z0-9]"
_ADDRESS_RE = re.compile(rf"(?<![\w.$/-])(data\.)?({_TYPE})\.([A-Za-z_][\w-]*)")
_MODULE_RE = re.compile(r"(?<![\w.$/-])module\.([A-Za-z_][\w-]*)")
_BLOCK_RE = re.compile(rf'\b(resource|data)\s+"({_TYPE})"\s+"([\w-]+)"')
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_BARE_TYPE_RE = re.compile(rf"^(?:data\s+)?({_TYPE})$")


def doc_sources(repo: RepoSource) -> list[str]:
    """The Markdown files read for the diff (top-level README first, then by path)."""
    files = [f for f in repo.all_files if f.lower().endswith(DOC_EXTENSIONS)]
    files.sort(
        key=lambda f: (f.count("/"), not posixpath.basename(f).lower().startswith("readme"), f)
    )
    return files[:MAX_DOC_FILES]


def _declared(repo: RepoSource) -> dict[str, Any]:
    """The repository's own blocks: resources, data sources and module calls."""
    remote = repo.remote_dirs
    resources: dict[tuple[str, str], list[dict[str, Any]]] = {}
    data: dict[tuple[str, str], list[dict[str, Any]]] = {}
    modules: dict[str, list[dict[str, Any]]] = {}
    providers: set[str] = set()
    for d in sorted(repo.modules):
        if d in remote:
            continue
        mod = repo.modules[d]
        for key, b in mod.resources.items():
            resources.setdefault(key, []).append(b.loc.to_dict())
        for key, b in mod.data.items():
            data.setdefault(key, []).append(b.loc.to_dict())
        for name, b in mod.module_calls.items():
            modules.setdefault(name, []).append(b.loc.to_dict())
        providers |= {p.labels[0] for p in mod.providers if p.labels}
        for tb in mod.terraform:
            for rp in tb.blocks.get("required_providers", []):
                providers |= set(rp.attrs)
    prefixes = {t.split("_", 1)[0] for t, _ in [*resources, *data]} | providers
    return {"resources": resources, "data": data, "modules": modules, "prefixes": prefixes}


def _mentions(repo: RepoSource, files: list[str], prefixes: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def known(rtype: str) -> bool:
        return rtype.split("_", 1)[0] in prefixes

    for f in files:
        for n, line in enumerate(repo.lines(f), 1):
            loc = {"file": f, "start_line": n, "end_line": n}
            found: list[tuple[str, str, str]] = []  # kind, type / name, name
            for m in _BLOCK_RE.finditer(line):
                found.append(
                    ("data" if m.group(1) == "data" else "resource", m.group(2), m.group(3))
                )
            for m in _ADDRESS_RE.finditer(line):
                if known(m.group(2)):
                    found.append(("data" if m.group(1) else "resource", m.group(2), m.group(3)))
            for m in _MODULE_RE.finditer(line):
                found.append(("module", "module", m.group(1)))
            for m in _CODE_SPAN_RE.finditer(line):
                bare = _BARE_TYPE_RE.match(m.group(1).strip())
                if bare and known(bare.group(1)):
                    found.append(("type", bare.group(1), ""))
            for kind, rtype, name in dict.fromkeys(found):
                out.append({"kind": kind, "type": rtype, "name": name, "location": loc})
    return out


def _ref(kind: str, rtype: str, name: str) -> str:
    if kind == "module":
        return f"module.{name}"
    if kind == "type":
        return rtype
    return f"{'data.' if kind == 'data' else ''}{rtype}.{name}"


def docs_diff(repo: RepoSource) -> dict[str, Any]:
    """The ``docs_diff`` section of model.json: ``documented_missing`` (named in the docs,
    not declared in the code), ``undocumented`` (resources declared, mentioned nowhere: not
    by address, not by type) and ``documented`` (the mentions that match), each with
    file:line."""
    files = doc_sources(repo)
    code = _declared(repo)
    mentions = _mentions(repo, files, code["prefixes"])
    types = {t for t, _ in code["resources"]} | {t for t, _ in code["data"]}
    missing: dict[str, dict[str, Any]] = {}
    documented: dict[str, dict[str, Any]] = {}
    for m in mentions:
        kind, rtype, name = m["kind"], m["type"], m["name"]
        if kind == "module":
            present = name in code["modules"]
        elif kind == "type":
            present = rtype in types
        else:
            present = (rtype, name) in code["data" if kind == "data" else "resources"]
        ref = _ref(kind, rtype, name)
        bucket = documented if present else missing
        entry = bucket.setdefault(ref, {"ref": ref, "kind": kind, "locations": []})
        if m["location"] not in entry["locations"]:
            entry["locations"].append(m["location"])
    named = {(m["type"], m["name"]) for m in mentions if m["kind"] == "resource"}
    named_types = {m["type"] for m in mentions if m["kind"] == "type"}
    undocumented = []
    for (rtype, name), locs in sorted(code["resources"].items()):
        if (rtype, name) in named or rtype in named_types:
            continue
        undocumented.append(
            {"ref": f"{rtype}.{name}", "type": rtype, "locations": locs, "location": locs[0]}
        )
    status, note = "ok", ""
    if not files:
        status, note = "no-docs", "no Markdown file in the repository: nothing to compare"
        undocumented = []  # nothing is documented: listing every resource says nothing
    return {
        "status": status,
        "note": note,
        "files": files,
        "mentions": len(mentions),
        "documented": [documented[k] for k in sorted(documented)],
        "documented_missing": [missing[k] for k in sorted(missing)][:MAX_LISTED],
        "undocumented": undocumented[:MAX_LISTED],
        "counts": {
            "documented": len(documented),
            "documented_missing": len(missing),
            "undocumented": len(undocumented),
            "resources": len(code["resources"]),
        },
    }
