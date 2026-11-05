"""What the repository's CI files and scripts say, checked against the files on disk.

Docs never count here: only CI configuration and scripts are read (:func:`ci_files`), and a
line of them is a lead until the files it names are found. :func:`env_commands` gives each
stack + environment its ``init`` / ``plan`` / ``apply`` command once: the CI line whose
directory is that stack and whose ``-var-file`` / ``-backend-config`` files are that
environment's real files, else the command those real files make. Lines that do not check
out go to the debug log only. :func:`ci_var_inputs` lists the variables CI sets.
"""

from __future__ import annotations

import logging
import posixpath
import re
from typing import Any

from .crawl import ci_system
from .loader import RepoSource
from .values import Location

log = logging.getLogger(__name__)

SCRIPT_NAMES = frozenset({"Makefile", "makefile", "GNUmakefile", "Taskfile.yml", "justfile"})
_SCRIPT_SUFFIXES = (".sh", ".mk", ".bash", ".ps1")
# terraform init -backend-config=FILE, plan -var-file=FILE (key=value pairs and CI
# expressions in the path are handled by the callers)
CLI_FLAG_RE = re.compile(r"(?<![\w-])--?(backend-config|var-file)(?:=|\s+)['\"]?([^\s'\";|&)]+)")
CHDIR_RE = re.compile(r"(?<![\w-])--?chdir=['\"]?([^\s'\";|&)]+)")
# ${{ matrix.env }}, ${ENV}, $ENV, $(ENV), %ENV%, <env>: one path segment, per environment
CI_EXPR_RE = re.compile(r"\$\{\{[^}]*\}\}|\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z_]\w*|%\w+%|<[\w-]+>")
SECRET_KEY_RE = re.compile(r"(secret|password|token|access_key|sas|client_certificate)", re.I)

_VAR_FLAG_RE = re.compile(r"(?<![\w-])--?var(?:=|\s+)['\"]?([A-Za-z_][\w-]*)=([^'\"\s]*)")
_TF_VAR_RE = re.compile(r"\bTF_VAR_([A-Za-z_]\w*)\s*[:=]\s*['\"]?([^'\"\s#]*)")
_WORKSPACE_RE = re.compile(r"\b(?:terraform|tofu)\s+workspace\s+(?:select|new)\b")
_TOOL_RE = re.compile(r"(?<![\w./-])(terraform|tofu|terragrunt)\b(.*)$")
_STEP_RE = re.compile(r"(?:^|\s)(?:run-all\s+)?(init|plan|apply)(?=\s|$)")
_CD_RE = re.compile(r"(?:^|[\s;&(])cd\s+['\"]?([^\s'\";|&)]+)")
_DIR_KEY_RE = re.compile(
    r"^\s*(?:-\s*)?(?:working[-_]directory|working_dir|workdir|dir)\s*:\s*(.+?)\s*$"
)
_STEP_START_RE = re.compile(r"^\s*-\s+\S")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"\b(\w*(?:secret|password|token|access_key|sas|client_certificate)\w*)=[^\s'\"]+", re.I
)
STEPS = ("init", "plan", "apply")


def ci_files(repo: RepoSource) -> list[str]:
    """The CI configuration and script files of the repository (never docs)."""
    out = []
    for f in repo.all_files:
        base = posixpath.basename(f)
        if ci_system(f) or base in SCRIPT_NAMES or base.endswith(_SCRIPT_SUFFIXES):
            out.append(f)
    return out


def _logical_lines(lines: list[str]) -> list[tuple[int, str]]:
    """``(first line number, text)`` with ``\\`` continuations joined; comments left out."""
    out: list[tuple[int, str]] = []
    start = 0
    buf = ""
    for n, line in enumerate(lines, 1):
        text = line.rstrip()
        if not buf and text.lstrip().startswith("#"):
            continue
        if not buf:
            start = n
        if text.endswith("\\"):
            buf += text[:-1].rstrip() + " "
            continue
        out.append((start, " ".join((buf + text).split()) if buf else text))
        buf = ""
    if buf:
        out.append((start, " ".join(buf.split())))
    return out


