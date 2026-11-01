"""Discovery as a phased pipeline: crawl → hypothesis → validate → (AWS) → render.

1. **Crawl + hypothesis**: every file of the repository is listed (tree-like; ``.terraform``,
   ``.venv``, ``.git``, ``dist``, ... are skipped and listed as such). READMEs, other
   Markdown files, ``docs/``, HANDOFF notes, CI files, scripts and the git log are read as
   *hints only*: a draft map of stacks, environments and links between stacks, each item
   marked ``hint`` and carrying its sources (``file:line``, how it was read).
2. **Validate**: every hint is checked against real repository files only (``.tf``,
   ``.tfvars``, ``backend-*.hcl`` / ``*.tfbackend``). An environment is a real tfvars /
   backend file (or environment directory holding Terraform code) in that stack's tree;
   docs and CI never create one. Each item is ``confirmed``, ``hint-only`` (named in
   docs / CI / git, nothing in the code) or ``contradicted`` (the hint names a path or file
   the code says otherwise about); the docs-vs-code mismatches are listed.
3. **AWS** (optional, off by default): read-only existence checks of confirmed resources
   with an AWS account's own credentials (:mod:`tflens.awscheck`). Skipped, with the
   reason, when not asked for or not configured.
4. **Render**: the model (inventory per environment column, diagrams, graph) is built from
   the confirmed data only and written by the caller's ``write`` callback, after the last
   cancellation point: a cancelled run writes nothing.

Every phase reports its steps to a :class:`Progress` (the web UI's progress screen); its
``step`` raises :class:`Cancelled` once a cancel was requested, so the run stops cleanly
between steps.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .crawl import ci_system, crawl, doc_files
from .detect import COMMON_STEMS, GENERIC_STEMS, backend_env
from .engine import in_scope
from .loader import ENV_CONTAINERS, ENV_WORDS, RepoSource, load_repo
from .output import Analysis, run_analysis

PHASES: tuple[tuple[str, str], ...] = (
    ("crawl", "Crawl + hypothesis"),
    ("validate", "Validate against the code"),
    ("aws", "Check in AWS (read-only)"),
    ("render", "Render"),
)
CONFIRMED, HINT_ONLY, CONTRADICTED = "confirmed", "hint-only", "contradicted"
MAX_SOURCES = 25  # sources kept per hint (all of them are counted)
MAX_TREE_LINES = 25000
MAX_HINT_FILE_BYTES = 512 * 1024
# files whose code is Terraform's: the only evidence an item is confirmed against
CODE_SUFFIXES = (".tf", ".tf.json", ".tfvars", ".tfvars.json", ".hcl", ".tfbackend")

_SCRIPT_NAMES = frozenset({"Makefile", "makefile", "GNUmakefile", "Taskfile.yml", "justfile"})
_HANDOFF_RE = re.compile(r"^handoff([._-][^/]*)?$", re.I)
_ENV_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_/.-])("
    + "|".join(sorted(map(re.escape, ENV_WORDS), key=len, reverse=True))
    + r")(?![A-Za-z0-9_-])"
)
_ENV_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[\w.-]+/)*("
    + "|".join(sorted(map(re.escape, ENV_CONTAINERS), key=len, reverse=True))
    + r")/([A-Za-z0-9][\w.-]*)"
)
_CLI_FLAG_RE = re.compile(r"(?<![\w-])--?(backend-config|var-file)(?:=|\s+)['\"]?([^\s'\";|&)]+)")
_DIR_RE = re.compile(
    r"(?:(?<![\w-])--?chdir=|working[-_]directory\s*:\s*|working_dir\s*[:=]\s*|"
    r"(?<![\w-])cd\s+|^\s*-?\s*dir\s*:\s*)['\"]?([^\s'\";|&)`]+)"
)
_CODE_PATH_RE = re.compile(r"`(\.?/?[\w.-]+(?:/[\w.-]+)*/?)`")
_STACK_WORDS_RE = re.compile(r"\b(stacks?|roots?|deploy\w*|apply|state|terraform|layers?)\b", re.I)
_LINK_WORDS_RE = re.compile(
    r"(->|→|=>|\bdepends?\s+on\b|\breads?\b|\buses?\b|\bneeds?\b|\bafter\b|\bbefore\b|"
    r"\bremote[ _]state\b|\boutputs?\b)",
    re.I,
)
_DYNAMIC = re.compile(r"[$%{}<>*]")


class Cancelled(Exception):
    """Raised by :meth:`Progress.step` once the run was asked to stop."""


class Progress(Protocol):
    def phase(self, name: str, label: str) -> None: ...

    def phase_done(self, name: str, status: str, detail: str = "") -> None: ...

    def step(self, text: str) -> None: ...

    def log(self, text: str) -> None: ...


class NullProgress:
    """No progress reporting, no cancellation (the CLI, the regenerate API)."""

    def phase(self, name: str, label: str) -> None:
        pass

    def phase_done(self, name: str, status: str, detail: str = "") -> None:
        pass

    def step(self, text: str) -> None:
        pass

    def log(self, text: str) -> None:
        pass


@dataclass
class Options:
    """What :func:`discover` analyzes (as :func:`output.run_analysis`) and whether it checks
    in AWS (``aws``: an :class:`awscheck.AwsTarget`, None = off)."""

    var_files: list[str] = field(default_factory=list)
    cli_vars: dict[str, str] = field(default_factory=dict)
    scope: str | None = None
    git_root: Path | None = None
    git: bool = True
    env: str | None = None
    auto_env: bool = False
    stack_envs: dict[str, str] | None = None
    aws: Any = None
    aws_requested: bool = False


T = TypeVar("T")


@dataclass
class Discovered:
    analysis: Analysis
    written: Any = None

    @property
    def model(self) -> dict[str, Any]:
        return self.analysis.model


# ------------------------------------------------------------------ phase 1: crawl


def tree_lines(all_files: list[str], skipped: list[dict[str, Any]]) -> list[str]:
    """The repository as ``tree`` prints it: directories first-level sorted, files under
    them; skipped directories listed with why (not walked)."""
    root: dict[str, Any] = {}
    for f in all_files:
        node = root
        parts = f.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p + "/", {})
        node[parts[-1]] = None
    for s in skipped:
        node = root
        parts = s["path"].split("/")
        for p in parts[:-1]:
            node = node.setdefault(p + "/", {})
        node[f"{parts[-1]}/  [skipped: {s['reason']}]"] = None
    out = ["."]

    def walk(node: dict[str, Any], prefix: str) -> None:
        keys = sorted(node, key=lambda k: (not k.endswith("/"), k))
        for i, k in enumerate(keys):
            if len(out) >= MAX_TREE_LINES:
                return
            last = i == len(keys) - 1
            out.append(prefix + ("└── " if last else "├── ") + k)
            if isinstance(node[k], dict):
                walk(node[k], prefix + ("    " if last else "│   "))

    walk(root, "")
    if len(out) >= MAX_TREE_LINES:
        out.append(f"… truncated at {MAX_TREE_LINES} lines")
    return out


def hint_sources(all_files: list[str]) -> list[tuple[str, str]]:
    """``(file, via)``: the files read for hints: docs (READMEs, Markdown, ``docs/``,
    HANDOFF notes) and CI / scripts. Never Terraform code."""
    docs = set(doc_files(all_files))
    out = []
    for f in all_files:
        base = posixpath.basename(f)
        if f.endswith(CODE_SUFFIXES):
            continue
        if ci_system(f) or base in _SCRIPT_NAMES or base.endswith((".sh", ".mk", ".bash", ".ps1")):
            out.append((f, "ci"))
        elif f in docs or _HANDOFF_RE.match(base):
            out.append((f, "docs"))
    return out


def _read(root: Path, rel: str) -> list[str] | None:
    path = root / rel
    try:
        path.resolve().relative_to(root.resolve())
        if path.stat().st_size > MAX_HINT_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _norm_path(value: str) -> str | None:
    value = value.strip().strip("'\"`").rstrip(".,;:)`'\"").removeprefix("./").rstrip("/")
    if not value or value in (".", "..") or _DYNAMIC.search(value) or "://" in value:
        return None
    if value.startswith(("/", "~", "-", "..")):
        return None
    return posixpath.normpath(value)


class _Hints:
    """The draft map: hints keyed by (kind, name), each with its sources."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        self,
        kind: str,
        name: str,
        file: str,
        line: int,
        via: str,
        text: str,
        **extra: Any,
    ) -> None:
        h = self.items.setdefault(
            (kind, name),
            {"kind": kind, "name": name, "status": "hint", "mentions": 0, "sources": []},
        )
        h["mentions"] += 1
        for k, v in extra.items():
            if isinstance(v, str):
                vals = h.setdefault(k, [])
                if v not in vals:
                    vals.append(v)
            else:
                h.setdefault(k, v)
        if len(h["sources"]) < MAX_SOURCES:
            h["sources"].append(
                {"file": file, "line": line, "via": via, "text": text.strip()[:200]}
            )

    def result(self) -> list[dict[str, Any]]:
        out = []
        for key in sorted(self.items):
            h = self.items[key]
            for k in ("paths", "files"):
                if k in h:
                    h[k] = sorted(h[k])
            h["via"] = sorted({s["via"] for s in h["sources"]})
            out.append(h)
        return out


