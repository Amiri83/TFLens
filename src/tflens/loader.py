"""Load a Terraform repository: python-hcl2 values + exact source locations."""

from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hcl2

from .expr import normalize_key, raw_to_expr
from .locate import LocBlock, scan
from .values import Location

_SKIP_DIRS = {".terraform", ".git", ".venv", "venv", "node_modules", "__pycache__"}
_LABEL_COUNT = {
    "resource": 2,
    "data": 2,
    "module": 1,
    "variable": 1,
    "output": 1,
    "provider": 1,
    "locals": 0,
    "terraform": 0,
    "moved": 0,
    "import": 0,
    "check": 1,
    "removed": 0,
}


@dataclass
class Attr:
    name: str
    raw: Any
    loc: Location
    _expr: Any = field(default=None, repr=False)

    @property
    def expr(self) -> Any:
        if self._expr is None:
            self._expr = raw_to_expr(self.raw)
        return self._expr


@dataclass
class Block:
    type: str
    labels: tuple[str, ...]
    loc: Location
    attrs: dict[str, Attr] = field(default_factory=dict)
    blocks: dict[str, list[Block]] = field(default_factory=dict)
    dynamic: list[str] = field(default_factory=list)


@dataclass
class ModuleSource:
    dir: str
    files: list[str] = field(default_factory=list)
    variables: dict[str, Block] = field(default_factory=dict)
    locals: dict[str, Attr] = field(default_factory=dict)
    outputs: dict[str, Block] = field(default_factory=dict)
    resources: dict[tuple[str, str], Block] = field(default_factory=dict)
    data: dict[tuple[str, str], Block] = field(default_factory=dict)
    module_calls: dict[str, Block] = field(default_factory=dict)


@dataclass
class ParseError:
    file: str
    message: str


@dataclass(frozen=True)
class RemoteModule:
    """One entry of ``<root>/.terraform/modules/modules.json`` (written by terraform init)."""

    key: str  # "net" or "net.subnets" for nested calls
    source: str
    version: str | None
    dir: str  # repo-relative directory of the materialized module
    manifest: str  # repo-relative path of modules.json
    status: str = "ok"  # "ok" or the reason the directory cannot be used

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "version": self.version,
            "dir": self.dir,
            "manifest": self.manifest,
            "status": self.status,
        }