def _scan(repo: RepoSource) -> dict[str, list[tuple[int, str]]]:
    cached = getattr(repo, "_ci_lines", None)
    if cached is None:
        cached = {f: _logical_lines(repo.lines(f)) for f in ci_files(repo)}
        repo._ci_lines = cached  # type: ignore[attr-defined]
    return cached


def _literal(value: str) -> bool:
    return bool(value) and not re.search(r"[$%<>{}]", value)


def ci_var_inputs(repo: RepoSource) -> dict[str, tuple[str | None, Location]]:
    """Variables CI / scripts set (``-var name=value``, ``TF_VAR_name``): ``name -> (value,
    line)``. The value is None when it is computed at run time, differs between lines, or
    the name reads as a secret (its value is never kept)."""
    out: dict[str, tuple[str | None, Location]] = {}
    for f, lines in sorted(_scan(repo).items()):
        for n, text in lines:
            for rx in (_VAR_FLAG_RE, _TF_VAR_RE):
                for m in rx.finditer(text):
                    name, raw = m.group(1), m.group(2)
                    value = raw if _literal(raw) and not SECRET_KEY_RE.search(name) else None
                    if name in out and out[name][0] != value:
                        out[name] = (None, out[name][1])
                    else:
                        out.setdefault(name, (value, Location(f, n, n)))
    return out


def ci_workspace_use(repo: RepoSource) -> str:
    """``selects`` (CI runs ``terraform workspace select``), ``never`` (CI runs terraform and
    never selects one: the default workspace) or ``none`` (no CI running terraform)."""
    runs = False
    for lines in _scan(repo).values():
        for _, text in lines:
            if _WORKSPACE_RE.search(text):
                return "selects"
            if _TOOL_RE.search(text):
                runs = True
    return "never" if runs else "none"


# ------------------------------------------------------------------- commands


def _command_lines(repo: RepoSource) -> list[dict[str, Any]]:
    """Every ``terraform | tofu | terragrunt init | plan | apply`` line of CI / scripts."""
    cached = getattr(repo, "_ci_commands", None)
    if cached is not None:
        return cached
    out: list[dict[str, Any]] = []
    for f, lines in sorted(_scan(repo).items()):
        raw = repo.lines(f)
        for n, text in lines:
            cd = None
            for seg in re.split(r"&&|\|\||;", text):
                m_cd = _CD_RE.search(seg)
                if m_cd and not _TOOL_RE.search(seg):
                    cd = m_cd.group(1)
                    continue
                m = _TOOL_RE.search(seg)
                if m is None:
                    continue
                rest = re.split(r"\s\|\s|\s>|\s2>", m.group(2))[0].strip().rstrip("'\"`,)")
                step = _STEP_RE.search(rest)
                if step is None:
                    continue
                out.append(
                    {
                        "tool": m.group(1),
                        "step": step.group(1),
                        "text": " ".join(f"{m.group(1)} {rest}".split()),
                        "file": f,
                        "line": n,
                        "dir": cd or _step_dir(raw, n),
                    }
                )
    repo._ci_commands = out  # type: ignore[attr-defined]
    return out


def _step_dir(lines: list[str], n: int) -> str | None:
    """The working directory of the CI step holding line ``n``: its ``working-directory``,
    a ``cd`` before it in the same step, else the file's default."""
    start = n - 1
    while start > 0 and not _STEP_START_RE.match(lines[start]):
        start -= 1
    end = n
    while end < len(lines) and not _STEP_START_RE.match(lines[end]):
        end += 1
    for line in lines[start:end]:
        m = _DIR_KEY_RE.match(line)
        if m:
            return m.group(1).strip("'\"")
    for line in reversed(lines[start : n - 1]):
        m = _CD_RE.search(line)
        if m and not _TOOL_RE.search(line):
            return m.group(1)
    for i, line in enumerate(lines):
        if line.strip().startswith("defaults:"):
            for below in lines[i + 1 : i + 4]:
                m = _DIR_KEY_RE.match(below)
                if m:
                    return m.group(1).strip("'\"")
    return None


