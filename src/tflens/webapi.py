"""Logic behind ``tflens serve``: plain functions over the model that return dicts.

The HTTP server (``server.py``) is a thin wrapper around :meth:`Api.handle`, so
everything here can be tested without sockets. Nothing in this module makes
network calls; it reads model.json (regenerating it from the repository when it
is missing or unusable) and repository files listed in the model.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .flow import build_flows, chain_svg
from .model import EDGE_KINDS, SCHEMA_VERSION
from .output import dumps, run_analysis
from .overview import build_overview
from .report import describe_node
from .shapes import build_root_view, build_shapes, root_entries, root_model
from .snippets import SourceLines, snippet_lines
from .tabdiagrams import DIAGRAMS as TAB_DIAGRAMS
from .tabdiagrams import tab_svg
from .visuals import DIAGRAMS, mermaid, svg

STATIC_DIR = Path(__file__).parent / "static"

# Categorical palette for services (assigned in sorted service order: deterministic).
SERVICE_PALETTE = (
    "#4e79a7",
    "#f28e2b",
    "#59a14f",
    "#e15759",
    "#76b7b2",
    "#edc948",
    "#b07aa1",
    "#ff9da7",
    "#9c755f",
    "#86bcb6",
    "#d37295",
    "#a0cbe8",
)
EXTERNAL_COLOR = "#bab0ac"
META_COLOR = "#d0d7de"
KIND_COLORS = {
    "ACCESS": "#d62728",
    "TRIGGER": "#ff7f0e",
    "NETWORK": "#1f77b4",
    "CONFIG": "#7f7f7f",
    "RUNS_ON": "#9467bd",
}
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
    ) -> None:
        self.root = root.resolve()
        self.readonly = readonly
        self.title = title or self.root.name
        self.model_path = model_path or default_model_path(self.root)
        self.var_files = var_files or []
        self.cli_vars = cli_vars or {}
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
            self._set(loaded, mtime, generated=False)
            return loaded

    def regenerate(self) -> dict[str, Any]:
        if self.readonly:
            raise ModelUnavailable("a recorded run cannot be regenerated")
        with self._lock:
            model = run_analysis(self.root, self.var_files, self.cli_vars).model
            try:
                self.model_path.parent.mkdir(parents=True, exist_ok=True)
                self.model_path.write_text(dumps(model), encoding="utf-8")
                self.note = f"regenerated {self.model_path}"
            except OSError as exc:
                self.note = f"regenerated in memory only (cannot write {self.model_path}: {exc})"
            self._set(model, self._stat(), generated=True)
            return model

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


def _unanalyzed_modules(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Module calls that could not be analyzed have no graph node; describe them
    from the module tree so the UI can still show where they are and why."""
    out: dict[str, dict[str, Any]] = {}

    def walk(mods: list[dict[str, Any]], parent: str) -> None:
        for m in mods:
            if not m["analyzed"]:
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
                    "analyzed": False,
                }
            walk(m["children"], m["id"] if m["address"] else "")

    walk(model["modules"], "")
    return out


def service_colors(model: dict[str, Any]) -> dict[str, str]:
    services = sorted({_service_of(n) for n in model["nodes"] if n["service"]} - {META_SERVICE})
    colors = {s: SERVICE_PALETTE[i % len(SERVICE_PALETTE)] for i, s in enumerate(services)}
    colors[META_SERVICE] = META_COLOR
    return colors


def _group_id(module: str) -> str:
    return GROUP_PREFIX + module


def _parent_module(module: str) -> str:
    """``module.a.module.b`` -> ``module.a``; top-level modules have no parent ("")."""
    head, sep, _ = module.rpartition(".module.")
    return head if sep else ""


