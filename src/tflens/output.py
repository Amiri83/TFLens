"""Serialize the analysis to model.json (deterministic: sorted, no timestamps, relative paths)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .analyze import analyze
from .crawl import crawl
from .detect import detect_environments, detect_structure, stack_env_debug, state_index
from .docsdiff import docs_diff
from .engine import Engine, ModuleInstance, in_scope
from .envs import (
    analyze_environments,
    default_env,
    env_id,
    env_roots,
    find_env,
    select_stacks,
    stacks,
)
from .loader import RepoSource, load_repo
from .model import SCHEMA_VERSION, Graph
from .values import to_json

_CALL_META = {"source", "version", "providers", "count", "for_each", "depends_on"}


@dataclass
class Analysis:
    repo: RepoSource
    engine: Engine
    graph: Graph
    model: dict[str, Any]

    @property
    def failure(self) -> str:
        """Why the run failed ('' if it did not)."""
        return self.model["discovery"]["reason"] if self.failed else ""

    @property
    def failed(self) -> bool:
        return self.model["discovery"]["status"] == "failed"


def run_analysis(
    root: Path,
    var_files: list[str] | None = None,
    cli_vars: dict[str, str] | None = None,
    scope: str | None = None,
    git_root: Path | None = None,
    git: bool = True,
    env: str | None = None,
    auto_env: bool = False,
    stack_envs: dict[str, str] | None = None,
) -> Analysis:
    """Analyze ``root``. ``scope`` limits the roots to one directory (an environment).
    The git history is read when ``git_root`` (default ``root``) is a git checkout.

    ``env`` applies a detected environment (its tfvars files, and its directory as the
    scope); ``ValueError`` if there is no such environment. With ``auto_env`` and no
    ``env`` (nor explicit var files, variables or scope), the default environment is
    applied when environments are detected (:func:`envs.default_env`: dev, else the
    first). Without explicit var files, variables or scope, every detected environment
    is also analyzed on its own (``environment_analysis``: counts per environment and a
    total).

    Every stack (root) is analyzed: with ``auto_env`` or ``stack_envs`` (stack id ->
    environment id, '' for none) each stack gets its own environment (the one chosen,
    else its default), so the stacks all show up side by side (``model["stacks"]``).
    ``env`` applies one environment and limits the analysis to its directory."""
    repo = load_repo(root)
    # a managed run of one environment passes its var files / scope: no per-env section
    explicit = bool(var_files or cli_vars or scope)
    probe = Engine(repo)
    probe.discover_roots()
    environments = detect_environments(repo, probe)
    all_roots = sorted(set(probe.root_modules) | set(probe.units))
    stack_list = stacks(environments, all_roots)
    selected = None
    per_stack: dict[str, Any] | None = None
    if env:
        selected = find_env(environments, env)
        if selected is None:
            known = ", ".join(env_id(e, environments) for e in environments) or "none"
            raise ValueError(f"unknown environment {env!r} (detected: {known})")
        var_files = list(selected["var_files"]) + list(var_files or [])
        if scope is None and selected["root"] != ".":
            scope = selected["root"]
    elif (auto_env or stack_envs) and not explicit:
        per_stack = select_stacks(stack_list, environments, stack_envs or {})
        chosen = {s["selected"] for s in per_stack["stacks"] if s["selected"]}
        primary = default_env([e for e in environments if env_id(e, environments) in chosen])
        selected = primary
    engine = Engine(
        repo,
        var_files,
        cli_vars,
        scope,
        root_var_files=per_stack["root_var_files"] if per_stack else None,
        exclude_roots=per_stack["exclude_roots"] if per_stack else None,
    )
    engine.state_index = state_index(repo, probe, environments)
    if per_stack:
        engine.root_envs = dict(per_stack["root_envs"])
    elif selected is not None:
        engine.root_envs = {r: selected["name"] for r in env_roots(selected, all_roots)}
    engine.run()
    graph = analyze(repo, engine)
    crawled = crawl(repo, git_root, git)
    model = build_model(repo, engine, graph, crawled)
    environments = model["structure"]["environments"]
    sel_id = env_id(selected, environments) if selected is not None else ""
    model["environment"] = (
        {
            "id": sel_id,
            "name": selected["name"],
            "root": selected["root"],
            "var_files": list(engine.var_files)
            if per_stack is None
            else list(selected["var_files"]),
            "backend_hcl": list(selected.get("backend_hcl", [])),
            # applied because none was asked for
            "default": not env and not (stack_envs or {}).get(_stack_of(stack_list, sel_id)),
        }
        if selected is not None
        else None
    )
    model["stacks"] = _stacks_section(
        per_stack["stacks"] if per_stack else stack_list, engine, selected, environments
    )
    trace = model["structure"].get("env_files", [])
    library = library_roots(model)
    for s in model["stacks"]:
        s.update(stack_env_debug(s, trace, model["discovery"]["skipped"]))
        # a reusable module nothing calls: not deployed from here, never counted
        s["library"] = bool(s["roots"]) and set(s["roots"]) <= library
    model["environment_analysis"] = (
        None
        if explicit
        else analyze_environments(
            repo,
            environments,
            model["environment"]["id"] if model["environment"] else None,
            model["stacks"],
            library_roots(model),
        )
    )
    return Analysis(repo, engine, graph, model)


def library_roots(model: dict[str, Any]) -> set[str]:
    """Roots that are reusable modules nothing in the repository calls: not deployed from
    here, so their resources are never counted (they are counted where they are called).
    Only when another root is deployed: a repository that is only such modules is what
    it is (its roots are counted)."""
    roots = (model.get("structure") or {}).get("roots", [])
    library = {r["dir"] for r in roots if r["role"] == "library"}
    return library if len(library) < len(roots) else set()


def _stack_of(stack_list: list[dict[str, Any]], eid: str) -> str:
    """The stack an environment (id) belongs to ('' if none)."""
    return next((s["id"] for s in stack_list if eid in s["environments"]), "")


def _stacks_section(
    stack_list: list[dict[str, Any]],
    engine: Engine,
    selected: dict[str, Any] | None,
    environments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``model["stacks"]``: each stack with its roots (those analyzed in this model), its
    environments and the one applied (the environment selector is per stack)."""
    sel_id = env_id(selected, environments) if selected is not None else None
    out = []
    for s in stack_list:
        chosen = s.get("selected")
        if "selected" not in s:  # one environment applied to its directory (--env)
            chosen = sel_id if sel_id in s["environments"] else None
        out.append(
            {
                "id": s["id"],
                "layout": s["layout"],
                "roots": list(s["roots"]),
                "analyzed": [r for r in s["roots"] if r in engine.roots],
                "environments": list(s["environments"]),
                "env_roots": {k: list(v) for k, v in s["env_roots"].items()},
                "selected": chosen,
                "default": bool(s.get("default")),
            }
        )
    return out