@dataclass
class RepoSource:
    root: Path
    modules: dict[str, ModuleSource] = field(default_factory=dict)
    tfvars: dict[str, dict[str, Attr]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    # root module dir -> modules.json key -> entry
    remote: dict[str, dict[str, RemoteModule]] = field(default_factory=dict)
    _lines: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @property
    def remote_dirs(self) -> set[str]:
        return {
            r.dir for entries in self.remote.values() for r in entries.values() if r.status == "ok"
        }

    def lines(self, file: str) -> list[str]:
        if file not in self._lines:
            try:
                text = (self.root / file).read_text(encoding="utf-8")
            except OSError:
                text = ""
            self._lines[file] = text.splitlines()
        return self._lines[file]

    def snippet(self, loc: Location, max_lines: int = 40) -> str:
        lines = self.lines(loc.file)[loc.start_line - 1 : loc.end_line]
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [
                f"... ({loc.end_line - loc.start_line + 1 - max_lines} more lines)"
            ]
        return "\n".join(lines)


def load_repo(root: Path) -> RepoSource:
    root = root.resolve()
    repo = RepoSource(root=root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for fname in sorted(filenames):
            rel = f"{rel_dir}/{fname}" if rel_dir != "." else fname
            if fname.endswith(".tf"):
                repo.files.append(rel)
                mod = repo.modules.setdefault(rel_dir, ModuleSource(dir=rel_dir))
                mod.files.append(rel)
                _load_tf(repo, mod, rel)
            elif fname.endswith(".tfvars"):
                repo.files.append(rel)
                _load_tfvars(repo, rel)
    for mod_dir in sorted(repo.modules):
        _load_manifest(repo, mod_dir)
    return repo


def _load_manifest(repo: RepoSource, mod_dir: str) -> None:
    """Load the remote modules that ``terraform init`` materialized for a root module.

    Only reads ``.terraform/modules/modules.json`` and the ``.tf`` files of the
    directories it lists; nothing is downloaded.
    """
    prefix = "" if mod_dir == "." else mod_dir + "/"
    manifest = f"{prefix}.terraform/modules/modules.json"
    path = repo.root / manifest
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["Modules"]
        if not isinstance(entries, list):
            raise TypeError("'Modules' is not a list")
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        repo.errors.append(ParseError(manifest, f"cannot read module manifest: {exc}"))
        return
    out: dict[str, RemoteModule] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key, source, rel_dir = entry.get("Key"), entry.get("Source"), entry.get("Dir")
        if not isinstance(key, str) or not key or not isinstance(rel_dir, str):
            continue
        version = entry.get("Version")
        version = version if isinstance(version, str) and version else None
        source = source if isinstance(source, str) else ""
        target = posixpath.normpath(posixpath.join(mod_dir, rel_dir))
        status = "ok"
        if rel_dir.startswith("/") or target == ".." or target.startswith("../"):
            status = f"directory {rel_dir!r} in {manifest} is outside the repository"
        elif target not in repo.modules:
            tf_files = _module_files(repo, target)
            if not tf_files:
                status = (
                    f"directory {target!r} listed in {manifest} is missing or has no .tf files "
                    "(re-run terraform init)"
                )
            else:
                mod = repo.modules.setdefault(target, ModuleSource(dir=target))
                for rel in tf_files:
                    repo.files.append(rel)
                    mod.files.append(rel)
                    _load_tf(repo, mod, rel)
        out[key] = RemoteModule(key, source, version, target, manifest, status)
    repo.remote[mod_dir] = {k: out[k] for k in sorted(out)}
    repo.files.sort()


def _module_files(repo: RepoSource, rel_dir: str) -> list[str]:
    path = repo.root / rel_dir
    try:
        path.resolve().relative_to(repo.root)
        names = sorted(p.name for p in path.iterdir() if p.is_file() and p.name.endswith(".tf"))
    except (OSError, ValueError):
        return []
    return [f"{rel_dir}/{n}" if rel_dir != "." else n for n in names]


def _read(repo: RepoSource, rel: str) -> tuple[dict[str, Any], LocBlock] | None:
    path = repo.root / rel
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        repo.errors.append(ParseError(rel, f"cannot read file: {exc}"))
        return None
    try:
        data = hcl2.loads(text)
    except Exception as exc:  # python-hcl2 raises lark errors of various types
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        repo.errors.append(ParseError(rel, f"python-hcl2 parse error: {msg}"))
        return None
    try:
        locs = scan(text)
    except ValueError as exc:
        repo.errors.append(ParseError(rel, f"location scan error: {exc}"))
        locs = LocBlock("<file>", (), 1, max(1, text.count("\n") + 1))
    return data, locs


def _load_tfvars(repo: RepoSource, rel: str) -> None:
    res = _read(repo, rel)
    if res is None:
        return
    data, locs = res
    out: dict[str, Attr] = {}
    for k, v in data.items():
        name = normalize_key(k)
        if not isinstance(name, str) or name.startswith("__"):
            continue
        out[name] = Attr(name, v, _loc(rel, locs.attrs.get(name), locs))
    repo.tfvars[rel] = out


def _loc(file: str, span: tuple[int, int] | None, fallback: LocBlock) -> Location:
    if span is None:
        return Location(file, fallback.start, fallback.end)
    return Location(file, span[0], span[1])


def _load_tf(repo: RepoSource, mod: ModuleSource, rel: str) -> None:
    res = _read(repo, rel)
    if res is None:
        return
    data, locs = res
    seen: dict[tuple[str, tuple[str, ...]], int] = {}
    for block_type, entries in data.items():
        block_type = normalize_key(block_type)
        nlabels = _LABEL_COUNT.get(block_type)
        if nlabels is None or not isinstance(entries, list):
            continue
        for entry in entries:
            for labels, body in _unnest(entry, nlabels):
                key = (block_type, labels)
                idx = seen.get(key, 0)
                seen[key] = idx + 1
                candidates = [
                    c for c in locs.children if c.type == block_type and c.labels == labels
                ]
                lb = candidates[idx] if idx < len(candidates) else None
                block = _make_block(rel, block_type, labels, body, lb, locs)
                _register(mod, block)


def _unnest(entry: Any, depth: int) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    if depth == 0:
        return [((), entry)] if isinstance(entry, dict) else []
    out: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    if not isinstance(entry, dict):
        return out
    for k, v in entry.items():
        if isinstance(k, str) and k.startswith("__"):
            continue
        for labels, body in _unnest(v, depth - 1):
            out.append(((str(normalize_key(k)),) + labels, body))
    return out


def _make_block(
    file: str,
    block_type: str,
    labels: tuple[str, ...],
    body: dict[str, Any],
    lb: LocBlock | None,
    parent: LocBlock,
) -> Block:
    span = lb or parent
    block = Block(block_type, labels, Location(file, span.start, span.end))
    for k, v in body.items():
        if isinstance(k, str) and k.startswith("__") and k.endswith("__"):
            continue
        name = str(normalize_key(k))
        if name == "dynamic":
            for entry in v if isinstance(v, list) else [v]:
                if isinstance(entry, dict):
                    block.dynamic.extend(
                        str(normalize_key(x)) for x in entry if not str(x).startswith("__")
                    )
            continue
        loc_children = lb.children_of(name) if lb else []
        marked = (
            isinstance(v, list)
            and bool(v)
            and all(isinstance(x, dict) and x.get("__is_block__") for x in v)
        )
        is_block = marked or (bool(loc_children) and name not in (lb.attrs if lb else {}))
        if is_block:
            bodies = v if isinstance(v, list) else [v]
            nested = []
            for i, nb in enumerate(bodies):
                if not isinstance(nb, dict):
                    continue
                child_lb = loc_children[i] if i < len(loc_children) else None
                nested.append(_make_block(file, name, (), nb, child_lb, span))
            block.blocks[name] = nested
        else:
            attr_span = lb.attrs.get(name) if lb else None
            block.attrs[name] = Attr(name, v, _loc(file, attr_span, span))
    return block


def _register(mod: ModuleSource, block: Block) -> None:
    t, labels = block.type, block.labels
    if t == "resource":
        mod.resources[(labels[0], labels[1])] = block
    elif t == "data":
        mod.data[(labels[0], labels[1])] = block
    elif t == "module":
        mod.module_calls[labels[0]] = block
    elif t == "variable":
        mod.variables[labels[0]] = block
    elif t == "output":
        mod.outputs[labels[0]] = block
    elif t == "locals":
        mod.locals.update(block.attrs)