def _mentions(text: str, name: str) -> bool:
    """Whether ``name`` appears in ``text`` as a word (``[dev, uat]``, ``ENV=dev``)."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text) is not None


def _pattern(text: str) -> tuple[re.Pattern[str], list[str]]:
    """A path with CI expressions -> a regex (one path segment per expression) and them."""
    exprs: list[str] = []
    parts: list[str] = []
    pos = 0
    for m in CI_EXPR_RE.finditer(text):
        parts.append(re.escape(text[pos : m.start()]))
        parts.append("([^/]+)")
        exprs.append(m.group(0))
        pos = m.end()
    parts.append(re.escape(text[pos:]))
    return re.compile("".join(parts)), exprs


def _norm(path: str) -> str:
    return posixpath.normpath(path.strip("'\"").removeprefix("./")) if path else "."


def _match_dir(value: str | None, root: str, binds: dict[str, str]) -> bool:
    """Whether a command's directory is this root (binding its CI expressions)."""
    if value is None:
        return False
    rx, exprs = _pattern(_norm(value))
    m = rx.fullmatch(root)
    if m is None:
        return False
    binds.update(zip(exprs, m.groups(), strict=True))
    return True


def _match_file(value: str, bases: list[str], pool: set[str], binds: dict[str, str]) -> str | None:
    """The file of ``pool`` a ``-var-file`` / ``-backend-config`` value names."""
    for base in bases:
        joined = posixpath.normpath(posixpath.join(base, value)) if base not in ("", ".") else value
        rx, exprs = _pattern(joined.removeprefix("./"))
        for f in sorted(pool):
            m = rx.fullmatch(f)
            if m is not None:
                binds.update(zip(exprs, m.groups(), strict=True))
                return f
    return None


def _check_command(
    cand: dict[str, Any],
    env: dict[str, Any],
    root: str,
    text_of: dict[str, str],
) -> tuple[int, dict[str, str]] | None:
    """How many real files of this environment a CI command names (with the values of its
    CI expressions), or None when it is not a command of this stack + environment."""
    binds: dict[str, str] = {}
    exprs: list[str] = []

    def hold(m: re.Match[str]) -> str:  # ${{ matrix.env }} has spaces: one token for the flags
        exprs.append(m.group(0))
        return f"\x00{len(exprs) - 1}\x00"

    def restore(value: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: exprs[int(m.group(1))], value)

    held = CI_EXPR_RE.sub(hold, cand["text"])
    chdir = CHDIR_RE.search(held)
    directory = restore(chdir.group(1)) if chdir else cand["dir"]
    in_root = _match_dir(directory, root, binds)
    here = posixpath.dirname(cand["file"]) or "."
    if directory is not None and not in_root:
        return None
    mine = set(env["var_files"]) | set(env.get("backend_hcl", []))
    score = 0
    slotted = False
    for flag in CLI_FLAG_RE.finditer(held):
        value = restore(flag.group(2))
        if "=" in flag.group(2):  # -backend-config="key=value": a setting, not a file
            continue
        bases = [root, here, ""]
        if _match_file(value, bases, mine, binds) is None:
            return None  # another environment's file, or one that is not there
        score += 1
        slotted = slotted or bool(CI_EXPR_RE.search(value))
    if slotted:
        # ${{ matrix.env }} stands for the environments its file lists, when it lists any
        others = [o for o in env.get("_siblings", []) if o != env["name"]]
        text = text_of[cand["file"]]
        if not _mentions(text, env["name"]) and any(_mentions(text, o) for o in others):
            return None
    if not score:
        saved_plan = cand["step"] == "apply" and bool(
            re.search(r"\bapply\b(?:\s+-\S+)*\s+(?!-)[\w.$/{}-]+$", cand["text"])
        )
        needs = env["var_files"] if cand["step"] != "init" else env.get("backend_hcl", [])
        if needs and cand["tool"] != "terragrunt" and not saved_plan:
            return None  # names none of this environment's files
        if directory is None and root != "." and not in_root and not _in_dir(here, root):
            return None  # nothing ties it to this stack
    return score, binds