def failure_reason(repo: RepoSource, engine: Engine) -> str:
    """Why nothing can be analyzed ('' when something can)."""
    if not repo.analyzable:
        unreadable = [s for s in repo.skipped if s.kind == "unreadable"]
        reason = (
            f"nothing to analyze: no *.tf, *.tf.json or terragrunt.hcl file among the "
            f"{len(repo.all_files)} file(s) found"
        )
        if unreadable:
            reason += f"; {len(unreadable)} directory(ies) could not be read"
        return reason
    if (
        engine.scope
        and not engine.roots
        and not any(in_scope(u, engine.scope) for u in engine.units)
    ):
        return f"no root module or terragrunt unit under {engine.scope}/"
    return ""


def inventory(repo: RepoSource) -> dict[str, Any]:
    """What the repository contains: the top two directory levels, files by extension."""
    levels: dict[str, Counter[str]] = {}
    total: Counter[str] = Counter()
    for d, counts in repo.dir_ext.items():
        total.update(counts)
        parts = [] if d == "." else d.split("/")
        for depth in range(min(len(parts), 2) + 1):
            key = "/".join(parts[:depth]) or "."
            levels.setdefault(key, Counter()).update(counts)
    skipped = {s.path: s for s in repo.skipped if s.path.count("/") < 2}
    entries = []
    for path in sorted(set(levels) | set(skipped), key=lambda p: (p != ".", p)):
        counts = levels.get(path, Counter())
        entry: dict[str, Any] = {
            "path": path,
            "files": sum(counts.values()),
            "by_extension": {k: counts[k] for k in sorted(counts)},
        }
        if path in skipped:
            entry["skipped"] = skipped[path].reason
        entries.append(entry)
    return {
        "total_files": sum(total.values()),
        "by_extension": {k: total[k] for k in sorted(total)},
        "entries": entries,
    }


def listing_text(inv: dict[str, Any]) -> str:
    """The inventory as plain text (failed runs, the CLI)."""
    lines = ["Repository contents (top 2 levels; files by extension, sub-directories included):"]
    for e in inv["entries"]:
        name = "./" if e["path"] == "." else e["path"] + "/"
        if "skipped" in e:
            lines.append(f"  {name:<32} skipped: {e['skipped']}")
            continue
        exts = ", ".join(f"{k} {v}" for k, v in e["by_extension"].items()) or "no files"
        lines.append(f"  {name:<32} {e['files']} file(s): {exts}")
    return "\n".join(lines)


def _discovery(repo: RepoSource, engine: Engine) -> dict[str, Any]:
    reason = failure_reason(repo, engine)
    return {
        "status": "failed" if reason else "ok",
        "reason": reason,
        "analyzable": repo.analyzable,
        "all_files": list(repo.all_files),
        "truncated": repo.truncated,
        "skipped": [s.to_dict() for s in repo.skipped],
        "inventory": inventory(repo),
    }