def _scan_line(hints: _Hints, file: str, n: int, via: str, line: str) -> None:
    dirs: list[str] = []
    for m in _ENV_WORD_RE.finditer(line):
        hints.add("env", m.group(1), file, n, via, line)
    for m in _ENV_PATH_RE.finditer(line):
        name = m.group(2).split(".")[0]
        path = _norm_path(m.group(0))
        if not name or name.lower() in ("modules", "module") or path is None:
            continue
        hints.add("env", name, file, n, via, line, paths=path)
    for m in _CLI_FLAG_RE.finditer(line):
        value = m.group(2)
        if "=" in value:
            continue
        path = _norm_path(value)
        if path is None:
            continue
        if m.group(1) == "backend-config":
            stem = backend_env(path) or ""
        else:
            stem = posixpath.basename(path).split(".")[0]
            if stem.lower() in GENERIC_STEMS | COMMON_STEMS:
                stem = posixpath.basename(posixpath.dirname(path))
        if stem:
            hints.add("env", stem, file, n, via, line, files=path)
    for m in _DIR_RE.finditer(line):
        path = _norm_path(m.group(1))
        if path is not None and not posixpath.splitext(path)[1]:
            dirs.append(path)
    if via == "docs" and _STACK_WORDS_RE.search(line):
        for m in _CODE_PATH_RE.finditer(line):
            path = _norm_path(m.group(1))
            if path is not None and not posixpath.splitext(path)[1]:
                dirs.append(path)
    dirs = list(dict.fromkeys(dirs))
    for d in dirs:
        hints.add("stack", d, file, n, via, line)
    if len(dirs) >= 2 and _LINK_WORDS_RE.search(line):
        a, b = dirs[0], dirs[1]
        hints.add("link", f"{a} -> {b}", file, n, via, line, ends=(a, b))


