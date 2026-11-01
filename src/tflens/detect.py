"""Zero-config structure detection: roots, backends, providers, modules, environments.

Everything here is derived from the loaded repository and the engine (no user input,
no terraform / terragrunt run, no network). Every fact carries the location it was read
from; a value that cannot be known statically is reported as ``UNKNOWN`` with the reason.

* **Roots**: directories that are not called as a module by another directory, plus
  Terragrunt units. Each root lists its *signals*: ``backend`` / ``cloud`` / ``provider``
  / ``terraform`` blocks, ``terragrunt.hcl``.
* **Backend** per root: type and the type's key settings (``bucket`` / ``key`` /
  ``dynamodb_table`` for s3, ...), from the ``backend`` block, a ``*.tfbackend`` partial
  configuration next to it, or Terragrunt's ``remote_state``; plus the workspace setup.
* **Providers**: ``required_providers`` constraints, versions locked in
  ``.terraform.lock.hcl``, ``required_version`` and ``provider`` configurations.
* **Modules**: every module call (local / registry / git / ...), its version, call site
  and inputs; a Terragrunt unit's ``terraform.source`` with its ``inputs``.
* **Environments**: from tfvars files (``envs/dev.tfvars``, ``vars/prod.tfvars``),
  env-named / ``envs/*`` directories that are roots, and Terragrunt layouts
  (``live/<env>/...``, ``env.hcl``).
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from .engine import Engine, ModuleInstance, in_scope
from .evaluate import evaluate
from .expr import raw_to_expr
from .loader import (
    ENV_CONTAINERS,
    ENV_WORDS,
    Attr,
    Block,
    ModuleSource,
    RepoSource,
    map_keys,
    read_attrs,
    read_blocks,
)
from .sources import classify_source
from .tags import tagging
from .terragrunt import Unit, substitute_raw, unit_context
from .values import Location, Unknown, display, is_known, to_plain

UNKNOWN = "UNKNOWN"
MAX_VALUE = 160
MAX_WORKSPACE_REFS = 5

# Directories holding per-environment tfvars files.
VAR_DIRS = frozenset({"vars", "tfvars", "varfiles", "var-files", "config", "configs"})
NOT_ENVS = frozenset(
    {"modules", "module", "common", "shared", "global", "templates", "lib", "scripts", "docs"}
)
# tfvars stems applied to every environment of the same root rather than being one.
COMMON_STEMS = frozenset({"common", "shared", "global", "defaults", "default", "base"})
GENERIC_STEMS = frozenset({"terraform", "vars", "variables", "values", "main", "inputs"})
_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
_ENV_NAME_BAD = re.compile(r"[^A-Za-z0-9_.-]+")
# Partial backend configurations named so: dev.tfbackend, backend.dev.hcl, backend-dev.hcl,
# dev.backend.hcl, backend/dev.hcl, backend-config/dev.hcl (an ``*.hcl`` only named after an
# environment, env/dev.hcl, is one when its settings are backend settings: is_backend_file)
_BACKEND_FILE_RE = re.compile(
    r"(\.tfbackend|(^|/)backend[^/]*\.(hcl|tfvars|conf)|[._-]backends?\.(hcl|tfvars|conf)"
    r"|(^|/)backends?([-_]?configs?)?/[^/]+\.(hcl|tfvars|conf))$"
)
_BACKEND_WORD_RE = re.compile(r"(^|[._-])backends?(?=[._-]|$)", re.I)
_BACKEND_DIR_RE = re.compile(r"^backends?([-_]?configs?)?$", re.I)
# CI / docs: terraform init -backend-config=FILE, plan -var-file=FILE (key=value pairs and
# CI expressions in the path are handled below)
_CLI_FLAG_RE = re.compile(r"(?<![\w-])--?(backend-config|var-file)(?:=|\s+)['\"]?([^\s'\";|&)]+)")
_CHDIR_RE = re.compile(r"(?<![\w-])--?chdir=['\"]?([^\s'\";|&)]+)")
# ${{ matrix.env }}, ${ENV}, $ENV, $(ENV), %ENV%, <env>: one path segment, per environment
_CI_EXPR_RE = re.compile(r"\$\{\{[^}]*\}\}|\$\{[^}]*\}|\$\([^)]*\)|\$[A-Za-z_]\w*|%\w+%|<[\w-]+>")
_SCRIPT_NAMES = frozenset({"Makefile", "makefile", "GNUmakefile", "Taskfile.yml", "justfile"})
_SECRET_KEY_RE = re.compile(r"(secret|password|token|access_key|sas|client_certificate)", re.I)

# The settings shown for each backend type (missing ones are reported as UNKNOWN).
BACKEND_KEYS: dict[str, tuple[str, ...]] = {
    "s3": ("bucket", "key", "region", "dynamodb_table"),
    "gcs": ("bucket", "prefix"),
    "azurerm": ("resource_group_name", "storage_account_name", "container_name", "key"),
    "remote": ("hostname", "organization"),
    "cloud": ("hostname", "organization"),
    "consul": ("address", "path"),
    "http": ("address",),
    "pg": ("conn_str", "schema_name"),
    "kubernetes": ("secret_suffix", "namespace"),
    "local": ("path",),
    "oss": ("bucket", "prefix", "key"),
    "cos": ("bucket", "prefix", "key"),
}
# settings of the backend types and the common optional ones: what a partial backend
# configuration named only after its environment (env/dev.hcl) must consist of
_BACKEND_SETTINGS = frozenset(k for keys in BACKEND_KEYS.values() for k in keys) | {
    "encrypt",
    "kms_key_id",
    "profile",
    "role_arn",
    "assume_role",
    "workspace_key_prefix",
    "use_lockfile",
    "acl",
    "subscription_id",
    "tenant_id",
    "use_azuread_auth",
    "skip_region_validation",
    "skip_credentials_validation",
    "skip_metadata_api_check",
    "dynamodb_endpoint",
}
_CALL_META = {"source", "version", "providers", "count", "for_each", "depends_on"}


def _loc(loc: Location | None) -> dict[str, Any] | None:
    return loc.to_dict() if loc else None


class _Static:
    """Evaluation scope without references (backend settings cannot use variables)."""

    location: Location | None = None

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def resolve(self, name: str, ops: Any) -> tuple[Any, Any]:
        return Unknown(f"{self.reason} ({name}.*)"), []

    def read_file(self, path: str) -> str | None:
        return None


def _static(raw: Any, reason: str = "not a literal value") -> Any:
    return evaluate(raw_to_expr(raw), _Static(reason))


def short(value: Any) -> str:
    """A value rendered for people, cut to ``MAX_VALUE`` characters."""
    if is_known(value) and not isinstance(value, str):
        try:
            text = json.dumps(to_plain(value), sort_keys=True)
        except (TypeError, ValueError):
            text = display(value)
    else:
        text = display(value)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def _setting(key: str, value: Any, loc: Location | None, note: str = "") -> dict[str, Any]:
    known = is_known(value) and not isinstance(value, (dict, list))
    out: dict[str, Any] = {"key": key, "location": _loc(loc)}
    if _SECRET_KEY_RE.search(key) and value is not None:
        out.update(value="(redacted)", known=True)
    elif known:
        out.update(value=short(value), known=True)
    else:
        reason = value.reason if isinstance(value, Unknown) else f"not a literal: {short(value)}"
        out.update(value=UNKNOWN, known=False, reason=reason)
    if note:
        out["note"] = note
    return out


# ------------------------------------------------------------------ structure


def detect_structure(repo: RepoSource, engine: Engine) -> dict[str, Any]:
    """The ``structure`` section of model.json."""
    referenced = engine.referenced_dirs()
    all_roots = sorted(set(engine.root_modules) | set(engine.units))
    trace: list[dict[str, Any]] = []
    environments = detect_environments(repo, engine, trace)
    shown = [r for r in all_roots if engine.selected(r)]
    roots = [_root(repo, engine, r, referenced, environments) for r in shown]
    return {
        "roots": roots,
        "environments": environments,
        "env_files": sorted(trace, key=lambda t: (t["stack"], t["file"], t["kind"])),
        "module_dirs": classify_module_dirs(repo, engine),
    }


def stack_env_debug(
    stack: dict[str, Any], trace: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per stack: the environment files found for it (accepted or not, with why) and,
    when none was accepted, why no environment was detected: no candidates, candidates
    ignored by an exclusion, unreadable, or rejected (each reason listed)."""
    roots = set(stack["roots"]) | {stack["id"]}
    files = [t for t in trace if t["stack"] in roots]
    out: dict[str, Any] = {"env_files": files}
    if stack["environments"]:
        return out
    below = [
        s
        for s in skipped
        if any(r == "." or in_scope(s["path"], r) for r in roots if not r.endswith("*"))
    ]
    excluded = [s["path"] for s in below if s["kind"] in ("excluded", "ignored", "symlink")]
    unreadable = [s["path"] for s in below if s["kind"] == "unreadable"]
    unreadable += [t["file"] for t in files if t["reason"].startswith("unreadable")]
    rejected = [t for t in files if not t["accepted"] and not t["reason"].startswith("unreadable")]
    where = ", ".join(sorted(roots - {stack["id"]}) or [stack["id"]])
    if unreadable:
        why = "unreadable: " + ", ".join(sorted(set(unreadable)))
    elif rejected:
        why = "candidates rejected: " + "; ".join(f"{t['file']}: {t['reason']}" for t in rejected)
    elif excluded:
        why = (
            f"no candidates at or below {where}; ignored by an exclusion (not searched): "
            + ", ".join(excluded)
        )
    else:
        why = (
            f"no candidates: no *.tfvars (other than terraform.tfvars / *.auto.tfvars), "
            f"backend-<env>.hcl / <env>.tfbackend, environment directory or CI -var-file / "
            f"-backend-config line at or below {where}"
        )
    out["no_env_reason"] = why
    return out


