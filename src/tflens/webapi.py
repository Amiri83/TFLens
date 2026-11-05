"""Logic behind ``tflens serve``: plain functions over the model that return dicts.

The HTTP server (``server.py``) is a thin wrapper around :meth:`Api.handle`, so
everything here can be tested without sockets. Nothing in this module makes
network calls; it reads model.json (regenerating it from the repository when it
is missing or unusable) and repository files listed in the model.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .canvas import KIND_COLOURS, canvas_view, icons_view, node_icon
from .cards import stack_cards
from .drawio import MIME as DRAWIO_MIME
from .drawio import chain_drawio, tab_drawio
from .envs import (
    difference_lines,
    env_counts_text,
    inventory_rows,
    node_root,
    scope_counts,
    summarize,
    unknown_groups,
)
from .flow import build_flows, chain_svg, confirmed_flows
from .graphlayout import layout as graph_layout
from .icons import service_colour
from .jobs import JobBusy, JobContext, JobManager
from .labels import call_name, node_label
from .layouts import LayoutError, SqliteLayouts
from .model import EDGE_KINDS, SCHEMA_VERSION
from .output import dumps, library_roots
from .overview import build_overview, confirmed_overview
from .pipeline import Options, discover
from .report import describe_node
from .shapes import build_root_view, build_shapes, root_entries, root_model
from .snippets import SourceLines, snippet_lines
from .tabdiagrams import DIAGRAMS as TAB_DIAGRAMS
from .tabdiagrams import tab_svg
from .visuals import (
    DIAGRAMS,
    ROOT_PREFIX,
    backend_text,
    env_matrix_table,
    mermaid,
    root_node_id,
    svg,
)

STATIC_DIR = Path(__file__).parent / "static"

# Service colours: icons.SERVICE_COLOURS (fixed per service, shared with the diagrams).
EXTERNAL_COLOR = service_colour("external")
META_COLOR = service_colour("meta")
# Edge type colours: the same in the Graph, every diagram and the legend (canvas.py).
KIND_COLORS = {k: KIND_COLOURS[k] for k in ("ACCESS", "TRIGGER", "NETWORK", "CONFIG", "RUNS_ON")}
ROOT_MODULE = "(root)"
# Data sources that only supply context (account, region, policy JSON); the UI
# classifies them as service "meta" and hides them by default.
META_DATA_TYPES = frozenset(
    {"aws_caller_identity", "aws_region", "aws_partition", "aws_iam_policy_document"}
)
META_SERVICE = "meta"
DEFAULT_HIDDEN_KINDS = frozenset({"CONFIG"})
GROUP_PREFIX = "group:"


def default_model_path(root: Path) -> Path:
    return root / "tflens-out" / "model.json"


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temporary file in the same directory and a
    rename: a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class ModelUnavailable(RuntimeError):
    """A read-only store's model.json is missing or unusable."""


class ModelStore:
    """Holds the current model; re-reads model.json when it changes on disk and
    regenerates it from the repository when it is missing or unusable.

    A ``readonly`` store (a recorded run) never regenerates or writes anything: a
    missing / unusable model.json raises :class:`ModelUnavailable`."""

    def __init__(
        self,
        root: Path,
        model_path: Path | None = None,
        var_files: list[str] | None = None,
        cli_vars: dict[str, str] | None = None,
        *,
        readonly: bool = False,
        title: str = "",
        env: str | None = None,
        auto_env: bool = False,
        aws: Any = None,
    ) -> None:
        self.root = root.resolve()
        # the AWS account Discover may check against (awscheck.AwsTarget; None: none
        # configured, the AWS phase is then unavailable)
        self.aws = aws
        self.readonly = readonly
        self.title = title or self.root.name
        self.model_path = model_path or default_model_path(self.root)
        self.var_files = var_files or []
        self.cli_vars = cli_vars or {}
        self.env = env or None  # a detected environment applied to the analysis
        # no environment asked for: apply the default one (dev, else the first) until
        # one, or none, is chosen in the switcher
        self.auto_env = auto_env and not (self.env or self.var_files or self.cli_vars)
        # the environment chosen per stack (stack id -> environment id, '' for none); the
        # other stacks keep their default
        self.stack_envs: dict[str, str] = {}
        self.generated = False  # whether the current model was produced by this process
        self.note = ""
        self._model: dict[str, Any] | None = None
        self._mtime: float | None = None
        self._lines: dict[str, list[str]] = {}
        self._lock = threading.RLock()

    def model(self) -> dict[str, Any]:
        with self._lock:
            mtime = self._stat()
            if self._model is not None and mtime == self._mtime:
                return self._model
            loaded = self._read() if mtime is not None else None
            if loaded is None:
                if self.readonly:
                    raise ModelUnavailable(f"model {self.model_path} is missing or unusable")
                return self.regenerate()
            if self._wants_default(loaded):
                return self.regenerate()
            self._set(loaded, mtime, generated=False)
            return loaded

    def _wants_default(self, model: dict[str, Any]) -> bool:
        """A model on disk analyzed with no environment although environments are
        detected and none was chosen: it is re-analyzed with the default one."""
        return (
            self.auto_env
            and not self.readonly
            and not self.stack_envs
            and model.get("environment") is None
            and bool((model.get("structure") or {}).get("environments"))
        )

    def regenerate(self) -> dict[str, Any]:
        """Discover synchronously (no progress, no AWS check) and serve the result."""
        if self.readonly:
            raise ModelUnavailable("a recorded run cannot be regenerated")
        with self._lock:
            return self.discover()

    def discover(self, progress: Any = None, aws: bool = False) -> dict[str, Any]:
        """Run the discovery pipeline with the current environment selection; the model
        is written (atomically) and served only once the run can no longer be cancelled.
        The store's lock is held only while the result is put in place, so the analyzer
        keeps serving the previous model while a Discover runs."""
        if self.readonly:
            raise ModelUnavailable("a recorded run cannot be regenerated")
        with self._lock:
            opts = Options(
                var_files=list(self.var_files),
                cli_vars=dict(self.cli_vars),
                env=self.env,
                auto_env=self.auto_env,
                stack_envs=dict(self.stack_envs) if not self.env else None,
                aws=self.aws if aws else None,
                aws_requested=aws,
            )

        def write(analysis: Any) -> dict[str, Any]:
            model = analysis.model
            with self._lock:
                try:
                    write_atomic(self.model_path, dumps(model))
                    self.note = f"regenerated {self.model_path}"
                except OSError as exc:
                    self.note = (
                        f"regenerated in memory only (cannot write {self.model_path}: {exc})"
                    )
                self._set(model, self._stat(), generated=True)
            return model

        return discover(self.root, opts, progress, write).written

    def select_env(self, env: str | None) -> dict[str, Any]:
        """Re-analyze with a detected environment's tfvars (None / '': no environment, not
        even the default one); ``ValueError`` for an environment that is not detected (the
        selection is kept)."""
        if self.readonly:
            raise ModelUnavailable("a recorded run cannot switch environments")
        with self._lock:
            previous = self.env, self.auto_env, dict(self.stack_envs)
            self.env, self.auto_env, self.stack_envs = env or None, False, {}
            try:
                return self.regenerate()
            except ValueError:
                self.env, self.auto_env, self.stack_envs = previous
                raise

    def select_stack_env(self, stack: str, env: str | None) -> dict[str, Any]:
        """Re-analyze with ``env`` (an environment id, None / '': none) applied to one
        stack; every other stack keeps its selection (or its default)."""
        if self.readonly:
            raise ModelUnavailable("a recorded run cannot switch environments")
        with self._lock:
            previous = self.env, self.auto_env, dict(self.stack_envs)
            self.env, self.auto_env = None, True
            self.stack_envs = {**self.stack_envs, stack: env or ""}
            try:
                return self.regenerate()
            except ValueError:
                self.env, self.auto_env, self.stack_envs = previous
                raise

    def lines(self, file: str) -> list[str] | None:
        """Lines of a repository file listed in the model (never anything else)."""
        with self._lock:
            if file in self._lines:
                return self._lines[file]
            model = self.model()
            readable = (model.get("crawl") or {}).get("readable", [])
            if file not in model["files"] and file not in readable:
                return None
            path = (self.root / file).resolve()
            try:
                path.relative_to(self.root)
                text = path.read_text(encoding="utf-8")
            except (OSError, ValueError, UnicodeDecodeError):
                return None
            self._lines[file] = text.splitlines()
            return self._lines[file]

    def _set(self, model: dict[str, Any], mtime: float | None, generated: bool) -> None:
        self._model, self._mtime, self.generated = model, mtime, generated
        self._lines.clear()

    def _stat(self) -> float | None:
        try:
            return self.model_path.stat().st_mtime
        except OSError:
            return None

    def _read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.model_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return None
        if not all(k in data for k in ("nodes", "edges", "unknowns", "files", "modules")):
            return None
        return data