def module_groups(modules: set[str]) -> list[dict[str, Any]]:
    """Compound parent nodes, one per non-root module (and each enclosing module)."""
    wanted: set[str] = set()
    for m in modules:
        while m and m != ROOT_MODULE and m not in wanted:
            wanted.add(m)
            m = _parent_module(m)
    return [
        {
            "data": {
                "id": _group_id(m),
                "label": m,
                "module": m,
                "parent": _group_id(_parent_module(m)) if _parent_module(m) else None,
            },
            "classes": "group",
        }
        for m in sorted(wanted)
    ]


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
    nodes = []
    for n in model["nodes"]:
        if n["kind"] == "module":
            continue
        service = _service_of(n)
        nodes.append(
            {
                "data": {
                    "id": n["id"],
                    "label": short_name(n),
                    "short_name": short_name(n),
                    "name": n["name"],
                    "type": n["type"],
                    "kind": n["kind"],
                    "category": n["category"],
                    "service": service or "external",
                    "module": _module_of(n),
                    "group": _group_id(n["module"]) if n["module"] else None,
                    "file": n["location"]["file"] if n["location"] else "",
                    "color": colors.get(service, EXTERNAL_COLOR),
                }
            }
        )
    edges = [
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
        for e in model["edges"]
    ]
    services = sorted({d["data"]["service"] for d in nodes})
    kinds = [k for k in EDGE_KINDS if any(e["kind"] == k for e in model["edges"])]
    return {
        "nodes": nodes,
        "edges": edges,
        "groups": module_groups({d["data"]["module"] for d in nodes}),
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
    node = next((n for n in model["nodes"] if n["id"] == node_id), None)
    node = node or _unanalyzed_modules(model).get(node_id)
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
            "terragrunt.hcl files only."
            + (" Crawled (docs / CI / history): the source can be shown." if readable else ""),
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


def summary_view(model: dict[str, Any], store: ModelStore | None = None) -> dict[str, Any]:
    disc = model.get("discovery") or {}
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
    def __init__(self, store: ModelStore) -> None:
        self.store = store

    def handle(self, method: str, target: str) -> Response:
        url = urlsplit(target)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        if method == "POST":
            if path == "/api/regenerate":
                if self.store.readonly:
                    return json_response({"error": "a recorded run is read-only"}, 405)
                return json_response(summary_view(self.store.regenerate(), self.store))
            return _not_found(path) if path.startswith("/api/") else _method_not_allowed()
        if method not in ("GET", "HEAD"):
            return _method_not_allowed()
        if path in ("/", "/index.html"):
            return static_file("index.html")
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
            return json_response(model)
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
            return json_response(build_overview(model, self.store.title, self.store.lines))
        if path == "/api/flow":
            return json_response(build_flows(model))
        if path == "/api/shapes":
            return json_response(build_shapes(model))
        if path == "/api/root-view":
            root, view = query.get("root", ""), query.get("view", "")
            data = build_root_view(model, root, view)
            if data is None:
                return _not_found(f"root {root!r} / view {view!r}")
            return json_response(data)
        if path == "/api/diagram":
            name, fmt = query.get("name", ""), query.get("format", "svg")
            if name.startswith("flow-") and fmt == "svg":
                # one chain of the Flow view (not exported: it is built from the model);
                # with root=<dir>, a chain of that root's own Flow view
                source = model
                if "root" in query:
                    if query["root"] not in {r["dir"] for r in root_entries(model)}:
                        return _not_found(f"root {query['root']!r}")
                    source = root_model(model, query["root"])
                body = chain_svg(source, name, self.store.title)
                if body is None:
                    return _not_found(f"flow diagram {name!r}")
                return _download(body, "image/svg+xml; charset=utf-8", f"{name}.svg", query)
            if name in TAB_DIAGRAMS and fmt == "svg":
                # the Root view's Network / IAM diagram: the repository's, or with
                # root=<dir> that root's
                source = model
                if "root" in query:
                    if query["root"] not in {r["dir"] for r in root_entries(model)}:
                        return _not_found(f"root {query['root']!r}")
                    source = root_model(model, query["root"])
                body = tab_svg(source, name, self.store.title)
                if body is None:
                    return _not_found(f"{name} diagram: nothing to draw")
                return _download(body, "image/svg+xml; charset=utf-8", f"{name}.svg", query)
            if name not in DIAGRAMS or fmt not in ("svg", "mmd"):
                return _not_found(f"diagram {name!r} ({fmt})")
            if fmt == "mmd":
                body = mermaid(model, name)
                return _download(body, "text/plain; charset=utf-8", f"{name}.mmd", query)
            body = svg(model, name, self.store.title)
            return _download(body, "image/svg+xml; charset=utf-8", f"{name}.svg", query)
        if path == "/api/handoff":
            return _download(
                self._handoff(model), "text/markdown; charset=utf-8", "handoff.md", query
            )
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

    def _handoff(self, model: dict[str, Any]) -> str:
        """A recorded run serves the handoff.md written with it (it includes the changes
        since the previous run); otherwise it is generated from the model."""
        if self.store.readonly:
            recorded = self.store.model_path.parent / "handoff.md"
            try:
                return recorded.read_text(encoding="utf-8")
            except OSError:
                pass
        from .handoff import render_handoff

        return render_handoff(model, self.store.title, self.store.lines)


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