def hypothesize(repo: RepoSource, crawled: dict[str, Any]) -> list[dict[str, Any]]:
    """Phase 1's draft map from docs, CI and the git log (every item a ``hint``)."""
    hints = _Hints()
    for f, via in hint_sources(repo.all_files):
        lines = _read(repo.root, f)
        if lines is None:
            continue
        for n, line in enumerate(lines, 1):
            _scan_line(hints, f, n, via, line)
    for c in (crawled.get("git") or {}).get("commits", []):
        text = c.get("subject", "")
        if c.get("body"):
            text += " " + c["body"].replace("\n", " ")
        _scan_line(hints, f"git:{c.get('short', '')}", 0, "git", text)
    return hints.result()


# ------------------------------------------------------------------ phase 2: validate


def _env_evidence(env: dict[str, Any], files: set[str]) -> list[str]:
    """The real Terraform files an environment rests on (tfvars, backend configurations,
    the code of its directory)."""
    found = set(env.get("var_files", [])) | set(env.get("backend_hcl", []))
    found |= {e["file"] for e in env.get("evidence", []) if e.get("file")}
    return sorted(f for f in found if f in files and f.endswith(CODE_SUFFIXES))


def _exists(path: str, files: list[str]) -> bool:
    return any(f == path or f.startswith(path + "/") or f.endswith("/" + path) for f in files)