# ---------------------------------------------------------------- pure views


def _module_of(node: dict[str, Any]) -> str:
    return node["module"] or ROOT_MODULE


def _service_of(node: dict[str, Any]) -> str:
    if node["kind"] == "data" and node["type"] in META_DATA_TYPES:
        return META_SERVICE
    return node["service"]


def short_name(node: dict[str, Any]) -> str:
    """Display name without the module path or the ``aws_`` type prefix
    (``module.worker.aws_iam_role.this`` -> ``iam_role.this``); external ARNs are
    cut to their resource part (``arn:aws:iam::1:role/x`` -> ``role/x``)."""
    if node["kind"] == "external":
        name = node["name"]
        return name.rsplit(":", 1)[-1] if name.startswith("arn:") else name
    if node["kind"] == "module":
        return node["name"]
    prefix = f"{node['module']}." if node["module"] else ""
    local = node["id"][len(prefix) :] if prefix and node["id"].startswith(prefix) else node["id"]
    data = local.startswith("data.")
    if data:
        local = local[len("data.") :]
    if local.startswith("aws_"):
        local = local[len("aws_") :]
    return f"data.{local}" if data else local


def _compact(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "name": node["name"],
        "short_name": short_name(node),
        "type": node["type"],
        "kind": node["kind"],
        "service": _service_of(node),
        "category": node["category"],
        "module": _module_of(node),
        "location": node["location"],
        "analyzed": node.get("analyzed", True),
    }


def _unanalyzed_modules(model: dict[str, Any], every: bool = False) -> dict[str, dict[str, Any]]:
    """Module calls that could not be analyzed have no graph node; describe them
    from the module tree so the UI can still show where they are and why (``every``:
    each module call of the tree, analyzed or not)."""
    out: dict[str, dict[str, Any]] = {}

    def walk(mods: list[dict[str, Any]], parent: str) -> None:
        for m in mods:
            if not m["analyzed"] or (every and m["address"]):
                out[m["id"]] = {
                    "id": m["id"],
                    "kind": "module",
                    "type": "module",
                    "name": m["address"].rsplit(".", 1)[-1],
                    "service": "terraform",
                    "category": "module",
                    "parent": parent or None,
                    "module": parent,
                    "location": m["location"],
                    "properties": {"source": m["source"], "status": m["status"]},
                    "attributes": {},
                    "analyzed": m["analyzed"],
                }
            walk(m["children"], m["id"] if m["address"] else "")

    walk(model["modules"], "")
    return out


def service_colors(model: dict[str, Any]) -> dict[str, str]:
    """One fixed colour per service (the same in every repository and every view)."""
    services = {_service_of(n) for n in model["nodes"] if n["service"]} - {META_SERVICE}
    colors = {s: service_colour(s) for s in sorted(services)}
    colors[META_SERVICE] = META_COLOR
    return colors


def _group_id(module: str) -> str:
    return GROUP_PREFIX + module