def _role(
    repo: RepoSource, root: str, unit: Unit | None, own: ModuleSource | None, envs: list[str]
):
    """``(deployable | library, reason)``: is this root applied, or a reusable module that
    nothing in the repository calls? Only a label: both are analyzed the same way."""
    if unit is not None:
        return "deployable", "terragrunt unit"
    if own is None:
        return "deployable", "root"
    why = []
    if any(tb.blocks.get("backend") or tb.blocks.get("cloud") for tb in own.terraform):
        why.append("backend")
    if own.providers:
        why.append("provider configuration")
    prefix = "" if root == "." else root + "/"
    if any(f.startswith(prefix) and "/" not in f[len(prefix) :] for f in repo.tfvars):
        why.append("tfvars")
    if envs:
        why.append("environments")
    if why:
        return "deployable", "has " + ", ".join(why)
    if own.variables or own.outputs:
        return (
            "library",
            "no backend, provider configuration or tfvars, and nothing in the repository "
            "calls it: looks like a reusable module",
        )
    return "deployable", "no backend configured (local state)"


def classify_module_dirs(repo: RepoSource, engine: Engine) -> list[dict[str, Any]]:
    """Every directory with Terraform code: root / child (called locally) / terragrunt
    source / remote (materialized by terraform init), who calls it and what it defines."""
    callers: dict[str, list[dict[str, Any]]] = {}
    for mod in repo.modules.values():
        for name, call in sorted(mod.module_calls.items()):
            attr = call.attrs.get("source")
            src = _static(attr.raw) if attr else None
            if isinstance(src, str) and src.startswith(("./", "../")):
                target = posixpath.normpath(posixpath.join(mod.dir, src))
                callers.setdefault(target, []).append(
                    {"caller": mod.dir, "address": f"module.{name}", "location": _loc(call.loc)}
                )
    tg_sources: dict[str, list[dict[str, Any]]] = {}
    for unit in repo.units:
        if unit.module_dir and unit.module_dir != unit.dir:
            loc = unit.source_attr.loc if unit.source_attr else unit.config.loc
            tg_sources.setdefault(unit.module_dir, []).append(
                {"caller": unit.dir, "address": "terraform.source", "location": _loc(loc)}
            )
    remote = repo.remote_dirs
    out = []
    for d in sorted(repo.modules):
        mod = repo.modules[d]
        if d in remote:
            role = "remote"
        elif d in callers:
            role = "child"
        elif d in tg_sources:
            role = "terragrunt-source"
        else:
            role = "root"
        out.append(
            {
                "dir": d,
                "role": role,
                "files": list(mod.files),
                "location": _loc(Location(mod.files[0], 1, 1)) if mod.files else None,
                "called_by": callers.get(d, []) + tg_sources.get(d, []),
                "resources": len(mod.resources),
                "data_sources": len(mod.data),
                "module_calls": len(mod.module_calls),
                "variables": len(mod.variables),
                "outputs": len(mod.outputs),
                "interface": _interface(repo, mod),
            }
        )
    return out


