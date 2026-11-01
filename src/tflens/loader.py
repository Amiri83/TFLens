"""Load a Terraform repository: python-hcl2 values + exact source locations.

Discovery is never silent: every directory that is not walked (unreadable, a tool / VCS
directory, a symlink) is recorded in :attr:`RepoSource.skipped`, and every file seen is
listed in :attr:`RepoSource.all_files` (analyzable or not) so the UI can show the whole
repository. Analyzed syntaxes: ``*.tf``, ``*.tf.json``, ``*.tfvars``, ``*.tfvars.json``
and ``terragrunt.hcl`` (see :mod:`tflens.terragrunt`).
"""

from __future__ import annotations

import json
import os
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hcl2

from .expr import normalize_key, raw_to_expr
from .jsonloc import JsonLocError, tf_json, tfvars_json
from .locate import LocBlock, object_keys, scan
from .values import Location

_SKIP_DIRS = {".terraform", ".git", ".venv", "venv", "node_modules", "__pycache__", "dist"}
# Hidden directories that are walked anyway: CI configuration (read by crawl.py).
CI_DIRS = frozenset({".github", ".gitlab", ".circleci", ".buildkite"})
# Code that exercises a module rather than deploying it: example, test and fixture
# directories are left out of the inventory and the graph (a module is mapped from its own
# directory only). An environment called "test" (envs/test, or test/ next to dev/ and
# prod/) is not one of them.
EXCLUDED_DIRS = frozenset({"examples", "example", "test", "tests", "fixtures"})
# Environment names (dev, prod, ...) and the directories whose sub-directories are
# environments (envs/<env>, live/<env>, ...): used by detect.py too.
ENV_WORDS = frozenset(
    {
        "dev",
        "develop",
        "development",
        "test",
        "testing",
        "qa",
        "uat",
        "sit",
        "int",
        "intg",
        "integ",
        "integration",
        "e2e",
        "stage",
        "staging",
        "stg",
        "preprod",
        "pre-prod",
        "prod",
        "production",
        "prd",
        "sandbox",
        "sbx",
        "nonprod",
        "non-prod",
        "perf",
        "demo",
        "dr",
    }
)
ENV_CONTAINERS = frozenset(
    {"envs", "env", "environments", "environment", "live", "deployments", "accounts", "stages"}
)
OUTPUT_DIR = "tflens-out"  # written by `tflens serve --dir` inside the repository
TERRAGRUNT_FILE = "terragrunt.hcl"
LOCK_FILE = ".terraform.lock.hcl"
# Files listed in the repository tree (the full inventory); beyond this it is truncated.
MAX_LISTED_FILES = 20000
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
    terraform: list[Block] = field(default_factory=list)  # terraform {} blocks
    providers: list[Block] = field(default_factory=list)  # provider "x" {} blocks


@dataclass
class ParseError:
    file: str
    message: str


@dataclass(frozen=True)
class SkippedDir:
    """A directory discovery did not walk, and why."""

    path: str
    kind: str  # unreadable | ignored | symlink
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "reason": self.reason}


@dataclass
class TgConfig:
    """One Terragrunt configuration file (``terragrunt.hcl`` or an included ``*.hcl``)."""

    file: str
    dir: str
    loc: Location
    source: Attr | None = None  # terraform { source = ... }
    terraform: Block | None = None
    inputs: dict[str, Attr] = field(default_factory=dict)
    includes: list[Block] = field(default_factory=list)
    remote_state: Block | None = None
    generate: list[Block] = field(default_factory=list)
    dependencies: dict[str, Block] = field(default_factory=dict)
    locals: dict[str, Attr] = field(default_factory=dict)


def file_kind(name: str) -> str:
    """How discovery treats a file name: terraform | tfvars | terragrunt | lock | other."""
    if name.endswith((".tf", ".tf.json")):
        return "terraform"
    if name.endswith((".tfvars", ".tfvars.json")):
        return "tfvars"
    if name == TERRAGRUNT_FILE:
        return "terragrunt"
    if name == LOCK_FILE:
        return "lock"
    return "other"


def extension(name: str) -> str:
    """Extension used in the inventory (``.tf.json`` / ``.tfvars.json`` kept whole)."""
    for double in (".tf.json", ".tfvars.json"):
        if name.endswith(double):
            return double
    base = name.lstrip(".")
    return "." + base.rsplit(".", 1)[1].lower() if "." in base else "(none)"


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
    # discovery: every file seen (repo-relative) and the directories not walked
    all_files: list[str] = field(default_factory=list)
    skipped: list[SkippedDir] = field(default_factory=list)
    truncated: bool = False
    # inventory: directory -> extension -> file count (directory's own files only)
    dir_ext: dict[str, Counter[str]] = field(default_factory=dict)
    # Terragrunt: config file -> parsed config; units are resolved by terragrunt.py
    terragrunt: dict[str, TgConfig] = field(default_factory=dict)
    units: list[Any] = field(default_factory=list)
    # .terraform.lock.hcl: directory -> provider source -> (version, location)
    locks: dict[str, dict[str, tuple[str, Location]]] = field(default_factory=dict)
    _lines: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @property
    def analyzable(self) -> list[str]:
        """Files TFLens analyzes: Terraform (HCL / JSON) and Terragrunt configurations."""
        return sorted(
            f for f in self.files if file_kind(posixpath.basename(f)) in ("terraform", "terragrunt")
        )

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