def _owning(path: str, roots: list[str]) -> list[str]:
    return [r for r in roots if r == path or in_scope(r, path)]


def validate(model: dict[str, Any], hints: list[dict[str, Any]]) -> dict[str, Any]:
    """Each hint (and each item found only in the code) with its status, and the
    docs-vs-code mismatches. Confirmation is against real files only."""
    disc = model.get("discovery") or {}
    all_files = list(disc.get("all_files") or model.get("files") or [])
    files = set(all_files) | set(model.get("files") or [])
    structure = model.get("structure") or {}
    trace = structure.get("env_files", [])
    roots = sorted({r["dir"] for r in structure.get("roots", [])} | set(model.get("roots") or []))
    stack_ids = [s["id"] for s in model.get("stacks", [])]
    by_kind: dict[str, dict[str, dict[str, Any]]] = {"env": {}, "stack": {}, "link": {}}
    for h in hints:
        by_kind[h["kind"]][h["name"]] = h

    # environments: one item per name
    confirmed: dict[str, list[dict[str, Any]]] = {}
    for e in structure.get("environments", []):
        evidence = _env_evidence(e, files)
        e["evidence_files"] = evidence
        e["status"] = CONFIRMED if evidence else HINT_ONLY
        if evidence:
            confirmed.setdefault(e["name"], []).append(e)
    rejected: dict[str, dict[str, str]] = {}
    for t in trace:
        if not t["accepted"] and t["file"] in files:
            rejected.setdefault(t["file"], {"file": t["file"], "reason": t["reason"]})
    envs = []
    for name in sorted(set(confirmed) | set(by_kind["env"])):
        hint = by_kind["env"].get(name)
        item: dict[str, Any] = {
            "name": name,
            "hint_mentions": hint["mentions"] if hint else 0,
            "hint_sources": hint["sources"] if hint else [],
            "via": hint["via"] if hint else [],
        }
        if name in confirmed:
            ev = sorted({f for e in confirmed[name] for f in e["evidence_files"]})
            item.update(
                status=CONFIRMED,
                stacks=sorted({e["root"] for e in confirmed[name]}),
                evidence=ev,
                evidence_count=len(ev),
                kinds=sorted({k for e in confirmed[name] for k in e.get("kinds", [])}),
                reason=f"{len(ev)} real file(s): "
                + ", ".join(ev[:5])
                + (" …" if len(ev) > 5 else ""),
            )
            if hint is None:
                item["mismatch"] = "defined in the code, never mentioned in docs / CI / git"
        else:
            assert hint is not None
            item.update(status=HINT_ONLY, stacks=[], evidence=[], evidence_count=0, kinds=[])
            problems = []
            for f in hint.get("files", []):
                if f in rejected:
                    problems.append(
                        f"{f} exists but is not an environment file ({rejected[f]['reason']})"
                    )
                elif not _exists(f, all_files):
                    problems.append(f"names {f}, which is not in the repository")
            for p in hint.get("paths", []):
                if p not in hint.get("files", []) and not _exists(p, all_files):
                    problems.append(f"names {p}, which is not in the repository")
            if problems:
                item["status"] = CONTRADICTED
                item["reason"] = "; ".join(problems)
            else:
                item["reason"] = (
                    f"mentioned {hint['mentions']} time(s) in {', '.join(hint['via'])}; no "
                    "tfvars / backend file / environment directory in any stack defines it"
                )
            item["mismatch"] = item["reason"]
        envs.append(item)

    # stacks: the roots, and every directory named as one
    stacks = []
    for name in sorted(set(roots) | set(by_kind["stack"])):
        hint = by_kind["stack"].get(name)
        item = {
            "name": name,
            "hint_mentions": hint["mentions"] if hint else 0,
            "hint_sources": hint["sources"] if hint else [],
            "via": hint["via"] if hint else [],
        }
        inside = _owning(name, roots)
        if name in roots:
            item.update(status=CONFIRMED, reason="a root module / unit (its .tf / terragrunt.hcl)")
        elif inside:
            item.update(
                status=CONFIRMED,
                reason="directory holding the root(s) " + ", ".join(inside[:5]),
            )
        elif _exists(name, all_files):
            item.update(
                status=CONTRADICTED,
                reason=f"{name}/ exists but holds no root module (no backend / provider; "
                "a module or other directory)",
            )
        else:
            item.update(status=CONTRADICTED, reason=f"{name}/ is not in the repository")
        if hint is None:
            item["hinted"] = False
        elif item["status"] != CONFIRMED:
            item["mismatch"] = item["reason"]
        stacks.append(item)

    # links: remote state reads between stacks
    code_links = sorted(
        {
            (lk["from_stack"], lk["to_stack"])
            for lk in model.get("stack_links", [])
            if lk.get("from_stack") and lk.get("to_stack")
        }
    )
    links = []
    matched: set[tuple[str, str]] = set()
    for name, hint in sorted(by_kind["link"].items()):
        a, b = hint["ends"]
        item = {
            "name": name,
            "hint_mentions": hint["mentions"],
            "hint_sources": hint["sources"],
            "via": hint["via"],
        }
        match = [(f, t) for f, t in code_links if {_closest(f, a, b), _closest(t, a, b)} == {a, b}]
        if match:
            matched.update(match)
            item.update(status=CONFIRMED, reason="terraform_remote_state between them")
        elif _owning(a, roots) and _owning(b, roots):
            item.update(
                status=CONTRADICTED,
                reason="both are stacks, but no terraform_remote_state links them",
            )
        else:
            item.update(status=HINT_ONLY, reason="not both stacks of this repository")
        if item["status"] != CONFIRMED:
            item["mismatch"] = item["reason"]
        links.append(item)
    for f, t in code_links:
        if (f, t) not in matched:  # in the code, never named in docs / CI
            links.append(
                {
                    "name": f"{f} -> {t}",
                    "hint_mentions": 0,
                    "hint_sources": [],
                    "via": [],
                    "status": CONFIRMED,
                    "reason": "terraform_remote_state between them",
                    "hinted": False,
                }
            )

    mismatches = []
    for kind, items in (("env", envs), ("stack", stacks), ("link", links)):
        for it in items:
            if it.get("mismatch"):
                mismatches.append(
                    {
                        "kind": kind,
                        "name": it["name"],
                        "status": it["status"],
                        "text": it["mismatch"],
                    }
                )
    dd = model.get("docs_diff") or {}
    for entry in dd.get("documented_missing", []):
        mismatches.append(
            {
                "kind": "resource",
                "name": entry["ref"],
                "status": CONTRADICTED,
                "text": f"named in the docs ({len(entry['locations'])} place(s)), not declared in the code",
                "location": entry["locations"][0],
            }
        )

    def counts(items: list[dict[str, Any]]) -> dict[str, int]:
        out = {CONFIRMED: 0, HINT_ONLY: 0, CONTRADICTED: 0}
        for it in items:
            out[it["status"]] += 1
        return out

    return {
        "envs": envs,
        "stacks": stacks,
        "links": links,
        "stack_ids": stack_ids,
        "mismatches": mismatches,
        "counts": {"envs": counts(envs), "stacks": counts(stacks), "links": counts(links)},
    }