def _root_group_id(root: str) -> str:
    return GROUP_PREFIX + ROOT_PREFIX + root


def _module_root(module: str, roots: list[str]) -> str | None:
    if "::" in module:
        return module.split("::", 1)[0]
    return roots[0] if len(roots) == 1 else None


def _parent_module(module: str) -> str:
    """``module.a.module.b`` -> ``module.a``; top-level modules have no parent ("")."""
    head, sep, _ = module.rpartition(".module.")
    return head if sep else ""


def module_groups(modules: set[str], roots: list[str] | None = None) -> list[dict[str, Any]]:
    """Compound parent nodes, one per non-root module (and each enclosing module). The
    label is the module call's name (``worker · module``); ``node`` is the module call to
    open on click; ``root_group`` the root a top-level call belongs to."""
    wanted: set[str] = set()
    for m in modules:
        while m and m != ROOT_MODULE and m not in wanted:
            wanted.add(m)
            m = _parent_module(m)
    out = []
    for m in sorted(wanted):
        root = _module_root(m, roots or [])
        out.append(
            {
                "data": {
                    "id": _group_id(m),
                    "label": f"{call_name(m) or m} · module",
                    "module": m,
                    "node": m,
                    "parent": _group_id(_parent_module(m)) if _parent_module(m) else None,
                    "root": root,
                    "root_group": _root_group_id(root) if root is not None else None,
                },
                "classes": "group",
            }
        )
    return out


def _cluster_path(module: str, root: str | None) -> list[str]:
    """The layout clusters of a node, outermost first: its root, then each module call."""
    chain = []
    m = module if module and module != ROOT_MODULE else ""
    while m:
        chain.append(_group_id(m))
        m = _parent_module(m)
    head = [_root_group_id(root)] if root is not None else ["group:external"]
    return head + chain[::-1]


def _default_visible(model: dict[str, Any], kinds: set[str]) -> set[str]:
    """Nodes the Graph shows before any filter is touched (not meta, connected by an edge
    of a kind shown by default): they come first in their cluster's grid."""
    meta = {n["id"] for n in model["nodes"] if _service_of(n) == META_SERVICE}
    out: set[str] = set()
    for e in model["edges"]:
        if e["kind"] in kinds and e["source"] not in meta and e["target"] not in meta:
            out.update((e["source"], e["target"]))
    return out


def edge_short_label(edge: dict[str, Any]) -> str:
    actions = edge["attributes"].get("actions")
    if isinstance(actions, list):
        return f"{len(actions)} action" + ("" if len(actions) == 1 else "s")
    label = edge["label"]
    return label if len(label) <= 24 else label[:23] + "…"


def graph_view(model: dict[str, Any]) -> dict[str, Any]:
    """Cytoscape elements (module call nodes are left to the repo view). Nodes
    carry ``group`` (their module's compound parent id, if any); the UI sets
    ``parent`` from it when grouping by module is enabled."""
    colors = service_colors(model)
    roots = list(model.get("roots") or [])
    nodes = []
    for n in model["nodes"]:
        if n["kind"] == "module":
            continue
        service = _service_of(n)
        root = node_root(n["id"], roots)
        nodes.append(
            {
                "data": {
                    "id": n["id"],
                    "label": short_name(n),
                    # what the Graph draws: module call + type, never a bare "this"
                    "display": node_label(n, root),
                    "short_name": short_name(n),
                    "name": n["name"],
                    "type": n["type"],
                    "kind": n["kind"],
                    "category": n["category"],
                    "service": service or "external",
                    "module": _module_of(n),
                    "group": _group_id(n["module"]) if n["module"] else None,
                    "root": root,
                    "root_group": _root_group_id(root) if root is not None else None,
                    "file": n["location"]["file"] if n["location"] else "",
                    "color": colors.get(service, EXTERNAL_COLOR),
                    "icon": node_icon(n["type"], service),
                }
            }
        )
    ids = {d["data"]["id"] for d in nodes}
    edges, dropped = [], []
    for e in model["edges"]:
        # an edge to something the Graph does not draw (a module call) would make
        # Cytoscape reject it: listed instead of silently lost
        if e["source"] not in ids or e["target"] not in ids:
            dropped.append(e["id"])
            continue
        edges.append(
            {
                "data": {
                    "id": e["id"],
                    "source": e["source"],
                    "target": e["target"],
                    "kind": e["kind"],
                    "label": e["label"],
                    "short_label": edge_short_label(e),
                    "color": KIND_COLORS.get(e["kind"], "#000000"),
                }
            }
        )
    services = sorted({d["data"]["service"] for d in nodes})
    kinds = [k for k in EDGE_KINDS if any(e["data"]["kind"] == k for e in edges)]
    groups = module_groups({d["data"]["module"] for d in nodes}, roots)
    structure = _structure_elements(model, nodes, groups, roots)
    shown = _default_visible(model, {k for k in kinds if k not in DEFAULT_HIDDEN_KINDS})
    positions, boxes = graph_layout(
        [
            (
                d["data"]["id"],
                _cluster_path(d["data"]["module"], d["data"]["root"]),
                (d["data"]["id"] not in shown, d["data"]["service"], d["data"]["display"]),
            )
            for d in nodes
        ]
    )
    # ungrouped: each module call / root is a node at the top of its cluster's box
    for s in structure["nodes"]:
        box = boxes.get(s["data"]["cluster"])
        if box:
            positions[s["data"]["id"]] = {"x": box["x"] + box["w"] / 2, "y": box["y"] + 12}
    return {
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "root_groups": structure["root_groups"],
        "structure": {"nodes": structure["nodes"], "edges": structure["edges"]},
        "positions": positions,
        "dropped_edges": dropped,
        "filters": {
            "kinds": kinds,
            "modules": sorted({d["data"]["module"] for d in nodes}),
            "services": services,
        },
        # initial UI state: CONFIG edges and meta data sources start hidden
        "defaults": {
            "kinds": [k for k in kinds if k not in DEFAULT_HIDDEN_KINDS],
            "show_meta": False,
            "group_by_module": True,
        },
        "colors": {
            "services": {s: colors.get(s, EXTERNAL_COLOR) for s in services},
            "kinds": dict(KIND_COLORS),
        },
    }


