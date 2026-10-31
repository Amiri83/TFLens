"""Serialize the analysis to model.json (deterministic: sorted, no timestamps, relative paths)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .analyze import analyze
from .engine import Engine, ModuleInstance
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


def run_analysis(
    root: Path, var_files: list[str] | None = None, cli_vars: dict[str, str] | None = None
) -> Analysis:
    repo = load_repo(root)
    engine = Engine(repo, var_files, cli_vars)
    engine.run()
    graph = analyze(repo, engine)
    return Analysis(repo, engine, graph, build_model(repo, engine, graph))


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


def build_model(repo: RepoSource, engine: Engine, graph: Graph) -> dict[str, Any]:
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
    }


def dumps(model: dict[str, Any]) -> str:
    return json.dumps(model, indent=2, ensure_ascii=False) + "\n"
