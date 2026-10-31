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
from .loader import Attr, Block, ModuleSource, RepoSource, map_keys, read_attrs, read_blocks
from .sources import classify_source
from .terragrunt import Unit, substitute_raw, unit_context
from .values import Location, Unknown, display, is_known, to_plain

UNKNOWN = "UNKNOWN"
MAX_VALUE = 160
MAX_WORKSPACE_REFS = 5

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
        "integration",
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
# Directories whose sub-directories are environments (envs/<env>, live/<env>, ...).
ENV_CONTAINERS = frozenset(
    {"envs", "env", "environments", "environment", "live", "deployments", "accounts", "stages"}
)
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
_BACKEND_FILE_RE = re.compile(r"(\.tfbackend|(^|/)backend[^/]*\.(hcl|tfvars|conf))$")
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
    environments = detect_environments(repo, engine)
    shown = [r for r in all_roots if engine.scope is None or in_scope(r, engine.scope)]
    roots = [_root(repo, engine, r, referenced, environments) for r in shown]
    return {"roots": roots, "environments": environments}


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
    return {
        "dir": root,
        "kind": "terragrunt" if unit is not None else "terraform",
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
        "environments": sorted(
            {e["name"] for e in environments if _env_covers(e, root)}, key=str.lower
        ),
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
        if found:
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
    for f in repo.all_files:
        d = posixpath.dirname(f) or "."
        if not _BACKEND_FILE_RE.search(f) or not (root == "." or in_scope(d, root)):
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


def detect_environments(repo: RepoSource, engine: Engine) -> list[dict[str, Any]]:
    """Environments of the repository, sorted by (directory, name)."""
    if not engine.root_modules and not engine.units:
        engine.discover_roots()
    roots = sorted(set(engine.root_modules) | set(engine.units))
    envs: dict[tuple[str, str], dict[str, Any]] = {}

    def add(scope: str, name: str, kind: str, loc: Location | None, root: str | None) -> None:
        key = (scope, env_name(name))
        e = envs.setdefault(
            key,
            {
                "name": key[1],
                "root": scope,
                "kinds": [],
                "var_files": [],
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
    for f in sorted(repo.tfvars):
        base = posixpath.basename(f)
        stem = base.removesuffix(".json").removesuffix(".tfvars")
        d = posixpath.dirname(f) or "."
        owner = _owner(d, roots)
        if ".auto." in base or (stem == "terraform" and d == owner):
            continue  # loaded automatically by terraform: not an environment
        attrs = repo.tfvars[f]
        first = min((a.loc for a in attrs.values()), key=lambda x: x.start_line, default=None)
        loc = first or Location(f, 1, 1)
        if stem.lower() in COMMON_STEMS:
            commons.setdefault(owner, []).append(f)
            continue
        if stem.lower() in GENERIC_STEMS:
            seg = env_segment(d)
            if seg is None:
                continue
            stem = seg[0]
        candidates.append((owner, stem, f, loc))
        siblings[d] = siblings.get(d, 0) + 1
    for owner, stem, f, loc in candidates:
        d = posixpath.dirname(f) or "."
        parent = posixpath.basename(d)
        plausible = (
            stem.lower() in ENV_WORDS
            or parent.lower() in ENV_CONTAINERS | VAR_DIRS
            or env_segment(d) is not None
            or siblings[d] >= 2
        )
        if not plausible:
            continue
        add(owner, stem, "tfvars", loc, owner if owner in roots else None)
        e = envs[(owner, env_name(stem))]
        for c in commons.get(owner, []):
            if c not in e["var_files"]:
                e["var_files"].append(c)
        if f not in e["var_files"]:
            e["var_files"].append(f)
    out = [envs[k] for k in sorted(envs)]
    for e in out:
        e["kinds"].sort()
        e["roots"].sort()
    return out


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
