"""Deep crawl of what surrounds the Terraform code: docs, CI, lock files and git history.

Read-only and offline. The only I/O is reading repository files and, when the analyzed
directory is a git checkout, one ``git log`` (no fetch, no network, no optional locks).
Everything found carries its ``file:line``; whatever cannot be read is listed in
``notes`` with the reason, never skipped silently. The result is the ``crawl`` section of
model.json and depends only on the files and the checked-out commit (deterministic).
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .docsdiff import doc_sources
from .loader import RepoSource

MAX_DOCS = 30
MAX_DOC_BYTES = 256 * 1024
MAX_EXCERPT = 400
MAX_HEADINGS = 12
MAX_CI_COMMANDS = 50
GIT_LOG_LIMIT = 200
GIT_TIMEOUT = 60.0
MAX_BODY = 500
MAX_HOT_FILES = 15
MAX_AUTHORS = 10

DOC_EXTENSIONS = ("", ".md", ".markdown", ".rst", ".txt", ".adoc")
DOC_DIRS = ("docs", "doc", "documentation")
# Files TFLens may show as source (besides the analyzed files): text formats only.
TEXT_EXTENSIONS = frozenset(
    {
        ".tf",
        ".json",
        ".hcl",
        ".tfvars",
        ".tfbackend",
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".adoc",
        ".yml",
        ".yaml",
        ".py",
        ".sh",
        ".ps1",
        ".js",
        ".ts",
        ".go",
        ".toml",
        ".cfg",
        ".ini",
        ".tpl",
    }
)
TEXT_NAMES = frozenset({"Jenkinsfile", "Makefile", "Dockerfile", "README", ".gitlab-ci.yml"})

# CI systems: (system, path regex)
CI_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub Actions", re.compile(r"(^|/)\.github/workflows/[^/]+\.ya?ml$")),
    ("GitLab CI", re.compile(r"(^|/)\.gitlab-ci\.ya?ml$")),
    ("Jenkins", re.compile(r"(^|/)Jenkinsfile([._-][^/]*)?$")),
    ("Atlantis", re.compile(r"(^|/)atlantis\.ya?ml$")),
    ("CircleCI", re.compile(r"(^|/)\.circleci/config\.ya?ml$")),
    ("Bitbucket Pipelines", re.compile(r"(^|/)bitbucket-pipelines\.ya?ml$")),
    ("Azure Pipelines", re.compile(r"(^|/)azure-pipelines\.ya?ml$")),
)
_TOOL_RE = re.compile(r"\b(terraform|terragrunt|tofu|tflint|tfsec|checkov|infracost|atlantis)\b")
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z-]+$")
_YAML_KEY_RE = re.compile(r"^\s*(?:-\s+)?([\w.-]+)\s*:(?:\s|$)")
_COMMAND_KEYS = frozenset({"run", "script", "sh", "command", "cmd", "commands", "steps"})
_README_RE = re.compile(r"^readme([._-][^/]*)?$", re.I)
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_NOISE_RE = re.compile(r"^\s*(!\[|\[!\[|<|\||```|~~~|---|\*\*\*|>|\.\. )")
_KEY_RE = re.compile(r"^\s*(?:-\s+)?(dir|workspace|name|terraform_version|project)\s*:\s*(.+?)\s*$")
_GIT_ENV = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "USER"})
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _loc(file: str, start: int, end: int | None = None) -> dict[str, Any]:
    return {"file": file, "start_line": start, "end_line": end or start}


def _depth(path: str) -> int:
    return path.count("/")


def _read_text(root: Path, rel: str, notes: list[dict[str, str]]) -> list[str] | None:
    path = root / rel
    try:
        path.resolve().relative_to(root)
        if path.stat().st_size > MAX_DOC_BYTES:
            notes.append({"path": rel, "reason": f"larger than {MAX_DOC_BYTES} bytes: not read"})
            return None
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        notes.append({"path": rel, "reason": "not UTF-8 text: not read"})
    except (OSError, ValueError) as exc:
        notes.append({"path": rel, "reason": f"cannot read: {getattr(exc, 'strerror', exc)}"})
    return None


def is_text(path: str) -> bool:
    base = posixpath.basename(path)
    if base in TEXT_NAMES or base.startswith("Jenkinsfile"):
        return True
    return posixpath.splitext(base)[1].lower() in TEXT_EXTENSIONS


# ------------------------------------------------------------------ docs


def doc_files(all_files: list[str]) -> list[str]:
    """READMEs at any depth, the files of docs/ directories (wherever they are) and every
    other Markdown file."""
    out = []
    for f in all_files:
        base = posixpath.basename(f)
        parts = posixpath.dirname(f).lower().split("/")
        ext = posixpath.splitext(base)[1].lower()
        if ext not in DOC_EXTENSIONS or base.startswith("."):
            continue
        readme = bool(_README_RE.match(base))
        in_docs = any(p in DOC_DIRS for p in parts) and bool(ext or readme)
        if readme or in_docs or ext in (".md", ".markdown"):
            out.append(f)
    # top-level README first, then by depth and path
    out.sort(key=lambda f: (_depth(f), not _README_RE.match(posixpath.basename(f)), f))
    return out


def _doc(rel: str, lines: list[str]) -> dict[str, Any]:
    headings: list[dict[str, Any]] = []
    for n, line in enumerate(lines, 1):
        m = _HEADING_RE.match(line)
        level, text = (len(m.group(1)), m.group(2)) if m else (0, "")
        if not m and n > 1 and _SETEXT_RE.match(line) and lines[n - 2].strip():
            level, text, n = (1 if line.strip()[0] == "=" else 2), lines[n - 2].strip(), n - 1
        if level and len(headings) < MAX_HEADINGS:
            headings.append({"level": level, "text": text, "location": _loc(rel, n)})
    title = headings[0]["text"] if headings else ""
    title_loc = headings[0]["location"] if headings else None
    excerpt, ex_loc = _excerpt(rel, lines)
    if not title and excerpt:
        title, title_loc = excerpt.split(". ")[0][:120], ex_loc
    return {
        "path": rel,
        "title": title or posixpath.basename(rel),
        "title_location": title_loc or _loc(rel, 1),
        "excerpt": excerpt,
        "excerpt_location": ex_loc or _loc(rel, 1),
        "headings": headings,
        "lines": len(lines),
    }


def _excerpt(rel: str, lines: list[str]) -> tuple[str, dict[str, Any] | None]:
    """The first prose paragraph (no headings, badges, HTML, tables or code)."""
    start, para = 0, []
    in_code = False
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            if para:
                break
            continue
        if in_code:
            continue
        nxt = lines[n] if n < len(lines) else ""
        prose = (
            stripped
            and not _HEADING_RE.match(line)
            and not _SETEXT_RE.match(line)
            and not _SETEXT_RE.match(nxt)
            and not _NOISE_RE.match(line)
        )
        if prose:
            if not para:
                start = n
            para.append(stripped)
        elif para:
            break
    if not para:
        return "", None
    text = " ".join(para)
    if len(text) > MAX_EXCERPT:
        text = text[: MAX_EXCERPT - 1].rsplit(" ", 1)[0] + "…"
    return text, _loc(rel, start, start + len(para) - 1)


# ------------------------------------------------------------------ CI


def ci_system(path: str) -> str | None:
    return next((name for name, rx in CI_PATTERNS if rx.search(path)), None)


def _ci(rel: str, system: str, lines: list[str]) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    name, name_loc = "", None
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith(("#", "//")):
            continue
        if not name and system != "Atlantis" and re.match(r"^name\s*:\s*\S", line):
            name, name_loc = line.split(":", 1)[1].strip().strip("'\""), _loc(rel, n)
        m = _TOOL_RE.search(line)
        key = _YAML_KEY_RE.match(line)
        if key and key.group(1) not in _COMMAND_KEYS:
            continue  # a YAML setting (name:, workflow:, uses:, ...), not a command
        if m and len(commands) < MAX_CI_COMMANDS:
            text = line[m.start() :].strip().rstrip("'\";)").strip()
            commands.append(
                {
                    "tool": m.group(1),
                    "command": _subcommand(line[m.end() :]),
                    "text": text[:160],
                    "location": _loc(rel, n),
                }
            )
    out: dict[str, Any] = {
        "path": rel,
        "system": system,
        "name": name or posixpath.basename(rel),
        "location": name_loc or _loc(rel, 1),
        "commands": commands,
    }
    if system == "Atlantis":
        out["projects"] = _atlantis_projects(rel, lines)
    return out


def _subcommand(rest: str) -> str:
    """The sub-command after a tool name: options (``-chdir=...``) and CI expressions
    (``${{ matrix.env }}``) are skipped."""
    depth = 0
    for token in rest.split():
        opens, closes = token.count("{{"), token.count("}}")
        if depth or opens or closes or token.startswith("-") or "$" in token:
            depth = max(depth + opens - closes, 0)
            continue
        return token if _SUBCOMMAND_RE.match(token) else ""
    return ""


def _atlantis_projects(rel: str, lines: list[str]) -> list[dict[str, Any]]:
    """``projects:`` items of atlantis.yaml (line-based: no YAML library needed)."""
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_projects = False
    for n, line in enumerate(lines, 1):
        if re.match(r"^projects\s*:", line):
            in_projects = True
            continue
        if in_projects and re.match(r"^\S", line) and not line.startswith("-"):
            in_projects = False
        if not in_projects:
            continue
        if re.match(r"^\s*-\s", line):
            current = {"location": _loc(rel, n)}
            items.append(current)
        m = _KEY_RE.match(line)
        if m and current is not None and m.group(1) not in current:
            current[m.group(1)] = m.group(2).strip("'\"")
            current["location"] = {**current["location"], "end_line": n}
    return [
        {
            "dir": p.get("dir", "."),
            "workspace": p.get("workspace", "default"),
            "name": p.get("name", ""),
            "location": p["location"],
        }
        for p in items
        if "dir" in p
    ]


# ------------------------------------------------------------------ lock files


def _locks(repo: RepoSource) -> list[dict[str, Any]]:
    out = []
    for d in sorted(repo.locks):
        providers = repo.locks[d]
        if not providers:
            continue
        file = f"{d}/.terraform.lock.hcl" if d != "." else ".terraform.lock.hcl"
        out.append(
            {
                "file": file,
                "dir": d,
                "providers": [
                    {"source": src, "version": v, "location": loc.to_dict()}
                    for src, (v, loc) in sorted(providers.items())
                ],
            }
        )
    return out


# ------------------------------------------------------------------ git


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _GIT_ENV}
    # read-only: no system config, no prompts, no optional index lock / refresh
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return env


def git_log(root: Path, git_root: Path | None = None, limit: int = GIT_LOG_LIMIT) -> dict[str, Any]:
    """The last ``limit`` commits touching ``root``: sha, author, date, message, files
    (relative to ``root``). Only when ``git_root`` (default ``root``) is a git checkout
    containing ``root``; otherwise ``status`` says why nothing was read."""
    git_root = (git_root or root).resolve()
    root = root.resolve()
    out: dict[str, Any] = {"status": "ok", "reason": "", "head": "", "commits": []}
    try:
        root.relative_to(git_root)
    except ValueError:
        return {
            **out,
            "status": "skipped",
            "reason": "the analyzed directory is outside the checkout",
        }
    if not (git_root / ".git").exists():
        return {**out, "status": "skipped", "reason": "not a git checkout (no .git)"}
    fmt = "%x1e%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1d"
    argv = [
        "git",
        "--no-pager",
        "-c",
        "core.quotepath=off",
        "-C",
        str(root),
        "log",
        f"-n{limit}",
        "--no-color",
        "--no-renames",
        "--relative",
        "--name-only",
        f"--format={fmt}",
        "HEAD",
        "--",
        ".",
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {**out, "status": "failed", "reason": f"cannot run git ({type(exc).__name__})"}
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        reason = err[-1][:300] if err else f"git log exited with {proc.returncode}"
        return {**out, "status": "failed", "reason": reason}
    commits = parse_git_log(proc.stdout)
    out["commits"] = commits
    out["head"] = commits[0]["sha"] if commits else ""
    out["truncated"] = len(commits) >= limit
    if not commits:
        out["reason"] = "no commits touch this directory"
    return out


def parse_git_log(text: str) -> list[dict[str, Any]]:
    commits = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        header, _, names = record.partition("\x1d")
        fields = header.split("\x1f")
        if len(fields) < 5 or not _SHA_RE.match(fields[0]):
            continue
        sha, author, date, subject, body = fields[:5]
        body = body.strip()
        if date.endswith("Z"):  # git versions differ: 2026-01-01T10:00:00Z / +00:00
            date = date[:-1] + "+00:00"
        commits.append(
            {
                "sha": sha,
                "short": sha[:8],
                "author": author,
                "date": date,
                "subject": subject,
                "body": body if len(body) <= MAX_BODY else body[: MAX_BODY - 1] + "…",
                "files": sorted({ln.strip() for ln in names.splitlines() if ln.strip()}),
            }
        )
    return commits


def _history(git: dict[str, Any], all_files: set[str]) -> dict[str, Any]:
    changes: Counter[str] = Counter()
    authors: Counter[str] = Counter()
    for c in git["commits"]:
        changes.update(c["files"])
        authors[c["author"]] += 1
    hot = sorted(changes.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_HOT_FILES]
    return {
        "hot_files": [{"path": p, "commits": n, "exists": p in all_files} for p, n in hot],
        "authors": [
            {"name": a, "commits": n}
            for a, n in sorted(authors.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_AUTHORS]
        ],
    }


# ------------------------------------------------------------------ entry point


def crawl(repo: RepoSource, git_root: Path | None = None, git: bool = True) -> dict[str, Any]:
    """The ``crawl`` section of model.json."""
    notes: list[dict[str, str]] = []
    docs = []
    for rel in doc_files(repo.all_files)[:MAX_DOCS]:
        lines = _read_text(repo.root, rel, notes)
        if lines is not None:
            docs.append(_doc(rel, lines))
    ci = []
    for rel in repo.all_files:
        system = ci_system(rel)
        if system is None:
            continue
        lines = _read_text(repo.root, rel, notes)
        if lines is not None:
            ci.append(_ci(rel, system, lines))
    log = (
        git_log(repo.root, git_root)
        if git
        else {"status": "skipped", "reason": "git history disabled", "head": "", "commits": []}
    )
    all_files = set(repo.all_files)
    history = _history(log, all_files)
    readable = {d["path"] for d in docs} | {c["path"] for c in ci}
    readable |= {h["path"] for h in history["hot_files"] if h["exists"] and is_text(h["path"])}
    readable |= {f for c in log["commits"] for f in c["files"] if f in all_files and is_text(f)}
    readable |= {lk["file"] for lk in _locks(repo)}
    readable |= set(doc_sources(repo))  # the docs vs code diff links to their lines
    return {
        "docs": docs,
        "ci": ci,
        "locks": _locks(repo),
        "git": {**log, **history},
        "notes": sorted(notes, key=lambda n: n["path"]),
        "readable": sorted(readable),
    }