def excluded_dir(name: str, parent: str, siblings: list[str]) -> bool:
    """Whether a directory holds example / test / fixture code (:data:`EXCLUDED_DIRS`)
    rather than being an environment: ``envs/test`` and ``test/`` next to ``dev/`` stay."""
    low = name.lower()
    if low not in EXCLUDED_DIRS:
        return False
    if low in ENV_WORDS:
        env_parent = posixpath.basename(parent).lower() in ENV_CONTAINERS
        env_sibling = any(s.lower() in ENV_WORDS and s.lower() != low for s in siblings)
        return not (env_parent or env_sibling)
    return True


def _rel(root: Path, path: str) -> str:
    rel = Path(path).relative_to(root).as_posix()
    return rel or "."


def load_repo(root: Path) -> RepoSource:
    from .terragrunt import resolve_units

    root = root.resolve()
    repo = RepoSource(root=root)

    def onerror(exc: OSError) -> None:
        path = _rel(root, exc.filename) if exc.filename else "?"
        repo.skipped.append(SkippedDir(path, "unreadable", f"cannot list: {exc.strerror or exc}"))

    # 1. the whole tree first (every depth), then 2. each file classified by what it is
    walked: list[tuple[str, list[str]]] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        rel_dir = _rel(root, dirpath)
        keep = []
        for d in sorted(dirnames):
            sub = f"{rel_dir}/{d}" if rel_dir != "." else d
            if os.path.islink(os.path.join(dirpath, d)):
                repo.skipped.append(SkippedDir(sub, "symlink", "symbolic link (not followed)"))
            elif d == OUTPUT_DIR and rel_dir == ".":
                # TFLens's own output: left out of the model entirely, so a model does not
                # depend on whether an earlier one was written next to it
                continue
            elif d in _SKIP_DIRS or (d.startswith(".") and d not in CI_DIRS):
                repo.skipped.append(SkippedDir(sub, "ignored", "tool, VCS or hidden directory"))
            elif excluded_dir(d, rel_dir, dirnames):
                repo.skipped.append(
                    SkippedDir(
                        sub,
                        "excluded",
                        "example / test / fixture code: not part of the inventory or the graph",
                    )
                )
            else:
                keep.append(d)
        dirnames[:] = keep
        walked.append((rel_dir, sorted(filenames)))
    for rel_dir, filenames in walked:
        counts = repo.dir_ext.setdefault(rel_dir, Counter())
        for fname in filenames:
            rel = f"{rel_dir}/{fname}" if rel_dir != "." else fname
            counts[extension(fname)] += 1
            if len(repo.all_files) < MAX_LISTED_FILES:
                repo.all_files.append(rel)
            else:
                repo.truncated = True
            kind = file_kind(fname)
            if kind == "terraform":
                repo.files.append(rel)
                mod = repo.modules.setdefault(rel_dir, ModuleSource(dir=rel_dir))
                mod.files.append(rel)
                _load_tf(repo, mod, rel)
            elif kind == "tfvars":
                repo.files.append(rel)
                _load_tfvars(repo, rel)
            elif kind == "terragrunt":
                repo.files.append(rel)
                load_terragrunt(repo, rel)
            elif kind == "lock":
                repo.files.append(rel)
                _load_lock(repo, rel_dir, rel)
    for mod_dir in sorted(repo.modules):
        _load_manifest(repo, mod_dir)
    repo.skipped.sort(key=lambda s: s.path)
    repo.all_files.sort()
    repo.units = resolve_units(repo)
    repo.files.sort()
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
        names = sorted(
            p.name for p in path.iterdir() if p.is_file() and p.name.endswith((".tf", ".tf.json"))
        )
    except (OSError, ValueError):
        return []
    return [f"{rel_dir}/{n}" if rel_dir != "." else n for n in names]


