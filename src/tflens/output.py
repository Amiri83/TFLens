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
from .detect import detect_environments, detect_structure
from .engine import Engine, ModuleInstance, in_scope
from .envs import analyze_environments, default_env, env_id, find_env
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
) -> Analysis:
    """Analyze ``root``. ``scope`` limits the roots to one directory (an environment).
    The git history is read when ``git_root`` (default ``root``) is a git checkout.

    ``env`` applies a detected environment (its tfvars files, and its directory as the
    scope); ``ValueError`` if there is no such environment. With ``auto_env`` and no
    ``env`` (nor explicit var files, variables or scope), the default environment is
    applied when environments are detected (:func:`envs.default_env`: dev, else the
    first). Without explicit var files, variables or scope, every detected environment
    is also analyzed on its own (``environment_analysis``: counts per environment and a
    total)."""
    repo = load_repo(root)
    # a managed run of one environment passes its var files / scope: no per-env section
    explicit = bool(var_files or cli_vars or scope)
    selected = None
    if env or (auto_env and not explicit):
        probe = Engine(repo)
        probe.discover_roots()
        environments = detect_environments(repo, probe)
        selected = find_env(environments, env) if env else default_env(environments)
        if env and selected is None:
            known = ", ".join(env_id(e, environments) for e in environments) or "none"
            raise ValueError(f"unknown environment {env!r} (detected: {known})")
    if selected is not None:
        var_files = list(selected["var_files"]) + list(var_files or [])
        if scope is None and selected["root"] != ".":
            scope = selected["root"]
    engine = Engine(repo, var_files, cli_vars, scope)
    engine.run()
    graph = analyze(repo, engine)
    crawled = crawl(repo, git_root, git)
    model = build_model(repo, engine, graph, crawled)
    environments = model["structure"]["environments"]
    model["environment"] = (
        {
            "id": env_id(selected, environments),
            "name": selected["name"],
            "root": selected["root"],
            "var_files": list(engine.var_files),
            "backend_hcl": list(selected.get("backend_hcl", [])),
            "default": not env,  # applied because none was asked for
        }
        if selected is not None
        else None
    )
    model["environment_analysis"] = (
        None
        if explicit
        else analyze_environments(
            repo, environments, model["environment"]["id"] if model["environment"] else None
        )
    )
    return Analysis(repo, engine, graph, model)


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
    }


def dumps(model: dict[str, Any]) -> str:
    return json.dumps(model, indent=2, ensure_ascii=False) + "\n"