def _source_text(repo: RepoSource, attr: Attr | None) -> str | None:
    """An argument's expression as written (``list(string)``), first line only."""
    if attr is None or attr.loc is None:
        return None
    lines = repo.lines(attr.loc.file)
    if not 1 <= attr.loc.start_line <= len(lines):
        return None
    line = lines[attr.loc.start_line - 1]
    sep = ":" if attr.loc.file.endswith(".json") else "="
    text = line.partition(sep)[2].strip().rstrip(",").strip()
    if attr.loc.file.endswith(".json"):
        text = text.strip('"')
    if attr.loc.end_line > attr.loc.start_line:
        text += " …"
    return text or None


def _description(block: Block) -> str | None:
    attr = block.attrs.get("description")
    value = _static(attr.raw) if attr else None
    return value if isinstance(value, str) else None


def _interface(repo: RepoSource, mod: ModuleSource) -> dict[str, list[dict[str, Any]]]:
    """A module's inputs (variables) and outputs, each with where it is declared."""
    variables = []
    for name in sorted(mod.variables):
        b = mod.variables[name]
        default = b.attrs.get("default")
        value = _static(default.raw) if default is not None else None
        hcl = None
        if default is not None and is_known(value):
            hcl = json.dumps(to_plain(value), sort_keys=True)
        variables.append(
            {
                "name": name,
                "type": _source_text(repo, b.attrs.get("type")),
                "required": default is None,
                "default": short(value) if default is not None else None,
                "default_hcl": hcl if hcl is None or len(hcl) <= MAX_VALUE else None,
                "description": _description(b),
                "location": _loc(b.loc),
            }
        )
    outputs = [
        {
            "name": name,
            "value": _source_text(repo, mod.outputs[name].attrs.get("value")),
            "description": _description(mod.outputs[name]),
            "location": _loc(mod.outputs[name].loc),
        }
        for name in sorted(mod.outputs)
    ]
    return {"variables": variables, "outputs": outputs}


def _tree(engine: Engine, root: str) -> list[ModuleInstance]:
    return [i for i in engine.instances if i.root_dir == root]


def _root(
    repo: RepoSource,
    engine: Engine,
    root: str,
    referenced: set[str],
    environments: list[dict[str, Any]],
) -> dict[str, Any]:
    unit: Unit | None = engine.units.get(root)
    mod_dir = engine.root_modules.get(root)
    mod = repo.modules.get(mod_dir) if mod_dir else None
    insts = _tree(engine, root)
    dirs = list(dict.fromkeys(i.src.dir for i in insts)) or ([mod_dir] if mod_dir else [])
    own = repo.modules.get(root)
    signals: list[dict[str, Any]] = []
    if unit is not None:
        signals.append({"signal": "terragrunt.hcl", "location": _loc(unit.config.loc)})
    if own is not None:
        for tb in own.terraform:
            for b in tb.blocks.get("backend", []):
                label = b.labels[0] if b.labels else "?"
                signals.append({"signal": f'backend "{label}" block', "location": _loc(b.loc)})
            for b in tb.blocks.get("cloud", []):
                signals.append({"signal": "cloud block", "location": _loc(b.loc)})
            signals.append({"signal": "terraform block", "location": _loc(tb.loc)})
        for p in own.providers:
            name = p.labels[0] if p.labels else "?"
            signals.append({"signal": f'provider "{name}" block', "location": _loc(p.loc)})
        if root not in referenced and unit is None:
            first = Location(own.files[0], 1, 1) if own.files else None
            signals.append(
                {"signal": "not called as a module by any other directory", "location": _loc(first)}
            )
    analyzed = root in engine.roots
    status = "analyzed" if analyzed else (unit.status if unit else "outside the analyzed scope")
    resources: list[str] = []
    data: list[str] = []
    for inst in insts:
        resources += [inst.node_id(t, n) for t, n in sorted(inst.src.resources)]
        data += [inst.node_id(t, n, data=True) for t, n in sorted(inst.src.data)]
    env_names = sorted({e["name"] for e in environments if _env_covers(e, root)}, key=str.lower)
    role, role_reason = _role(repo, root, unit, own, env_names)
    return {
        "dir": root,
        "kind": "terragrunt" if unit is not None else "terraform",
        "role": role,
        "role_reason": role_reason,
        "module_dir": mod_dir,
        "analyzed": analyzed,
        "status": status,
        "signals": signals,
        "backend": _backend(repo, root, unit, mod),
        "required_version": _required_version(repo, dirs),
        "providers": _providers(repo, root, mod_dir, dirs, unit),
        "provider_configs": _provider_configs(repo, insts, dirs),
        "modules": _modules(repo, engine, root, unit, insts),
        "resources": sorted(resources),
        "data_sources": sorted(data),
        "environments": env_names,
        "tagging": tagging(insts),
    }


def _env_covers(env: dict[str, Any], root: str) -> bool:
    return root in env["roots"] or (not env["roots"] and env["root"] == ".")


# ------------------------------------------------------------------ backend


def _backend(
    repo: RepoSource, root: str, unit: Unit | None, mod: ModuleSource | None
) -> dict[str, Any]:
    if unit is not None and unit.remote_state is not None:
        return _remote_state(repo, unit)
    block, origin = None, ""
    for tb in mod.terraform if mod else []:
        for name in ("backend", "cloud"):
            if tb.blocks.get(name):
                block, origin = tb.blocks[name][0], f"terraform {name} block"
                break
        if block is not None:
            break
    if block is None:
        first = mod.files[0] if mod and mod.files else None
        return {
            "type": "local",
            "origin": "default (no backend block)",
            "location": _loc(Location(first, 1, 1)) if first else None,
            "settings": [],
            "config_files": [],
            "workspace": _workspace(repo, None, "local", mod),
            "note": "no backend configured: state is kept in a local terraform.tfstate",
        }
    btype = "cloud" if block.type == "cloud" else (block.labels[0] if block.labels else UNKNOWN)
    reason = "backend settings cannot use variables"
    values = {k: (_static(a.raw, reason), a.loc) for k, a in block.attrs.items()}
    for name, nested in block.blocks.items():  # workspaces { name = ... }
        if nested:
            values[name] = (
                {k: _static(a.raw, reason) for k, a in nested[0].attrs.items()},
                nested[0].loc,
            )
    config_files = _backend_files(repo, root)
    return {
        "type": btype,
        "origin": origin,
        "location": _loc(block.loc),
        "settings": _settings(btype, values, config_files),
        "config_files": config_files,
        "workspace": _workspace(repo, values, btype, mod),
    }


