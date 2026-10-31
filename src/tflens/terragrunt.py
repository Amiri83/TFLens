"""Terragrunt units, resolved statically (terragrunt is never run).

A *unit* is a directory with a ``terragrunt.hcl`` that deploys something: it has a
``terraform { source = ... }`` (or ``.tf`` files of its own). A ``terragrunt.hcl`` that is
only ``include``-d by others (a parent with ``remote_state`` / shared ``inputs``) is not
a unit. Includes are merged the way Terragrunt does it (shallow): the child's
``terraform.source``, ``remote_state`` and ``inputs`` keys win over the parent's.

The path helpers ``get_terragrunt_dir()``, ``get_repo_root()``,
``get_parent_terragrunt_dir()``, ``path_relative_to_include()``,
``path_relative_from_include()`` and ``find_in_parent_folders([name])`` are substituted;
``local.NAME`` is substituted when the local is a literal string. Anything else
(``dependency.*``, ``get_env()``, ...) stays unknown, with the reason.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .loader import TERRAGRUNT_FILE, Attr, Block, RepoSource, TgConfig, load_terragrunt
from .sources import classify_source, is_local

# Marks a repository-absolute path inside a substituted string.
_REPO = "\x00/"
_FUNC_RE = re.compile(
    r"\$\{\s*(get_terragrunt_dir|get_repo_root|get_parent_terragrunt_dir|"
    r"path_relative_to_include|path_relative_from_include|find_in_parent_folders)"
    r'\(\s*(?:"([^"]*)"[^)]*)?\)\s*\}'
)
_LOCAL_RE = re.compile(r"\$\{\s*local\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}")


@dataclass
class Unit:
    dir: str
    config: TgConfig
    parents: list[TgConfig] = field(default_factory=list)
    source: str | None = None  # terraform.source after substitution (None: not set)
    source_attr: Attr | None = None
    source_info: dict[str, Any] | None = None  # sources.classify_source
    module_dir: str | None = None  # local module directory analyzed for this unit
    status: str = "ok"  # "ok" or why the unit cannot be analyzed
    inputs: dict[str, Attr] = field(default_factory=dict)
    remote_state: Block | None = None
    remote_state_config: TgConfig | None = None  # the file remote_state comes from

    @property
    def file(self) -> str:
        return self.config.file


@dataclass
class _Ctx:
    repo: RepoSource
    unit_dir: str
    include_dir: str | None
    locals: dict[str, Attr]


def _strip(raw: Any) -> str | None:
    """python-hcl2 string -> template text (quotes removed); None for non-strings."""
    if not isinstance(raw, str):
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    return raw


def _find_up(repo: RepoSource, start: str, name: str) -> str | None:
    d = posixpath.dirname(start) if start != "." else None
    while d is not None:
        cand = posixpath.join(d, name) if d not in ("", ".") else name
        if (repo.root / cand).is_file():
            return cand
        if d in ("", "."):
            return None
        d = posixpath.dirname(d) or "."
    return None


def _relpath(a: str, b: str) -> str:
    return posixpath.relpath(a if a != "." else ".", b if b != "." else ".")


def substitute(text: str, ctx: _Ctx, depth: int = 0) -> str:
    """Replace the Terragrunt path helpers and literal locals; leave other ``${}``."""

    def func(m: re.Match[str]) -> str:
        name, arg = m.group(1), m.group(2)
        if name == "get_terragrunt_dir":
            return _REPO + ctx.unit_dir
        if name == "get_repo_root":
            return _REPO + "."
        if name == "find_in_parent_folders":
            found = _find_up(ctx.repo, ctx.unit_dir, arg or TERRAGRUNT_FILE)
            return _REPO + found if found else m.group(0)
        if ctx.include_dir is None:
            return m.group(0)
        if name == "get_parent_terragrunt_dir":
            return _REPO + ctx.include_dir
        if name == "path_relative_to_include":
            return _relpath(ctx.unit_dir, ctx.include_dir)
        return _relpath(ctx.include_dir, ctx.unit_dir)  # path_relative_from_include

    def local(m: re.Match[str]) -> str:
        attr = ctx.locals.get(m.group(1))
        value = _strip(attr.raw) if attr is not None else None
        if value is None or depth > 8:
            return m.group(0)
        out = substitute(value, ctx, depth + 1)
        return m.group(0) if "${" in out else out

    return _LOCAL_RE.sub(local, _FUNC_RE.sub(func, text))


def repo_path(text: str, base_dir: str) -> str | None:
    """A substituted local path -> repo-relative directory (None if not a local path)."""
    if text.startswith(_REPO):
        path = text[len(_REPO) :]
    elif is_local(text):
        path = posixpath.join(base_dir, text)
    else:
        return None
    path = posixpath.normpath(path.replace("//", "/"))
    return "." if path in ("", ".") else path


def display(text: str) -> str:
    """A substituted string for people (repo-absolute marker shown as ``<repo>/``)."""
    return (
        text.replace(_REPO + "./", "<repo>/")
        .replace(_REPO + ".", "<repo>")
        .replace(_REPO, "<repo>/")
    )


def substitute_raw(raw: Any, ctx: _Ctx) -> Any:
    """Apply :func:`substitute` to every string of a python-hcl2 value."""
    if isinstance(raw, str):
        out = substitute(raw, ctx)
        return out.replace(_REPO + ".", ".").replace(_REPO, "") if out != raw else raw
    if isinstance(raw, dict):
        return {k: substitute_raw(v, ctx) for k, v in raw.items()}
    if isinstance(raw, list):
        return [substitute_raw(v, ctx) for v in raw]
    return raw


def _include_targets(repo: RepoSource, cfg: TgConfig) -> list[TgConfig]:
    out = []
    for inc in cfg.includes:
        attr = inc.attrs.get("path")
        text = _strip(attr.raw) if attr else None
        if text is None:
            continue
        ctx = _Ctx(repo, cfg.dir, None, cfg.locals)
        target = repo_path(substitute(text, ctx), cfg.dir)
        if target is None or "${" in target or not (repo.root / target).is_file():
            continue
        parent = load_terragrunt(repo, target)
        if parent is not None and parent.file != cfg.file:
            out.append(parent)
    return out


def resolve_units(repo: RepoSource) -> list[Unit]:
    configs = [repo.terragrunt[f] for f in sorted(repo.terragrunt)]
    parents_of = {c.file: _include_targets(repo, c) for c in configs}
    included = {p.file for ps in parents_of.values() for p in ps}
    units: list[Unit] = []
    for cfg in configs:
        if posixpath.basename(cfg.file) != TERRAGRUNT_FILE:
            continue
        parents = parents_of[cfg.file]
        chain = parents + [cfg]  # later entries win
        source_cfg = next((c for c in reversed(chain) if c.source is not None), None)
        has_tf = cfg.dir in repo.modules
        if cfg.file in included and source_cfg is None and not has_tf:
            continue  # a parent configuration, not a unit
        unit = Unit(cfg.dir, cfg, parents)
        ctx = unit_context(repo, unit)
        for c in chain:
            for name, attr in c.inputs.items():
                unit.inputs[name] = Attr(name, substitute_raw(attr.raw, ctx), attr.loc)
            if c.remote_state is not None:
                unit.remote_state, unit.remote_state_config = c.remote_state, c
        _resolve_source(repo, unit, source_cfg, ctx, has_tf)
        units.append(unit)
    return units


def _resolve_source(
    repo: RepoSource, unit: Unit, source_cfg: TgConfig | None, ctx: _Ctx, has_tf: bool
) -> None:
    if source_cfg is None or source_cfg.source is None:
        if has_tf:
            unit.module_dir = unit.dir
        else:
            unit.status = f"{unit.file} has no terraform.source and its directory has no .tf files"
        return
    unit.source_attr = source_cfg.source
    text = _strip(source_cfg.source.raw)
    if text is None:
        unit.status = f"terraform.source in {source_cfg.file} is not a string"
        return
    src = substitute(text, ctx)
    unit.source = display(src)
    if "${" in src:
        unit.source_info = {"kind": "unknown", "source": unit.source, "ref": None}
        unit.status = f"terraform.source {unit.source!r} cannot be resolved statically"
        return
    target = repo_path(src, unit.dir)
    if target is None:
        unit.source_info = classify_source(src)
        unit.status = (
            f"terraform.source {src!r} is a {unit.source_info['kind']} module: "
            "terragrunt downloads it at run time, it is not in the repository"
        )
        return
    unit.source_info = {"kind": "local", "source": unit.source, "ref": None}
    if target == ".." or target.startswith("../"):
        unit.status = f"terraform.source {unit.source!r} is outside the repository"
    elif target in repo.modules:
        unit.module_dir = target
    else:
        unit.status = (
            f"terraform.source {unit.source!r} resolves to {target!r}, "
            "which has no .tf files in the repository"
        )


def unit_context(repo: RepoSource, unit: Unit) -> _Ctx:
    """Substitution context of ``unit`` (its own and its parents' locals)."""
    include_dir = unit.parents[-1].dir if unit.parents else None
    locals_: dict[str, Attr] = {}
    for c in [*unit.parents, unit.config]:
        locals_.update(c.locals)
    return _Ctx(repo, unit.dir, include_dir, locals_)