def _structure_elements(
    model: dict[str, Any],
    nodes: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    roots: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Containment: one compound per root (the module groups nest inside it when the
    Graph groups by module) and, for the ungrouped Graph, one node per root and module
    call with a CONTAINS edge root -> module call -> each resource."""
    root_dirs = sorted({d["data"]["root"] for d in nodes + groups if d["data"]["root"] is not None})
    color = service_colour("terraform")
    root_groups = [
        {
            "data": {
                "id": _root_group_id(r),
                "label": f"root {r}/" if r != "." else "root ./",
                "node": root_node_id(r),
                "parent": None,
            },
            "classes": "group root-group",
        }
        for r in root_dirs
    ]
    s_nodes = [
        {
            "data": {
                "id": root_node_id(r),
                "label": f"root {r}/" if r != "." else "root ./",
                "display": f"root {r}/" if r != "." else "root ./",
                "kind": "root",
                "service": "terraform",
                "module": ROOT_MODULE,
                "color": color,
                "icon": "terraform",
                "cluster": _root_group_id(r),
            },
            "classes": "structure",
        }
        for r in root_dirs
    ]
    s_edges = []

    def contains(parent: str, child: str) -> None:
        s_edges.append(
            {
                "data": {
                    "id": f"contains:{parent}->{child}",
                    "source": parent,
                    "target": child,
                    "kind": "CONTAINS",
                    "label": "contains",
                    "short_label": "",
                    "color": "#8c959f",
                }
            }
        )

    for g in groups:
        d = g["data"]
        s_nodes.append(
            {
                "data": {
                    "id": d["module"],
                    "label": d["label"],
                    "display": d["label"],
                    "kind": "module",
                    "service": "terraform",
                    "module": d["module"],
                    "color": color,
                    "icon": "module-local",
                    "cluster": d["id"],
                },
                "classes": "structure",
            }
        )
        parent = _parent_module(d["module"])
        if parent:
            contains(parent, d["module"])
        elif d["root"] is not None:
            contains(root_node_id(d["root"]), d["module"])
    for n in nodes:
        d = n["data"]
        if d["module"] != ROOT_MODULE:
            contains(d["module"], d["id"])
        elif d["root"] is not None:
            contains(root_node_id(d["root"]), d["id"])
    return {"root_groups": root_groups, "nodes": s_nodes, "edges": s_edges}


def _evidence_items(edge: dict[str, Any], source: SourceLines | None) -> list[dict[str, Any]]:
    return [
        {
            "edge": edge["id"],
            "kind": edge["kind"],
            "label": edge["label"],
            "file": ev["file"],
            "start_line": ev["start_line"],
            "end_line": ev["end_line"],
            "note": ev.get("note", ""),
            "lines": snippet_lines(ev, source),
        }
        for ev in edge["evidence"]
    ]


def _defined_by(node: dict[str, Any], source: SourceLines | None) -> dict[str, Any] | None:
    loc = node["location"]
    if not loc:
        return None
    return {
        "node": node["id"],
        "module": _module_of(node),
        "file": loc["file"],
        "start_line": loc["start_line"],
        "end_line": loc["end_line"],
        "lines": snippet_lines(loc, source, context=0),
    }


def _edge_ref(edge: dict[str, Any], other: str) -> dict[str, Any]:
    return {"id": edge["id"], "kind": edge["kind"], "label": edge["label"], "other": other}


def node_detail(
    model: dict[str, Any], node_id: str, source: SourceLines | None = None
) -> dict[str, Any] | None:
    if node_id.startswith(ROOT_PREFIX):
        return root_detail(model, node_id[len(ROOT_PREFIX) :], source)
    node = next((n for n in model["nodes"] if n["id"] == node_id), None)
    node = node or _unanalyzed_modules(model, every=True).get(node_id)
    if node is None:
        return None
    out_edges = [e for e in model["edges"] if e["source"] == node_id]
    in_edges = [e for e in model["edges"] if e["target"] == node_id]
    if node["kind"] == "module":
        description = f"Terraform module call (source {node['properties'].get('source')!r})"
        if not node.get("analyzed", True):
            description += ", not analyzed"
    elif node["kind"] == "external":
        description = f"external {node['type']} (outside the repository)"
    else:
        description = describe_node(node)
    evidence = []
    for e in out_edges + in_edges:
        evidence += _evidence_items(e, source)
    return {
        "type": "node",
        "id": node_id,
        "title": node_id,
        "short_name": short_name(node),
        "summary": {
            "description": description,
            "short_name": short_name(node),
            "label": node_label(node, node_root(node_id, list(model.get("roots") or []))),
            "kind": node["kind"],
            "type": node["type"],
            "service": _service_of(node),
            "module": _module_of(node),
            "outgoing": len(out_edges),
            "incoming": len(in_edges),
        },
        "node": node,
        "defined_by": _defined_by(node, source),
        "edges": {
            "outgoing": [_edge_ref(e, e["target"]) for e in out_edges],
            "incoming": [_edge_ref(e, e["source"]) for e in in_edges],
        },
        "evidence": evidence,
        "unknowns": [u for u in model["unknowns"] if u["subject"] == node_id],
    }


def root_detail(
    model: dict[str, Any], root_dir: str, source: SourceLines | None = None
) -> dict[str, Any] | None:
    """A root (stack) as a node of the pane: its backend block as the defining code, the
    signals that make it a root as evidence, the unknowns of its resources."""
    root = next(
        (r for r in (model.get("structure") or {}).get("roots", []) if r["dir"] == root_dir), None
    )
    if root is None:
        return None
    roots = list(model.get("roots") or [])
    backend = root["backend"]
    loc = backend.get("location")
    defined = (
        {
            "node": ROOT_PREFIX + root_dir,
            "module": ROOT_MODULE,
            **{k: loc[k] for k in ("file", "start_line", "end_line")},
            "lines": snippet_lines(loc, source, context=2),
        }
        if loc
        else None
    )
    evidence = []
    for sig in root.get("signals") or []:
        ev = sig.get("location")
        if not ev:
            continue
        evidence.append(
            {
                "edge": "",
                "kind": "ROOT",
                "label": sig["signal"],
                **{k: ev[k] for k in ("file", "start_line", "end_line")},
                "note": "",
                "lines": snippet_lines(ev, source),
            }
        )
    label = f"root {root_dir}/" if root_dir != "." else "root ./"
    modules = [
        {
            "address": m["address"],
            "node": f"{root_dir}::{m['address']}" if len(roots) > 1 else m["address"],
        }
        for m in root.get("modules") or []
    ]
    return {
        "type": "node",
        "id": ROOT_PREFIX + root_dir,
        "title": label,
        "short_name": label,
        "summary": {
            "description": f"{root['kind']} root ({root.get('role', 'deployable')}), "
            f"{backend_text(backend)} backend — {backend.get('origin', '')}",
            "short_name": label,
            "label": label,
            "kind": "root",
            "type": "root",
            "service": "terraform",
            "module": ROOT_MODULE,
            "outgoing": len(modules),
            "incoming": 0,
        },
        "node": {"properties": {}},
        "defined_by": defined,
        "edges": {"outgoing": [], "incoming": []},
        "modules": modules,
        "evidence": evidence,
        "unknowns": [u for u in model["unknowns"] if node_root(u["subject"], roots) == root_dir],
    }


def edge_detail(
    model: dict[str, Any], edge_id: str, source: SourceLines | None = None
) -> dict[str, Any] | None:
    edge = next((e for e in model["edges"] if e["id"] == edge_id), None)
    if edge is None:
        return None
    nodes = {n["id"]: n for n in model["nodes"]}
    # the blocks that create the relationship: the "via" resources, else the source
    definers = [v for v in edge["via"] if v in nodes] or [edge["source"]]
    subjects = {edge["source"], edge["target"], *edge["via"]}
    return {
        "type": "edge",
        "id": edge_id,
        "title": f"{edge['kind']}: {edge['source']} → {edge['target']}",
        "summary": {
            "kind": edge["kind"],
            "label": edge["label"],
            "source": edge["source"],
            "target": edge["target"],
            "via": edge["via"],
            "attributes": edge["attributes"],
        },
        "edge": edge,
        "source": _compact(nodes[edge["source"]]) if edge["source"] in nodes else None,
        "target": _compact(nodes[edge["target"]]) if edge["target"] in nodes else None,
        "defined_by": [
            d for d in (_defined_by(nodes[i], source) for i in definers if i in nodes) if d
        ],
        "evidence": _evidence_items(edge, source),
        "unknowns": [u for u in model["unknowns"] if u["subject"] in subjects],
    }


def _flatten_modules(mods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in mods:
        out.append(m)
        out += _flatten_modules(m["children"])
    return out


def _modules_by_dir(model: dict[str, Any]) -> dict[str, list[str]]:
    by_dir: dict[str, list[str]] = {}
    for m in _flatten_modules(model["modules"]):
        if m["dir"] is not None:
            by_dir.setdefault(m["dir"], []).append(m["address"] or ROOT_MODULE)
    return {k: sorted(set(v)) for k, v in by_dir.items()}


def _defined_in(model: dict[str, Any], path: str) -> list[dict[str, Any]]:
    candidates = model["nodes"] + list(_unanalyzed_modules(model).values())
    here = [n for n in candidates if n["location"] and n["location"]["file"] == path]
    here.sort(key=lambda n: (n["location"]["start_line"], n["id"]))
    return [_compact(n) for n in here]


def _all_files(model: dict[str, Any]) -> list[str]:
    """Every file discovery saw (models written before discovery: the analyzed files)."""
    disc = model.get("discovery")
    return sorted(set(disc["all_files"]) | set(model["files"])) if disc else model["files"]


def repo_tree(model: dict[str, Any], title: str = "") -> dict[str, Any]:
    """Directory/file tree of the whole repository: analyzed files are marked
    (``analyzed``), directories discovery did not walk carry ``skipped`` (the reason),
    and each directory lists its module addresses."""
    by_dir = _modules_by_dir(model)
    analyzed = set(model["files"])
    roots = {r["dir"] for r in (model.get("structure") or {}).get("roots", [])}
    counts: dict[str, int] = {}
    for n in model["nodes"]:
        if n["location"] and n["kind"] in ("resource", "data"):
            counts[n["location"]["file"]] = counts.get(n["location"]["file"], 0) + 1
    root: dict[str, Any] = {
        "name": title or ".",
        "path": ".",
        "type": "dir",
        "modules": by_dir.get(".", []),
        "root": "." in roots,
        "children": [],
    }
    dirs = {".": root}

    def ensure_dir(path: str) -> dict[str, Any]:
        if path in dirs:
            return dirs[path]
        parent = ensure_dir(str(PurePosixPath(path).parent))
        d = {
            "name": PurePosixPath(path).name,
            "path": path,
            "type": "dir",
            "modules": by_dir.get(path, []),
            "root": path in roots,
            "children": [],
        }
        dirs[path] = d
        parent["children"].append(d)
        return d

    for f in _all_files(model):
        parent = ensure_dir(str(PurePosixPath(f).parent))
        parent["children"].append(
            {
                "name": PurePosixPath(f).name,
                "path": f,
                "type": "file",
                "analyzed": f in analyzed,
                "resources": counts.get(f, 0),
            }
        )
    for s in (model.get("discovery") or {}).get("skipped", []):
        d = ensure_dir(s["path"])
        d["skipped"] = s["reason"]
        d["skipped_kind"] = s["kind"]
    _sort_tree(root)
    return root


def _sort_tree(d: dict[str, Any]) -> None:
    d["children"].sort(key=lambda c: (c["type"] != "dir", c["name"]))
    for c in d["children"]:
        if c["type"] == "dir":
            _sort_tree(c)


def file_detail(model: dict[str, Any], path: str) -> dict[str, Any] | None:
    if path not in model["files"]:
        if path not in _all_files(model):
            return None
        readable = path in (model.get("crawl") or {}).get("readable", [])
        return {
            "path": path,
            "analyzed": False,
            "readable": readable,
            "note": "Not analyzed: TFLens reads *.tf, *.tf.json, *.tfvars(.json) and "
            "terragrunt.hcl files only." + (" The source can be shown." if readable else ""),
            "modules": [],
            "resources": [],
            "unknowns": [],
        }
    parent = str(PurePosixPath(path).parent)
    return {
        "path": path,
        "analyzed": True,
        "modules": _modules_by_dir(model).get(parent, []),
        "resources": _defined_in(model, path),
        "unknowns": [
            u for u in model["unknowns"] if u["location"] and u["location"]["file"] == path
        ],
    }


def source_view(
    path: str, start: int, end: int, source: SourceLines | None
) -> dict[str, Any] | None:
    """A ``file:line`` reference with its snippet (only files listed in the model)."""
    lines = source(path) if source is not None else None
    if lines is None:
        return None
    start = max(1, min(start, len(lines) or 1))
    end = max(start, min(end, len(lines) or 1))
    loc = {"file": path, "start_line": start, "end_line": end}
    return {**loc, "lines": snippet_lines(loc, source, context=3)}


def environments_view(model: dict[str, Any], store: ModelStore | None = None) -> dict[str, Any]:
    """The Environments view and the switcher: one row per detected environment (its
    tfvars applied) with its files, wiring and state file, the service inventory (one
    column per environment, the rows that differ between them flagged), a total row, and
    the counts of the model shown. Without environments the inventory has one column:
    the model shown (variable defaults)."""
    analysis = model.get("environment_analysis") or {}
    active = model.get("environment")
    rows = [{**r, "text": env_counts_text(r)} for r in analysis.get("environments", [])]
    # reusable modules nothing calls are not deployed from here: never counted
    library = set(analysis.get("library_roots") or library_roots(model))
    roots = list(model.get("roots") or [])
    shown_nodes = [n for n in model["nodes"] if node_root(n["id"], roots) not in library]
    stack_of = {r: s["id"] for s in model.get("stacks", []) for r in s["roots"]}
    current = summarize(
        None,
        shown_nodes,
        model["unknowns"],
        roots=roots,
        stack_of=stack_of,
        env_name=active["name"] if active else None,
    )
    per_env = bool(rows) and all("inventory" in r for r in rows)  # older model.json: no
    # one column per environment name (every stack with its own tfvars); older models:
    # one per environment row
    columns = [
        {**c, "text": env_counts_text(c)}
        for c in analysis.get("columns") or (rows if per_env else [])
    ]
    inventory = analysis.get("inventory") if per_env else None
    if inventory is None:
        cols = [(c["id"], c["inventory"]) for c in columns]
        inventory = inventory_rows(cols or [("", current["inventory"])])
    out: dict[str, Any] = {
        "active": active["id"] if active else None,
        "active_name": active["name"] if active else None,
        "default": bool(active and active.get("default")),  # applied as none was chosen
        "switchable": store is not None and not store.readonly,
        "types": analysis.get("types", []),
        "environments": [public_row(r) for r in rows],
        "columns": columns,
        "inventory": inventory,
        "library_roots": sorted(library),
        "differences": difference_lines({**analysis, "inventory": inventory}) if per_env else [],
        "inventory_columns": "environments" if per_env else "current",
        "not_wired": [r["id"] for r in rows if not r.get("wired", True)],
        "total": None,
        "skipped": analysis.get("skipped", []),
        "current": {**current, "text": env_counts_text(current)},
        # one environment selector per stack (the stacks with environments)
        "stacks": model.get("stacks", []),
        "selected": analysis.get("selected") or ([active["id"]] if active else []),
        "stack_links": model.get("stack_links", []),
    }
    if rows:
        total = analysis["total"]
        out["total"] = {**total, "text": env_counts_text(total)}
    out["counts"] = scope_counts(model)
    # the grouped Unknowns panel: of the model shown, of each column and each row
    out["current"]["unknown_groups"] = unknown_groups(current["unknowns"])
    for section in (out["environments"], out["columns"]):
        for row in section:
            row["unknown_groups"] = unknown_groups(row.get("unknowns") or [])
    return out


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    """An environment row of the UI: its real files and verified commands, without the CI
    lines naming it."""
    out = {k: v for k, v in row.items() if k != "var_file_cli"}
    out["wiring"] = [w for w in row.get("wiring", []) if not w.startswith("CI / scripts:")]
    out["commands"] = list(row.get("commands") or [])
    return out


def public_model(model: dict[str, Any]) -> dict[str, Any]:
    """``api/model``: the model without what was read from docs, CI and the git log, the
    validation of those hints and the rejected candidate files (model.json keeps them)."""
    out = {k: v for k, v in model.items() if k not in ("crawl", "docs_diff", "pipeline")}
    structure = model.get("structure")
    if structure:
        out["structure"] = {
            **{k: v for k, v in structure.items() if k != "env_files"},
            "environments": [public_row(e) for e in structure.get("environments", [])],
        }
    return out


def aws_check(model: dict[str, Any]) -> dict[str, Any] | None:
    """The AWS check of a run that asked for one (None: it was off)."""
    aws = (model.get("pipeline") or {}).get("aws")
    return aws if aws and aws.get("status") != "off" else None


def summary_view(model: dict[str, Any], store: ModelStore | None = None) -> dict[str, Any]:
    disc = model.get("discovery") or {}
    active = model.get("environment")
    out = {
        "title": store.title if store else "",
        "summary": model["summary"],
        "roots": model["roots"],
        "parse_errors": model["parse_errors"],
        "remote_modules": model.get("remote_modules", []),
        "tool": model.get("tool"),
        "status": disc.get("status", "ok"),
        "reason": disc.get("reason", ""),
        "skipped": disc.get("skipped", []),
        "flow": build_flows(model)["metrics"],
        "environment": active["id"] if active else None,
        "shapes": [
            {"dir": r["dir"], "shape": r["shape"], "view": r["view"]}
            for r in build_shapes(model)["roots"]
        ],
    }
    if store is not None:
        out["model_path"] = str(store.model_path)
        out["generated"] = store.generated
        out["note"] = store.note
        out["readonly"] = store.readonly
    return out


# ---------------------------------------------------------------- dispatch


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, indent=1, ensure_ascii=False, sort_keys=False).encode("utf-8")
    return Response(status, "application/json; charset=utf-8", body)


def _not_found(what: str) -> Response:
    return json_response({"error": "not found", "detail": what}, 404)


class Api:
    def __init__(
        self,
        store: ModelStore,
        jobs: JobManager | None = None,
        layouts: SqliteLayouts | None = None,
    ) -> None:
        self.store = store
        self.jobs = jobs or JobManager()
        # saved canvas layouts: a recorded run's rows in the app database; standalone, a
        # database next to model.json (written on the first save only)
        self.layouts = layouts or SqliteLayouts(store.model_path.parent / "layouts.sqlite")

    def _layout_api(self, method: str, path: str, query: dict[str, str], body: bytes) -> Response:
        """``GET api/layout?view=V``: the saved positions (``null``: none, the canvas
        starts from its own layout); ``POST`` with ``{"positions": {...}}`` saves them;
        ``POST api/layout/reset?view=V`` forgets them (the "Reset layout" button)."""
        view = query.get("view", "")
        try:
            if path == "/api/layout/reset" and method == "POST":
                return json_response({"ok": True, "view": view, "reset": self.layouts.reset(view)})
            if path == "/api/layout" and method == "POST":
                try:
                    data = json.loads(body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, ValueError):
                    return json_response({"error": "body must be JSON"}, 400)
                if not isinstance(data, dict):
                    return json_response({"error": "body must be a JSON object"}, 400)
                saved = self.layouts.save(view, data.get("positions"))
                return json_response({"ok": True, "view": view, "count": len(saved)})
            if path == "/api/layout" and method in ("GET", "HEAD"):
                return json_response({"view": view, "positions": self.layouts.get(view)})
        except LayoutError as exc:
            return json_response({"error": "bad layout", "detail": str(exc)}, 400)
        return _method_not_allowed()

    def _job_key(self) -> str:
        return f"standalone:{self.store.root}"

    def start_discover(self, aws: bool = False, background: bool = True) -> dict[str, Any]:
        """Start a Discover run of this store (raises :class:`JobBusy`)."""
        store = self.store

        def work(ctx: JobContext) -> dict[str, Any]:
            store.discover(ctx, aws=aws)
            ctx.message("ok", f"Discover finished: {store.note}")
            return {"reload": True}

        return self.jobs.start(
            self._job_key(), f"Discover {store.title}", work, background=background
        )

    def _jobs_api(self, method: str, path: str, query: dict[str, str]) -> Response | None:
        if path == "/api/discover" and method == "POST":
            if self.store.readonly:
                return json_response({"error": "a recorded run is read-only"}, 405)
            try:
                job = self.start_discover(aws=query.get("aws") == "1")
            except JobBusy as exc:
                return json_response({"ok": False, "error": str(exc), "job": exc.job}, 409)
            return json_response({"ok": True, "job": job})
        if path == "/api/job" and method in ("GET", "HEAD"):
            job = (
                self.jobs.status(query["id"], self._job_key())
                if query.get("id")
                else self.jobs.current(self._job_key())
            )
            if query.get("id") and job is None:
                return _not_found(f"job {query['id']!r}")
            return json_response({"job": job, "aws": self.aws_option()})
        if path == "/api/job/cancel" and method == "POST":
            ok = self.jobs.cancel(query.get("id", ""), self._job_key())
            return json_response({"ok": ok, "job": self.jobs.status(query.get("id", ""))})
        return None

    def aws_option(self) -> dict[str, Any]:
        """Whether the AWS phase can be turned on for a run (and why not)."""
        target = self.store.aws
        if self.store.readonly:
            return {"available": False, "reason": "a recorded run is read-only"}
        if target is None:
            return {
                "available": False,
                "reason": "no AWS account configured (start with --aws-profile NAME)",
            }
        if target.problem:
            return {"available": False, "reason": f"{target.label}: {target.problem}"}
        return {"available": True, "label": target.label}

    def handle(self, method: str, target: str, body: bytes = b"") -> Response:
        url = urlsplit(target)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        handled = self._jobs_api(method, path, query)
        if handled is not None:
            return handled
        if path in ("/api/layout", "/api/layout/reset"):
            return self._layout_api(method, path, query, body)
        if method == "POST":
            if path == "/api/regenerate":
                if self.store.readonly:
                    return json_response({"error": "a recorded run is read-only"}, 405)
                return json_response(summary_view(self.store.regenerate(), self.store))
            if path == "/api/env":
                if self.store.readonly:
                    return json_response({"error": "a recorded run is read-only"}, 405)
                try:
                    if query.get("stack"):  # the environment selector of one stack
                        model = self.store.select_stack_env(query["stack"], query.get("name", ""))
                    else:
                        model = self.store.select_env(query.get("name", ""))
                except ValueError as exc:
                    return json_response({"error": "unknown environment", "detail": str(exc)}, 404)
                return json_response(environments_view(model, self.store))
            return _not_found(path) if path.startswith("/api/") else _method_not_allowed()
        if method not in ("GET", "HEAD"):
            return _method_not_allowed()
        if path in ("/", "/index.html"):
            from .web import analyzer_page  # the shared shell's template (web imports this)

            return Response(200, "text/html; charset=utf-8", analyzer_page(self.store).encode())
        if path.startswith("/static/"):
            return static_file(path[len("/static/") :])
        if not path.startswith("/api/"):
            return _not_found(path)
        try:
            return self._api(path, query)
        except ModelUnavailable as exc:
            return json_response({"error": "model unavailable", "detail": str(exc)}, 404)

    def _api(self, path: str, query: dict[str, str]) -> Response:
        model = self.store.model()
        if path == "/api/summary":
            return json_response(summary_view(model, self.store))
        if path == "/api/model":
            return json_response(public_model(model))
        if path == "/api/environments":
            return json_response(environments_view(model, self.store))
        if path == "/api/graph":
            return json_response(graph_view(model))
        if path == "/api/nodes":
            return json_response([_compact(n) for n in model["nodes"]])
        if path == "/api/edges":
            return json_response(
                [
                    {k: e[k] for k in ("id", "kind", "source", "target", "label", "via")}
                    for e in model["edges"]
                ]
            )
        if path == "/api/tree":
            return json_response(repo_tree(model, self.store.title))
        if path == "/api/overview":
            ov = confirmed_overview(build_overview(model, self.store.title, self.store.lines))
            return json_response({**ov, "aws": aws_check(model)})
        if path == "/api/flow":
            return json_response(confirmed_flows(build_flows(model)))
        if path == "/api/shapes":
            return json_response(build_shapes(model))
        if path == "/api/stack-cards":
            return json_response(stack_cards(model))
        if path == "/api/env-matrix":
            return json_response(env_matrix_table(model))
        if path == "/api/icons":
            return json_response(icons_view(model))
        if path == "/api/canvas":
            # a diagram as editable canvas elements (Architecture, Module tree, flow-<n>,
            # network / iam; with root=<dir>, that root's own)
            name, source = query.get("name", ""), model
            if "root" in query:
                if query["root"] not in {r["dir"] for r in root_entries(model)}:
                    return _not_found(f"root {query['root']!r}")
                source = root_model(model, query["root"])
            data = canvas_view(source, name, self.store.title)
            return json_response(data) if data else _not_found(f"canvas {name!r}")
        if path == "/api/root-view":
            root, view = query.get("root", ""), query.get("view", "")
            data = build_root_view(model, root, view)
            if data is None:
                return _not_found(f"root {root!r} / view {view!r}")
            return json_response(data)
        if path == "/api/diagram":
            name, fmt = query.get("name", ""), query.get("format", "svg")
            if (name.startswith("flow-") or name in TAB_DIAGRAMS) and fmt in ("svg", "drawio"):
                # one chain of the Flow view, or the Root view's Network / IAM diagram (not
                # exported: built from the model), as SVG or as an editable draw.io file;
                # with root=<dir>, that root's own
                source = model
                if "root" in query:
                    if query["root"] not in {r["dir"] for r in root_entries(model)}:
                        return _not_found(f"root {query['root']!r}")
                    source = root_model(model, query["root"])
                flow = name.startswith("flow-")
                draw = {
                    ("svg", True): chain_svg,
                    ("svg", False): tab_svg,
                    ("drawio", True): chain_drawio,
                    ("drawio", False): tab_drawio,
                }[(fmt, flow)]
                body = draw(source, name, self.store.title)
                if body is None:
                    return _not_found(
                        f"flow diagram {name!r}" if flow else f"{name} diagram: nothing to draw"
                    )
                ctype = "image/svg+xml" if fmt == "svg" else DRAWIO_MIME
                return _download(body, f"{ctype}; charset=utf-8", f"{name}.{fmt}", query)
            if name not in DIAGRAMS or fmt not in ("svg", "mmd"):
                return _not_found(f"diagram {name!r} ({fmt})")
            if fmt == "mmd":
                body = mermaid(model, name)
                return _download(body, "text/plain; charset=utf-8", f"{name}.mmd", query)
            body = svg(model, name, self.store.title)
            return _download(body, "image/svg+xml; charset=utf-8", f"{name}.svg", query)
        if path == "/api/source":
            try:
                start = int(query.get("start", "1"))
                end = int(query.get("end", str(start)))
            except ValueError:
                return json_response({"error": "start/end must be line numbers"}, 400)
            view = source_view(query.get("file", ""), start, end, self.store.lines)
            return json_response(view) if view else _not_found(f"file {query.get('file')!r}")
        if path == "/api/node":
            detail = node_detail(model, query.get("id", ""), self.store.lines)
            return json_response(detail) if detail else _not_found(f"node {query.get('id')!r}")
        if path == "/api/edge":
            detail = edge_detail(model, query.get("id", ""), self.store.lines)
            return json_response(detail) if detail else _not_found(f"edge {query.get('id')!r}")
        if path == "/api/file":
            detail = file_detail(model, query.get("path", ""))
            return json_response(detail) if detail else _not_found(f"file {query.get('path')!r}")
        return _not_found(path)


def _download(body: str, ctype: str, filename: str, query: dict[str, str]) -> Response:
    headers = {}
    if query.get("download") == "1":
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return Response(200, ctype, body.encode("utf-8"), headers)


def _method_not_allowed() -> Response:
    return json_response({"error": "method not allowed"}, 405)


def static_file(rel: str) -> Response:
    """A file from the packaged static directory (no path escapes)."""
    parts = PurePosixPath(rel).parts
    if not parts or any(p in ("..", "") or p.startswith(".") for p in parts):
        return _not_found(rel)
    path = STATIC_DIR.joinpath(*parts)
    try:
        path.resolve().relative_to(STATIC_DIR.resolve())
        body = path.read_bytes()
    except (OSError, ValueError):
        return _not_found(rel)
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "text/javascript"):
        ctype += "; charset=utf-8"
    return Response(200, ctype, body)