def _in_dir(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def _bound_text(text: str, binds: dict[str, str]) -> str:
    """The command with its CI expressions replaced, and no secret value in it."""
    out = CI_EXPR_RE.sub(lambda m: binds.get(m.group(0), m.group(0)), text)
    return _ASSIGNMENT_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", out)


def _from_files(env: dict[str, Any], root: str, repo: RepoSource) -> list[dict[str, Any]]:
    """The commands an environment's real files make: what a run from the stack's directory
    needs, located at those files."""
    files = set(repo.all_files)
    roots = env.get("roots") or [root]
    kinds = set(env.get("kinds") or [])

    def rel(f: str) -> str:
        return f if root == "." else posixpath.relpath(f, root)

    out = []
    backend = [f for f in env.get("backend_hcl", []) if f in files]
    var_files = [f for f in env["var_files"] if f in files]
    terragrunt = "terragrunt" in kinds
    if terragrunt and len(roots) > 1:
        tool, directory = "terragrunt run-all", env["root"]
    elif terragrunt:
        tool, directory = "terragrunt", root
    else:
        chdir = "" if root == "." else f" -chdir={root}"
        tool, directory = f"terraform{chdir}", root
    for step in STEPS:
        flags = []
        used = []
        if not terragrunt and step == "init":
            flags = [f"-backend-config={rel(f)}" for f in backend]
            used = backend
        elif not terragrunt and step in ("plan", "apply"):
            flags = [f"-var-file={rel(f)}" for f in var_files]
            used = var_files
        where = (used or backend or var_files or [None])[0]
        out.append(
            {
                "step": step,
                "command": " ".join([tool, step, *flags]),
                "dir": directory,
                "source": "files",
                "location": Location(where, 1, 1).to_dict() if where else None,
            }
        )
    return out


def env_commands(
    repo: RepoSource, env: dict[str, Any], siblings: list[str] | None = None
) -> list[dict[str, Any]]:
    """``init`` / ``plan`` / ``apply`` of one stack + environment, once each: the CI line
    naming most of its real files (``source: ci``, with its file:line), else the command
    its real files make (``source: files``)."""
    root = (env.get("roots") or [env["root"]])[0]
    if len(env.get("roots") or []) > 1:
        root = env["root"]
    text_of = {f: "\n".join(repo.lines(f)) for f in _scan(repo)}
    best: dict[str, tuple[int, dict[str, Any], dict[str, str]]] = {}
    view = {**env, "_siblings": siblings or []}
    for cand in _command_lines(repo):
        checked = _check_command(cand, view, root, text_of)
        if checked is None:
            log.debug(
                "command at %s:%d not verified for %s (%s)",
                cand["file"],
                cand["line"],
                env["name"],
                root,
            )
            continue
        score, binds = checked
        current = best.get(cand["step"])
        if current is None or score > current[0]:
            best[cand["step"]] = (score, cand, binds)
    derived = {c["step"]: c for c in _from_files(env, root, repo)}
    out = []
    for step in STEPS:
        if step not in best:
            out.append(derived[step])
            continue
        _, cand, binds = best[step]
        out.append(
            {
                "step": step,
                "command": _bound_text(cand["text"], binds),
                "dir": root,
                "source": "ci",
                "location": Location(cand["file"], cand["line"], cand["line"]).to_dict(),
            }
        )
    return out