def _module_tree(inst: ModuleInstance) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    for name in sorted(inst.src.module_calls):
        child = inst.children.get(name)
        if child is not None:
            children.append(_module_tree(child))
            continue
        call = inst.src.module_calls[name]
        addr = f"{inst.address}.module.{name}" if inst.address else f"module.{name}"
        children.append(
            {
                "id": f"{inst.prefix}{addr}",
                "address": addr,
                "dir": None,
                "source": inst.child_source.get(name),
                "remote": None,
                "location": call.loc.to_dict(),
                "analyzed": False,
                "status": inst.child_status.get(name, "not analyzed"),
                "inputs": {},
                "outputs": {},
                "resources": [],
                "children": [],
            }
        )
    inputs: dict[str, Any] = {}
    if inst.call is not None and inst.parent is not None:
        for name in sorted(inst.call.attrs):
            if name not in _CALL_META:
                inputs[name] = to_json(inst.parent.eval_attr(inst.call.attrs[name]))
    resources = [inst.node_id(t, n) for t, n in sorted(inst.src.resources)]
    resources += [inst.node_id(t, n, data=True) for t, n in sorted(inst.src.data)]
    remote = None
    if inst.call is not None and inst.parent is not None:
        entry = inst.parent.child_remote.get(inst.call.labels[0])
        remote = entry.to_dict() if entry else None
    return {
        "id": inst.module_id or f"{inst.prefix}(root)",
        "address": inst.address,
        "dir": inst.src.dir,
        "source": inst.parent.child_source.get(inst.call.labels[0])
        if inst.call and inst.parent
        else None,
        "remote": remote,
        "location": inst.call.loc.to_dict() if inst.call else None,
        "analyzed": True,
        "status": "analyzed",
        "inputs": inputs,
        "outputs": {name: to_json(inst.output(name)) for name in sorted(inst.src.outputs)},
        "resources": sorted(resources),
        "children": children,
    }


def _variables(engine: Engine) -> list[dict[str, Any]]:
    out = []
    for inst in engine.instances:
        for v in inst.all_vars():
            out.append(
                {
                    "module": inst.module_id or f"{inst.prefix}(root)",
                    "name": v.name,
                    "value": to_json(v.value),
                    "source": v.source,
                    "location": v.location.to_dict(),
                    "has_default": v.declared_default,
                    "description": v.description,
                }
            )
    out.sort(key=lambda d: (d["module"], d["name"]))
    return out


def build_model(
    repo: RepoSource, engine: Engine, graph: Graph, crawled: dict[str, Any] | None = None
) -> dict[str, Any]:
    # first: detection may add the files it reads (env.hcl, *.tfbackend) to repo.files
    structure = detect_structure(repo, engine)
    discovery = _discovery(repo, engine)
    nodes = [graph.nodes[k].to_dict() for k in sorted(graph.nodes)]
    edges = [e.to_dict() for e in graph.edges]
    edge_counts: dict[str, int] = {}
    for e in graph.edges:
        edge_counts[e.kind] = edge_counts.get(e.kind, 0) + 1
    kinds: dict[str, int] = {}
    for n in graph.nodes.values():
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    roots = [i for i in engine.instances if i.parent is None]
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "tflens", "version": __version__},
        "summary": {
            "files": len(repo.files),
            "modules": len(engine.instances),
            "nodes": {k: kinds[k] for k in sorted(kinds)},
            "edges": {k: edge_counts[k] for k in sorted(edge_counts)},
            "unknowns": len(graph.unknowns),
            "parse_errors": len(repo.errors),
        },
        "files": sorted(repo.files),
        "parse_errors": [
            {"file": e.file, "message": e.message}
            for e in sorted(repo.errors, key=lambda e: e.file)
        ],
        "roots": [i.root_dir for i in roots],
        "remote_modules": [
            r.to_dict() for root in sorted(repo.remote) for r in repo.remote[root].values()
        ],
        "modules": [_module_tree(i) for i in roots],
        "variables": _variables(engine),
        "nodes": nodes,
        "edges": edges,
        "unknowns": [u.to_dict() for u in graph.unknowns],
        "not_created": list(graph.not_created),
        "discovery": discovery,
        "structure": structure,
        "crawl": crawled if crawled is not None else crawl(repo, git=False),
        "stack_links": stack_links(nodes, [i.root_dir for i in roots]),
        "docs_diff": docs_diff(repo),
    }


def stack_links(nodes: list[dict[str, Any]], root_dirs: list[str]) -> list[dict[str, Any]]:
    """The ``terraform_remote_state`` data sources: which stack reads which stack's state
    (or a state no stack of the repository writes), with where it is declared."""
    out = []
    for n in nodes:
        rs = (n.get("properties") or {}).get("remote_state")
        if n["kind"] != "data" or rs is None:
            continue
        if "::" in n["id"]:
            owner: str | None = n["id"].split("::", 1)[0]
        else:  # one root analyzed: ids carry no "<root>::" prefix
            owner = root_dirs[0] if len(root_dirs) == 1 else None
        out.append(
            {
                "data_source": n["id"],
                "from_stack": owner,
                "to_stack": rs["stack"],
                "env": rs["env"],
                "backend": rs["backend"],
                "config": rs["config"],
                "status": rs["status"],
                "reason": rs["reason"],
                "location": n["location"],
            }
        )
    return out


def dumps(model: dict[str, Any]) -> str:
    return json.dumps(model, indent=2, ensure_ascii=False) + "\n"