def _text(repo: RepoSource, rel: str) -> str | None:
    try:
        return (repo.root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        repo.errors.append(ParseError(rel, f"cannot read file: {exc}"))
        return None


def _read(repo: RepoSource, rel: str) -> tuple[dict[str, Any], LocBlock] | None:
    text = _text(repo, rel)
    if text is None:
        return None
    if rel.endswith(".tf.json"):
        try:
            return tf_json(text)
        except JsonLocError as exc:
            repo.errors.append(ParseError(rel, f"JSON syntax error: {exc}"))
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
    if rel.endswith(".json"):
        text = _text(repo, rel)
        if text is None:
            return
        try:
            values = tfvars_json(text)
        except JsonLocError as exc:
            repo.errors.append(ParseError(rel, f"JSON syntax error: {exc}"))
            return
        repo.tfvars[rel] = {
            k: Attr(k, v, Location(rel, start, end)) for k, (v, start, end) in values.items()
        }
        return
    out = read_attrs(repo, rel)
    if out is not None:
        repo.tfvars[rel] = out


def read_attrs(repo: RepoSource, rel: str) -> dict[str, Attr] | None:
    """Top-level attributes of an HCL file (tfvars, ``*.tfbackend``), with locations."""
    res = _read(repo, rel)
    if res is None:
        return None
    data, locs = res
    out: dict[str, Attr] = {}
    for k, v in data.items():
        name = normalize_key(k)
        if not isinstance(name, str) or name.startswith("__") or _is_block_list(v):
            continue
        out[name] = Attr(name, v, _loc(rel, locs.attrs.get(name), locs))
    return out


def _is_block_list(v: Any) -> bool:
    return (
        isinstance(v, list)
        and bool(v)
        and all(isinstance(x, dict) and x.get("__is_block__") for x in v)
    )


def read_blocks(repo: RepoSource, rel: str, block_type: str) -> list[Block]:
    """Top-level blocks of one type (no labels) in an HCL file, e.g. ``locals`` of env.hcl."""
    res = _read(repo, rel)
    if res is None:
        return []
    data, locs = res
    out = []
    for entry in data.get(block_type, []):
        for labels, body in _unnest(entry, 0):
            cands = locs.children_of(block_type)
            lb = cands[len(out)] if len(out) < len(cands) else None
            out.append(_make_block(rel, block_type, labels, body, lb, locs))
    return out


def _load_lock(repo: RepoSource, rel_dir: str, rel: str) -> None:
    """Provider versions pinned in ``.terraform.lock.hcl`` (written by terraform init)."""
    res = _read(repo, rel)
    if res is None:
        return
    data, locs = res
    out: dict[str, tuple[str, Location]] = {}
    for entry in data.get("provider", []):
        for labels, body in _unnest(entry, 1):
            version = raw_to_expr(body.get("version", ""))
            value = getattr(version, "value", None)
            if not isinstance(value, str):
                continue
            lb = next((c for c in locs.children if c.labels == labels), None)
            out[labels[0]] = (value, _loc(rel, (lb.start, lb.end) if lb else None, locs))
    repo.locks[rel_dir] = out


def load_terragrunt(repo: RepoSource, rel: str) -> TgConfig | None:
    """Parse a Terragrunt configuration (cached; included files are loaded on demand)."""
    if rel in repo.terragrunt:
        return repo.terragrunt[rel]
    text = _text(repo, rel)
    if text is None:
        return None
    res = _read(repo, rel)
    if res is None:
        return None
    if rel not in repo.files:
        repo.files.append(rel)
    data, locs = res
    rel_dir = posixpath.dirname(rel) or "."
    cfg = TgConfig(rel, rel_dir, Location(rel, locs.start, locs.end))
    labelled = {"dependency": 1, "generate": 1}
    for raw_key, value in data.items():
        key = normalize_key(raw_key)
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            span = locs.attrs.get(key)
            if key == "inputs" and isinstance(value, dict):
                keys = object_keys(text, *span) if span else {}
                for k, v in value.items():
                    name = str(normalize_key(k))
                    if not name.startswith("__"):
                        cfg.inputs[name] = Attr(name, v, _loc(rel, keys.get(name) or span, locs))
            continue
        for entry in value:
            depth = labelled.get(key, 0)
            if key == "include" and not entry.get("__is_block__"):
                depth = 1  # include "root" { ... }
            for labels, body in _unnest(entry, depth):
                cands = [c for c in locs.children if c.type == key and c.labels == labels]
                block = _make_block(rel, key, labels, body, cands[0] if cands else None, locs)
                if key == "terraform":
                    cfg.terraform = block
                    cfg.source = block.attrs.get("source")
                elif key == "include":
                    cfg.includes.append(block)
                elif key == "remote_state":
                    cfg.remote_state = block
                elif key == "generate":
                    cfg.generate.append(block)
                elif key == "dependency":
                    cfg.dependencies[labels[0]] = block
                elif key == "locals":
                    cfg.locals.update(block.attrs)
    repo.terragrunt[rel] = cfg
    return cfg


def map_keys(repo: RepoSource, attr: Attr) -> dict[str, Location]:
    """Locations of the keys of a map-valued attribute (``config = {...}``)."""
    text = "\n".join(repo.lines(attr.loc.file))
    spans = object_keys(text, attr.loc.start_line, attr.loc.end_line)
    return {k: Location(attr.loc.file, s, e) for k, (s, e) in spans.items()}


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
                labels: tuple[str, ...] = ()
                if name == "backend":  # backend "s3" { ... }: keep the label and key lines
                    inner = [(k, x) for k, x in nb.items() if isinstance(x, dict)]
                    if len(inner) == 1 and len(nb) == 1:
                        labels, nb = (str(normalize_key(inner[0][0])),), inner[0][1]
                nested.append(_make_block(file, name, labels, nb, child_lb, span))
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
    elif t == "terraform":
        mod.terraform.append(block)
    elif t == "provider":
        mod.providers.append(block)