def _settings(
    btype: str,
    values: dict[str, tuple[Any, Location | None]],
    config_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wanted = BACKEND_KEYS.get(btype, ())
    out = []
    for key in list(wanted) + sorted(k for k in values if k not in wanted):
        if key == "workspaces":
            continue
        if key in values:
            value, loc = values[key]
            out.append(_setting(key, value, loc))
            continue
        found = [s for f in config_files for s in f["settings"] if s["key"] == key]
        if len({s["value"] for s in found}) > 1:
            # one partial configuration per environment (backend.<env>.hcl)
            per_env = {
                backend_env(s["location"]["file"]) or s["location"]["file"]: s["value"]
                for s in found
            }
            out.append(
                {
                    "key": key,
                    "value": "per environment: "
                    + ", ".join(f"{k} = {v}" for k, v in per_env.items()),
                    "known": True,
                    "location": found[0]["location"],
                    "per_environment": per_env,
                    "note": "from " + ", ".join(s["location"]["file"] for s in found),
                }
            )
        elif found:
            out.append({**found[0], "note": f"from {found[0]['location']['file']}"})
        else:
            out.append(
                {
                    "key": key,
                    "value": UNKNOWN,
                    "known": False,
                    "location": None,
                    "reason": "not set in the configuration: supplied by "
                    "terraform init -backend-config (or the environment) at run time",
                }
            )
    return out


def _backend_files(repo: RepoSource, root: str) -> list[dict[str, Any]]:
    """Partial backend configurations (``*.tfbackend``, ``backend*.hcl``) of a root."""
    out = []
    for f in backend_files(repo):
        d = posixpath.dirname(f) or "."
        if not (root == "." or in_scope(d, root)):
            continue
        attrs = read_attrs(repo, f) or {}
        if f not in repo.files:
            repo.files.append(f)
        out.append(
            {
                "file": f,
                "settings": [_setting(k, _static(a.raw), a.loc) for k, a in sorted(attrs.items())],
            }
        )
    return out


def _remote_state(repo: RepoSource, unit: Unit) -> dict[str, Any]:
    block = unit.remote_state
    assert block is not None
    ctx = unit_context(repo, unit)
    reason = "resolved by terragrunt at run time"
    btype_attr = block.attrs.get("backend")
    btype_value = _static(substitute_raw(btype_attr.raw, ctx), reason) if btype_attr else None
    btype = btype_value if isinstance(btype_value, str) else UNKNOWN
    values: dict[str, tuple[Any, Location | None]] = {}
    config = block.attrs.get("config")
    if config is not None and isinstance(config.raw, dict):
        keys = map_keys(repo, config)
        for k, raw in config.raw.items():
            name = str(k).strip('"')
            if name.startswith("__"):
                continue
            values[name] = (_static(substitute_raw(raw, ctx), reason), keys.get(name, config.loc))
    origin = "terragrunt remote_state"
    if unit.remote_state_config is not None and unit.remote_state_config.file != unit.file:
        origin += f" (included from {unit.remote_state_config.file})"
    return {
        "type": btype,
        "origin": origin,
        "location": _loc(block.loc),
        "settings": _settings(btype, values, []),
        "config_files": [],
        "workspace": {
            "value": "default",
            "note": "terragrunt: one state per unit (key per directory)",
            "references": [],
        },
    }


def _workspace(
    repo: RepoSource,
    values: dict[str, tuple[Any, Location | None]] | None,
    btype: str,
    mod: ModuleSource | None,
) -> dict[str, Any]:
    refs = _workspace_refs(repo, mod)
    ws = (values or {}).get("workspaces")
    if ws is not None and isinstance(ws[0], dict):
        cfg, loc = ws
        for key, label in (("name", "workspace {}"), ("prefix", "workspaces with prefix {}*")):
            if key in cfg:
                return {
                    "value": label.format(short(cfg[key])),
                    "location": _loc(loc),
                    "references": refs,
                }
        if "tags" in cfg:
            return {"value": f"workspaces tagged {short(cfg['tags'])}", "location": _loc(loc)}
    note = "only the default workspace is configured"
    if btype == "s3" and values and "workspace_key_prefix" in values:
        note = f"non-default workspaces under {short(values['workspace_key_prefix'][0])}/"
    if refs:
        note += "; terraform.workspace is referenced, so the state may differ per workspace"
    return {"value": "default", "note": note, "references": refs}


def _workspace_refs(repo: RepoSource, mod: ModuleSource | None) -> list[dict[str, Any]]:
    out = []
    for f in mod.files if mod else []:
        for n, line in enumerate(repo.lines(f), 1):
            if "terraform.workspace" in line:
                out.append(Location(f, n, n).to_dict())
                if len(out) >= MAX_WORKSPACE_REFS:
                    return out
    return out


# ------------------------------------------------------------------ providers


def _required_providers(tb: Block) -> list[tuple[str, Any, Location]]:
    out = []
    for b in tb.blocks.get("required_providers", []):
        out += [(name, attr.raw, attr.loc) for name, attr in b.attrs.items()]
    attr = tb.attrs.get("required_providers")  # Terraform JSON syntax: a map argument
    if attr is not None and isinstance(attr.raw, dict):
        out += [(str(k), v, attr.loc) for k, v in attr.raw.items() if not str(k).startswith("__")]
    return out


def _required_version(repo: RepoSource, dirs: list[str]) -> list[dict[str, Any]]:
    out = []
    for d in dirs:
        for tb in repo.modules[d].terraform if d in repo.modules else []:
            attr = tb.attrs.get("required_version")
            if attr is not None:
                value = _static(attr.raw)
                out.append({"module": d, "value": short(value), "location": _loc(attr.loc)})
    return out


def _full_source(source: str) -> str:
    return f"registry.terraform.io/{source}" if source.count("/") == 1 else source


def _providers(
    repo: RepoSource, root: str, mod_dir: str | None, dirs: list[str], unit: Unit | None
) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    locks = {**repo.locks.get(mod_dir or root, {}), **repo.locks.get(root, {})}
    for d in dirs:
        for tb in repo.modules[d].terraform if d in repo.modules else []:
            for name, raw, loc in _required_providers(tb):
                value = to_plain(_static(raw)) if is_known(_static(raw)) else {}
                if isinstance(value, str):  # legacy: aws = "~> 3.0"
                    value = {"version": value}
                if not isinstance(value, dict):
                    value = {}
                entry = by_name.setdefault(
                    name,
                    {"name": name, "source": None, "constraints": [], "locked": None},
                )
                if value.get("source") and not entry["source"]:
                    entry["source"] = str(value["source"])
                entry["constraints"].append(
                    {
                        "module": d,
                        "version": str(value["version"]) if value.get("version") else None,
                        "location": _loc(loc),
                    }
                )
    for d in dirs:  # providers configured but never declared in required_providers
        for p in repo.modules[d].providers if d in repo.modules else []:
            name = p.labels[0] if p.labels else "?"
            by_name.setdefault(
                name, {"name": name, "source": None, "constraints": [], "locked": None}
            )
    for entry in by_name.values():
        source = entry["source"] or f"hashicorp/{entry['name']}"
        locked = locks.get(_full_source(source))
        if locked:
            entry["locked"] = {"version": locked[0], "location": _loc(locked[1])}
    out = [by_name[k] for k in sorted(by_name)]
    if unit is not None:
        for c in [*unit.parents, unit.config]:
            for g in c.generate:
                label = g.labels[0] if g.labels else ""
                contents = g.attrs.get("contents")
                text = str(contents.raw) if contents else ""
                if "provider" in label or "provider" in text:
                    out.append(
                        {
                            "name": f"(generated: {label})",
                            "source": None,
                            "constraints": [],
                            "locked": None,
                            "note": f'provider configuration generated by terragrunt (generate "{label}")',
                            "location": _loc(g.loc),
                        }
                    )
    return out


def _provider_configs(
    repo: RepoSource, insts: list[ModuleInstance], dirs: list[str]
) -> list[dict[str, Any]]:
    by_dir = {i.src.dir: i for i in insts}
    out = []
    for d in dirs:
        for p in repo.modules[d].providers if d in repo.modules else []:
            inst = by_dir.get(d)
            entry: dict[str, Any] = {
                "name": p.labels[0] if p.labels else "?",
                "module": d,
                "alias": None,
                "region": None,
                "location": _loc(p.loc),
            }
            for key in ("alias", "region"):
                attr = p.attrs.get(key)
                if attr is None:
                    continue
                value = inst.eval_attr(attr) if inst else _static(attr.raw)
                entry[key] = short(value) if is_known(value) else UNKNOWN
                if not is_known(value):
                    entry[f"{key}_reason"] = short(value)
            out.append(entry)
    return out


# ------------------------------------------------------------------ modules


def _modules(
    repo: RepoSource, engine: Engine, root: str, unit: Unit | None, insts: list[ModuleInstance]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if unit is not None and unit.source_attr is not None:
        info = unit.source_info or {"kind": "unknown", "ref": None}
        inputs = []
        for name in sorted(unit.inputs):
            tv = engine.root_var(root, name)
            value = tv[0] if tv else None
            inputs.append(
                {"name": name, "value": short(value), "location": _loc(unit.inputs[name].loc)}
            )
        out.append(
            {
                "address": "terraform.source",
                "name": posixpath.basename(root) if root != "." else "(top)",
                "kind": info["kind"],
                "source": unit.source,
                "version": info.get("ref"),
                "location": _loc(unit.source_attr.loc),
                "dir": unit.module_dir,
                "analyzed": root in engine.roots,
                "status": "analyzed" if root in engine.roots else unit.status,
                "inputs": inputs,
            }
        )
    for inst in insts:
        for name in sorted(inst.src.module_calls):
            call = inst.src.module_calls[name]
            source = inst.child_source.get(name)
            info = (
                classify_source(source)
                if source
                else {"kind": "unknown", "source": None, "ref": None}
            )
            version_attr = call.attrs.get("version")
            version = None
            if version_attr is not None:
                v = inst.eval_attr(version_attr)
                version = short(v) if is_known(v) else UNKNOWN
            child = inst.children.get(name)
            out.append(
                {
                    "address": f"{inst.address}.module.{name}"
                    if inst.address
                    else f"module.{name}",
                    "name": name,
                    "kind": info["kind"],
                    "source": source,
                    "version": version or info["ref"],
                    "location": _loc(call.loc),
                    "dir": child.src.dir if child is not None else None,
                    "analyzed": child is not None,
                    "status": inst.child_status.get(name, "analyzed"),
                    "inputs": [
                        {
                            "name": k,
                            "value": short(inst.eval_attr(call.attrs[k])),
                            "location": _loc(call.attrs[k].loc),
                        }
                        for k in sorted(call.attrs)
                        if k not in _CALL_META
                    ],
                }
            )
    return out


# ------------------------------------------------------------------ environments


def env_name(name: str) -> str:
    """A name that passes ``tfrepo.validate_env_name``."""
    name = _ENV_NAME_BAD.sub("-", name).strip("-._")[:63]
    return name or "env"


def _is_env_dir(name: str, parent: str | None) -> bool:
    low = name.lower()
    if low in ENV_WORDS:
        return True
    if parent is None or parent.lower() not in ENV_CONTAINERS:
        return False
    return not (low in NOT_ENVS or name.startswith(("_", ".")) or _REGION_RE.match(low))


def env_segment(path: str) -> tuple[str, str] | None:
    """``(env name, env directory)`` of a path in an environment layout, else None."""
    if path in ("", "."):
        return None
    parts = path.split("/")
    for i, part in enumerate(parts):
        if _is_env_dir(part, parts[i - 1] if i else None):
            return part, "/".join(parts[: i + 1])
    return None


def _owner(path_dir: str, roots: list[str]) -> str:
    """The deepest root containing a directory ('.' if none does)."""
    best = "."
    for r in roots:
        if r != "." and in_scope(path_dir, r) and len(r) > len(best):
            best = r
    return best


def detect_environments(
    repo: RepoSource, engine: Engine, trace: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Environments of the repository, sorted by (directory, name).

    Each is ``{name, root, kinds, var_files, backend_hcl, var_file_cli, evidence, roots}``:
    its tfvars files, its partial backend configurations (``backend.<env>.hcl``,
    ``<env>.tfbackend``, ``backend/<env>.hcl``) and the ``-var-file`` / ``-backend-config``
    options naming them in CI files and docs. Backend files and CLI usages are attached to
    the environment found from the layout or tfvars; they only add a kind (``backend`` /
    ``ci``) when they are what reveals the environment.

    Detection is per stack: a tfvars file or a partial backend configuration at any depth
    below a root belongs to that root (the deepest one) and its stem names the
    environment. ``trace`` (when given) receives one entry per candidate file:
    ``{file, stack, env, kind, accepted, reason}``, the rejected ones with why."""
    if not engine.root_modules and not engine.units:
        engine.discover_roots()
    roots = sorted(set(engine.root_modules) | set(engine.units))
    envs: dict[tuple[str, str], dict[str, Any]] = {}

    def note(
        file: str, owner: str, name: str | None, kind: str, accepted: bool, reason: str
    ) -> None:
        if trace is not None:
            trace.append(
                {
                    "file": file,
                    "stack": owner,
                    "env": env_name(name) if name else None,
                    "kind": kind,
                    "accepted": accepted,
                    "reason": reason,
                }
            )

    def add(scope: str, name: str, kind: str, loc: Location | None, root: str | None) -> None:
        key = (scope, env_name(name))
        e = envs.setdefault(
            key,
            {
                "name": key[1],
                "root": scope,
                "kinds": [],
                "var_files": [],
                "backend_hcl": [],
                "var_file_cli": [],
                "evidence": [],
                "roots": [],
            },
        )
        if kind not in e["kinds"]:
            e["kinds"].append(kind)
        if loc is not None and loc.to_dict() not in e["evidence"]:
            e["evidence"].append(loc.to_dict())
        if root is not None and root not in e["roots"]:
            e["roots"].append(root)

    # 1. directory layouts: envs/dev, live/prod/..., env-named root directories
    for r in roots:
        seg = env_segment(r)
        if seg is None:
            continue
        unit = engine.units.get(r)
        mod = repo.modules.get(r)
        if unit is not None:
            loc = unit.config.loc
        else:
            loc = Location(mod.files[0], 1, 1) if mod and mod.files else None
        add(seg[1], seg[0], "terragrunt" if unit else "dir", loc, r)
        note(
            loc.file if loc else r,
            r,
            seg[0],
            "terragrunt" if unit else "dir",
            True,
            f"root inside the environment directory {seg[1]}/",
        )
    # Terragrunt env.hcl: locals { environment = "dev" } names the environment
    for r in sorted(engine.units):
        found = _env_hcl(repo, r)
        if found is None:
            continue
        name, scope, loc = found
        for key in [k for k in envs if k[0] == scope and k[1] != env_name(name)]:
            old = envs.pop(key)
            for root in old["roots"]:
                add(scope, name, "terragrunt", None, root)
        add(scope, name, "terragrunt", loc, r)
    # 2. tfvars files
    commons: dict[str, list[str]] = {}
    candidates: list[tuple[str, str, str, Location]] = []  # owner, name, file, loc
    siblings: dict[str, int] = {}
    for err in repo.errors:
        if err.file.endswith((".tfvars", ".tfvars.json")):
            d = posixpath.dirname(err.file) or "."
            note(err.file, _owner(d, roots), None, "tfvars", False, f"unreadable: {err.message}")
    for f in sorted(repo.tfvars):
        base = posixpath.basename(f)
        stem = base.removesuffix(".json").removesuffix(".tfvars")
        d = posixpath.dirname(f) or "."
        owner = _owner(d, roots)
        if ".auto." in base or (stem == "terraform" and d == owner):
            note(
                f,
                owner,
                None,
                "tfvars",
                False,
                "loaded automatically by terraform on every run (terraform.tfvars / "
                "*.auto.tfvars): not an environment",
            )
            continue
        if _BACKEND_FILE_RE.search(f):
            continue  # a partial backend configuration (-backend-config): listed below
        attrs = repo.tfvars[f]
        first = min((a.loc for a in attrs.values()), key=lambda x: x.start_line, default=None)
        loc = first or Location(f, 1, 1)
        if stem.lower() in COMMON_STEMS:
            commons.setdefault(owner, []).append(f)
            note(
                f,
                owner,
                None,
                "tfvars",
                True,
                f"shared values ({stem}): applied to every environment of {owner}",
            )
            continue
        if stem.lower() in GENERIC_STEMS:
            seg = env_segment(d)
            if seg is None:
                note(
                    f,
                    owner,
                    None,
                    "tfvars",
                    False,
                    f"generic name ({stem}) outside an environment directory: no environment name",
                )
                continue
            stem = seg[0]
        candidates.append((owner, stem, f, loc))
        siblings[d] = siblings.get(d, 0) + 1
    for owner, stem, f, loc in candidates:
        d = posixpath.dirname(f) or "."
        parent = posixpath.basename(d)
        seg = env_segment(d)
        if stem.lower() in ENV_WORDS:
            why = f"named after an environment ({stem})"
        elif seg is not None:
            why = f"inside the environment directory {seg[1]}/"
        elif parent.lower() in ENV_CONTAINERS | VAR_DIRS:
            why = f"in a {parent}/ directory"
        elif siblings[d] >= 2:
            why = f"one of {siblings[d]} tfvars files in {d}/"
        elif owner in roots:
            why = "tfvars file next to or below the root"
        else:
            note(
                f,
                owner,
                stem,
                "tfvars",
                False,
                "not below any root and not named like an environment",
            )
            continue
        where = "next to" if d == owner else "below"
        note(f, owner, stem, "tfvars", True, f"{why}; {where} the root {owner}: env = file stem")
        add(owner, stem, "tfvars", loc, owner if owner in roots else None)
        e = envs[(owner, env_name(stem))]
        for c in commons.get(owner, []):
            if c not in e["var_files"]:
                e["var_files"].append(c)
        if f not in e["var_files"]:
            e["var_files"].append(f)

    def attach(owner: str, name: str, kind: str, loc: Location) -> dict[str, Any]:
        """The environment ``name`` of ``owner`` (created, with ``kind``, if new)."""
        key = (owner, env_name(name))
        if key not in envs:
            covering = [k for k in envs if k[1] == key[1] and in_scope(owner, k[0])]
            if covering:  # envs/dev/backend.hcl next to the envs/dev root
                return envs[max(covering, key=lambda k: len(k[0]))]
            add(owner, name, kind, loc, owner if owner in roots else None)
        return envs[key]

    # 3. partial backend configurations: backend.<env>.hcl, <env>.tfbackend, backend/<env>.hcl,
    # env/<env>.hcl: matched to the environment of the same name
    configs = backend_files(repo)
    per_dir: dict[str, int] = {}
    for f in configs:
        d = posixpath.dirname(f) or "."
        per_dir[d] = per_dir.get(d, 0) + 1
    for f in configs:
        d = posixpath.dirname(f) or "."
        name = backend_env(f)
        owner = _owner(d, roots)
        if d != owner:
            owner = _owner(_backend_owner_dir(d), roots)
        if name is None:
            note(
                f,
                owner,
                None,
                "backend",
                False,
                "partial backend configuration with no environment in its name or directory",
            )
            continue
        known = any(k[1] == env_name(name) for k in envs)
        if name.lower() in ENV_WORDS:
            why = f"named after an environment ({name})"
        elif known:
            why = f"matches the environment {name} found from tfvars / directories"
        elif per_dir[d] >= 2:
            why = f"one of {per_dir[d]} backend configurations in {d}/"
        elif env_segment(d) is not None:
            why = "inside an environment directory"
        elif owner in roots:
            why = "backend configuration next to or below the root"
        else:
            note(
                f,
                owner,
                name,
                "backend",
                False,
                "not below any root and not named like an environment",
            )
            continue
        note(f, owner, name, "backend", True, f"partial backend configuration: {why}")
        e = attach(owner, name, "backend", Location(f, 1, 1))
        if f not in e["backend_hcl"]:
            e["backend_hcl"].append(f)
        if f not in repo.files:
            repo.files.append(f)  # listed, so its source can be shown
    # 4. -backend-config / -var-file in CI files, scripts and docs
    for use in cli_env_files(repo, roots):
        if use["env"] is None:
            # a literal path: the environment that already lists the file
            e = next(
                (x for x in envs.values() if use["file"] in x["var_files"] + x["backend_hcl"]),
                None,
            )
            stem = posixpath.basename(use["file"]).split(".")[0]
            if e is None and stem.lower() in ENV_WORDS:
                e = attach(use["owner"], stem, "ci", use["loc"])
        else:
            e = attach(use["owner"], use["env"], "ci", use["loc"])
        if e is None:
            continue
        src = use["loc"]
        note(
            use["file"],
            e["roots"][0] if e["roots"] else e["root"],
            e["name"],
            "ci",
            True,
            f"{use['flag']} {use['value']} in {src.file}:{src.start_line}",
        )
        files = e["var_files"] if use["flag"] == "-var-file" else e["backend_hcl"]
        if use["file"] not in files:
            files.append(use["file"])
        entry = {
            "flag": use["flag"],
            "value": use["value"],
            "file": use["file"],
            "location": use["loc"].to_dict(),
        }
        if entry not in e["var_file_cli"]:
            e["var_file_cli"].append(entry)
    out = [envs[k] for k in sorted(envs)]
    for e in out:
        e["kinds"].sort()
        e["roots"].sort()
        e["backend_hcl"].sort()
        e["var_file_cli"].sort(key=lambda u: (u["location"]["file"], u["location"]["start_line"]))
        e.update(_wiring(e))
        e["state"] = _env_state(repo, engine, e)
    return out


def _known_settings(settings: list[dict[str, Any]]) -> dict[str, str]:
    return {
        s["key"]: s["value"]
        for s in settings
        if s.get("known") and "per_environment" not in s and s.get("value") != "(redacted)"
    }


def state_index(
    repo: RepoSource, engine: Engine, environments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The state each stack writes, per environment: ``[{root, env, type, settings}]``
    (the known backend settings: the backend block merged with the environment's partial
    configuration). ``terraform_remote_state`` data sources are matched against it."""
    out: list[dict[str, Any]] = []
    covered: set[str] = set()
    for e in environments:
        state = e.get("state") or {}
        settings = _known_settings(state.get("settings", []))
        for r in e["roots"] or [e["root"]]:
            out.append(
                {"root": r, "env": e["name"], "type": state.get("type"), "settings": settings}
            )
            covered.add(r)
    for r in sorted(set(engine.root_modules) | set(engine.units)):
        if r in covered:
            continue
        mod = repo.modules.get(engine.root_modules.get(r, r))
        b = _backend(repo, r, engine.units.get(r), mod)
        out.append(
            {
                "root": r,
                "env": None,
                "type": b["type"],
                "settings": _known_settings(b["settings"]),
            }
        )
    return out


def _wiring(env: dict[str, Any]) -> dict[str, Any]:
    """Is the environment deployed from this repository? It is *wired* when a backend
    configuration or a CI / script line is used for it, or when it is its own root
    directory; an environment with only tfvars is listed as not wired (and analyzed)."""
    how = []
    if env["backend_hcl"]:
        how.append("backend config: " + ", ".join(env["backend_hcl"]))
    ci = sorted({u["location"]["file"] for u in env["var_file_cli"]})
    if ci:
        how.append("CI / scripts: " + ", ".join(ci))
    if {"dir", "terragrunt"} & set(env["kinds"]):
        how.append("its own root directory")
    out: dict[str, Any] = {"wired": bool(how), "wiring": how}
    if not how:
        out["not_wired_reason"] = (
            "tfvars only: no backend configuration and no CI / script line uses it"
        )
    return out


def _env_state(repo: RepoSource, engine: Engine, env: dict[str, Any]) -> dict[str, Any]:
    """The state file of an environment: the backend type and its key settings (``bucket``
    / ``key`` / ``region`` / ``dynamodb_table`` for s3) from the root's backend block and
    the environment's partial configurations (later files override earlier ones, as with
    repeated ``-backend-config``); a setting found nowhere is UNKNOWN with the reason."""
    root = env["roots"][0] if env["roots"] else env["root"]
    unit = engine.units.get(root)
    if unit is not None and unit.remote_state is not None:
        rs = _remote_state(repo, unit)
        return {"type": rs["type"], "origin": rs["origin"], "files": [], "settings": rs["settings"]}
    mod = repo.modules.get(engine.root_modules.get(root, root))
    block = None
    for tb in mod.terraform if mod else []:
        found = tb.blocks.get("backend") or tb.blocks.get("cloud")
        if found:
            block = found[0]
            break
    files = list(env["backend_hcl"])
    if block is None and not files:
        return {
            "type": "local",
            "origin": "default (no backend block)",
            "files": [],
            "settings": [],
            "note": "no backend configured: state is kept in a local terraform.tfstate",
        }
    btype = UNKNOWN
    values: dict[str, tuple[Any, Location | None]] = {}
    origin = "partial backend configuration"
    if block is not None:
        btype = "cloud" if block.type == "cloud" else (block.labels[0] if block.labels else UNKNOWN)
        origin = f"{block.type} block" + (f" + {', '.join(files)}" if files else "")
        reason = "backend settings cannot use variables"
        values = {k: (_static(a.raw, reason), a.loc) for k, a in block.attrs.items()}
    for f in files:
        for k, a in (read_attrs(repo, f) or {}).items():
            values[k] = (_static(a.raw, "backend settings cannot use variables"), a.loc)
    wanted = BACKEND_KEYS.get(btype, ())
    settings = []
    for key in list(wanted) + sorted(k for k in values if k not in wanted):
        if key in values:
            settings.append(_setting(key, *values[key]))
        else:
            if files:
                reason = f"not set in {', '.join(files)} nor in the backend block"
            else:
                reason = "no backend configuration for this environment, not set in the block"
            settings.append(
                {
                    "key": key,
                    "value": UNKNOWN,
                    "known": False,
                    "location": None,
                    "reason": reason + ": supplied by terraform init -backend-config (or the "
                    "environment) at run time",
                }
            )
    return {
        "type": btype,
        "origin": origin,
        "location": _loc(block.loc) if block is not None else None,
        "files": files,
        "settings": settings,
    }


def backend_files(repo: RepoSource) -> list[str]:
    """The repository's partial backend configurations (``terraform init
    -backend-config=FILE``): files named so (:data:`_BACKEND_FILE_RE`), and any other
    ``*.hcl`` file, wherever it is, whose content is backend settings only (``bucket``,
    ``key``, ``region``, ...): classified by content, not by name or location."""
    configs = {u.file for u in repo.units} | {p.file for u in repo.units for p in u.parents}
    configs |= set(repo.terragrunt)
    out = []
    for f in repo.all_files:
        if _BACKEND_FILE_RE.search(f):
            out.append(f)
            continue
        base = posixpath.basename(f)
        if not base.endswith(".hcl") or base.startswith(".") or f in configs:
            continue
        keys = set(read_attrs(repo, f) or {})
        if keys and keys <= _BACKEND_SETTINGS and keys & {"bucket", "key", "prefix", "path"}:
            out.append(f)
    return out


def _backend_owner_dir(d: str) -> str:
    """The directory a partial backend configuration belongs to: backend/dev.hcl and
    backend-config/dev.hcl belong to the directory above."""
    return (posixpath.dirname(d) or ".") if _BACKEND_DIR_RE.match(posixpath.basename(d)) else d


def backend_env(path: str) -> str | None:
    """The environment a partial backend configuration is for: ``backend.dev.hcl``,
    ``dev.tfbackend``, ``backend/dev.hcl``, ``envs/dev/backend.hcl`` -> ``dev``."""
    base = posixpath.basename(path)
    stem = base
    for ext in (".tfbackend", ".hcl", ".tfvars", ".conf"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    rest = _BACKEND_WORD_RE.sub(".", stem).strip("._-")
    if rest:
        return rest
    seg = env_segment(posixpath.dirname(path))
    return seg[0] if seg else None


def _cli_sources(repo: RepoSource) -> list[str]:
    """Files whose commands may pass -var-file / -backend-config: CI, scripts, docs."""
    from .crawl import ci_system, doc_files

    docs = set(doc_files(repo.all_files))
    out = []
    for f in repo.all_files:
        base = posixpath.basename(f)
        if (
            ci_system(f)
            or f in docs
            or base in _SCRIPT_NAMES
            or base.endswith((".sh", ".mk", ".bash", ".ps1"))
        ):
            out.append(f)
    return out


def cli_env_files(repo: RepoSource, roots: list[str]) -> list[dict[str, Any]]:
    """``-var-file`` / ``-backend-config`` options that name a repository file, with the
    environment a CI expression in the path stands for (``backend.${{ matrix.env }}.hcl``
    matched against ``backend.dev.hcl`` -> ``dev``; None for a literal path)."""
    pool = set(repo.all_files)
    out = []
    for src in _cli_sources(repo):
        text = "\n".join(repo.lines(src))
        for n, line in enumerate(repo.lines(src), 1):
            if "-var-file" not in line and "-backend-config" not in line:
                continue
            norm = _CI_EXPR_RE.sub("\0", line)
            chdir = _CHDIR_RE.search(norm)
            for m in _CLI_FLAG_RE.finditer(norm):
                flag, value = f"-{m.group(1)}", m.group(2)
                if "=" in value:
                    continue  # -backend-config="key=value": a setting, not a file
                bases = [chdir.group(1)] if chdir else []
                bases += [""] + [r for r in roots if r != "."]
                matches = _match_cli_path(value, bases, pool)
                names = {env for _, env in matches if env is not None}
                if len(names) == 1 and next(iter(names)).lower() not in ENV_WORDS:
                    continue  # one match that does not look like an environment
                # env: [dev, uat, prod] in the same file: the expression stands for those
                # only, not for every file it matches (env/intg.tfvars is not deployed)
                listed = {e for e in names if _mentions(text, e)}
                if listed:
                    matches = [(f, e) for f, e in matches if e is None or e in listed]
                for f, env in matches:
                    d = posixpath.dirname(f) or "."
                    if flag == "-backend-config":
                        d = _backend_owner_dir(d)
                    out.append(
                        {
                            "flag": flag,
                            "value": value.replace("\0", "*"),
                            "file": f,
                            "env": env,
                            "owner": _owner(d, roots),
                            "loc": Location(src, n, n),
                        }
                    )
    return out


def _mentions(text: str, name: str) -> bool:
    """Whether ``name`` appears in ``text`` as a word (``[dev, uat]``, ``ENV=dev``)."""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text) is not None


def _match_cli_path(value: str, bases: list[str], pool: set[str]) -> list[tuple[str, str | None]]:
    """Repository files a CLI path names (relative to one of ``bases``), with the value of
    the first CI expression in it (one path segment) when there is one."""
    found: dict[str, str | None] = {}
    for base in bases:
        if "\0" in base:
            continue
        joined = posixpath.normpath(posixpath.join(base, value)) if base else value
        joined = joined.removeprefix("./")
        if "\0" not in joined:
            if joined in pool:
                found.setdefault(joined, None)
            continue
        parts = [re.escape(p) for p in joined.split("\0")]
        rx = re.compile("([^/]+)".join(parts))
        for f in sorted(pool):
            m = rx.fullmatch(f)
            if m and len(set(m.groups())) == 1:
                found.setdefault(f, m.group(1))
    return sorted(found.items())


def _env_hcl(repo: RepoSource, unit_dir: str) -> tuple[str, str, Location] | None:
    """``locals { environment | env | stage = "..." }`` of the nearest env.hcl."""
    d = unit_dir
    while True:
        rel = f"{d}/env.hcl" if d != "." else "env.hcl"
        if rel in repo.all_files:
            for block in read_blocks(repo, rel, "locals"):
                for key in ("environment", "env", "stage"):
                    attr: Attr | None = block.attrs.get(key)
                    value = _static(attr.raw) if attr else None
                    if isinstance(value, str) and value:
                        if rel not in repo.files:
                            repo.files.append(rel)
                        return value, d, attr.loc if attr else Location(rel, 1, 1)
        if d == ".":
            return None
        d = posixpath.dirname(d) or "."