def _closest(path: str, a: str, b: str) -> str | None:
    """Which of two hinted directories a stack belongs to (None: neither)."""
    hits = [x for x in (a, b) if path == x or in_scope(path, x)]
    return max(hits, key=len) if hits else None


def confirmed_only(model: dict[str, Any]) -> None:
    """Render from confirmed data only: an environment with no real Terraform file behind
    it is dropped from the structure, the stacks and the per-environment columns."""
    structure = model.get("structure") or {}
    envs = structure.get("environments", [])
    keep = [e for e in envs if e.get("status", CONFIRMED) == CONFIRMED]
    if len(keep) == len(envs):
        return
    dropped = {e["name"] for e in envs} - {e["name"] for e in keep}
    structure["environments"] = keep
    for s in model.get("stacks", []):  # environment ids: name, or <root>:<name>
        s["environments"] = [e for e in s["environments"] if e.split(":")[-1] not in dropped]
    analysis = model.get("environment_analysis") or {}
    for key in ("columns", "environments"):
        if key in analysis:
            analysis[key] = [
                c for c in analysis[key] if str(c.get("id", "")).split(":")[-1] not in dropped
            ]


# ------------------------------------------------------------------ the pipeline


def discover(
    root: Path,
    options: Options | None = None,
    progress: Progress | None = None,
    write: Callable[[Analysis], T] | None = None,
) -> Discovered:
    """Run the four phases on ``root``; ``write`` (phase 4) persists the result, called
    only once nothing can cancel the run any more. Raises :class:`Cancelled` (nothing
    written) when the progress object says so."""
    from .awscheck import run_checks

    opts = options or Options()
    prog = progress or NullProgress()
    phases: list[dict[str, Any]] = []

    def done(name: str, status: str, detail: str) -> None:
        phases.append(
            {"name": name, "label": dict(PHASES)[name], "status": status, "detail": detail}
        )
        prog.phase_done(name, status, detail)
        prog.log(f"{dict(PHASES)[name]}: {status}" + (f" ({detail})" if detail else ""))

    # 1. crawl + hypothesis
    prog.phase("crawl", dict(PHASES)["crawl"])
    prog.step("listing every file")
    repo = load_repo(root)
    skipped = [s.to_dict() for s in repo.skipped]
    prog.log(
        f"{len(repo.all_files)} file(s) listed, {len(skipped)} director(ies) skipped"
        + (" (listing truncated)" if repo.truncated else "")
    )
    tree = tree_lines(repo.all_files, skipped)
    prog.step("reading docs, CI and git history (hints only)")
    crawled = crawl(repo, opts.git_root, opts.git)
    prog.step("drafting the map of stacks, environments and links")
    hints = hypothesize(repo, crawled)
    n_by = {k: sum(1 for h in hints if h["kind"] == k) for k in ("stack", "env", "link")}
    prog.log(
        f"hints: {n_by['env']} environment(s), {n_by['stack']} stack(s), {n_by['link']} link(s) "
        f"from {len(hint_sources(repo.all_files))} doc / CI file(s) and the git log"
    )
    done("crawl", "done", f"{len(repo.all_files)} files, {len(hints)} hints")

    # 2. validate
    prog.phase("validate", dict(PHASES)["validate"])
    analysis = run_analysis(
        root,
        opts.var_files,
        opts.cli_vars,
        scope=opts.scope,
        git_root=opts.git_root,
        git=opts.git,
        env=opts.env,
        auto_env=opts.auto_env,
        stack_envs=opts.stack_envs,
        repo=repo,
        crawled=crawled,
        step=prog.step,
    )
    model = analysis.model
    prog.step("confirming the hints against .tf / .tfvars / backend files")
    validation = validate(model, hints)
    c = validation["counts"]["envs"]
    prog.log(
        f"environments: {c[CONFIRMED]} confirmed, {c[HINT_ONLY]} hint-only, "
        f"{c[CONTRADICTED]} contradicted; {len(validation['mismatches'])} docs-vs-code mismatch(es)"
    )
    done("validate", "done", f"{c[CONFIRMED]} environment(s) confirmed")

    # 3. AWS (optional)
    prog.phase("aws", dict(PHASES)["aws"])
    aws = run_checks(model, opts.aws, opts.aws_requested, prog)
    done("aws", aws["status"], aws["message"])

    # 4. render
    prog.phase("render", dict(PHASES)["render"])
    prog.step("building the views from confirmed data")
    confirmed_only(model)
    model["pipeline"] = {
        "phases": phases
        + [{"name": "render", "label": dict(PHASES)["render"], "status": "done", "detail": ""}],
        "crawl": {
            "files": len(repo.all_files),
            "truncated": repo.truncated,
            "skipped": skipped,
            "tree": tree,
            "hint_files": [{"file": f, "via": v} for f, v in hint_sources(repo.all_files)],
        },
        "hints": hints,
        "validation": validation,
        "aws": aws,
    }
    prog.step("writing the results")  # the last point a cancel takes effect
    written = write(analysis) if write is not None else None
    prog.phase_done("render", "done", "")
    prog.log("Render: done")
    return Discovered(analysis, written)
