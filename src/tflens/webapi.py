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

from .model import EDGE_KINDS, SCHEMA_VERSION
from .output import dumps, run_analysis
from .report import describe_node
from .snippets import SourceLines, snippet_lines

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
KIND_COLORS = {
    "ACCESS": "#d62728",
    "TRIGGER": "#ff7f0e",
    "NETWORK": "#1f77b4",
    "CONFIG": "#7f7f7f",
    "RUNS_ON": "#9467bd",
}
ROOT_MODULE = "(root)"


def default_model_path(root: Path) -> Path:
    return root / "tflens-out" / "model.json"


class ModelStore:
    """Holds the current model; re-reads model.json when it changes on disk and
    regenerates it from the repository when it is missing or unusable."""

    def __init__(
        self,
        root: Path,
        model_path: Path | None = None,
        var_files: list[str] | None = None,
        cli_vars: dict[str, str] | None = None,
    ) -> None:
        self.root = root.resolve()
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
                return self.regenerate()
            self._set(loaded, mtime, generated=False)
            return loaded

    def regenerate(self) -> dict[str, Any]:
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
            if file not in self.model()["files"]:
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


def _compact(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node["id"],
        "name": node["name"],
        "type": node["type"],
        "kind": node["kind"],
        "service": node["service"],
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
    services = sorted({n["service"] for n in model["nodes"] if n["service"]})
    return {s: SERVICE_PALETTE[i % len(SERVICE_PALETTE)] for i, s in enumerate(services)}


def graph_view(model: dict[str, Any]) -> dict[str, Any]:
    """Cytoscape elements (module call nodes are left to the repo view)."""
    colors = service_colors(model)
    nodes = []
    for n in model["nodes"]:
        if n["kind"] == "module":
            continue
        nodes.append(
            {
                "data": {
                    "id": n["id"],
                    "label": n["name"] if n["kind"] == "external" else n["id"],
                    "name": n["name"],
                    "type": n["type"],
                    "kind": n["kind"],
                    "category": n["category"],
                    "service": n["service"] or "external",
                    "module": _module_of(n),
                    "file": n["location"]["file"] if n["location"] else "",
                    "color": colors.get(n["service"], EXTERNAL_COLOR),
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
                "color": KIND_COLORS.get(e["kind"], "#000000"),
            }
        }
        for e in model["edges"]
    ]
    services = sorted({d["data"]["service"] for d in nodes})
    return {
        "nodes": nodes,
        "edges": edges,
        "filters": {
            "kinds": [k for k in EDGE_KINDS if any(e["kind"] == k for e in model["edges"])],
            "modules": sorted({d["data"]["module"] for d in nodes}),
            "services": services,
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
        "summary": {
            "description": description,
            "kind": node["kind"],
            "type": node["type"],
            "service": node["service"],
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


def repo_tree(model: dict[str, Any], title: str = "") -> dict[str, Any]:
    """Directory/file tree of the analyzed files, with module addresses per directory."""
    by_dir = _modules_by_dir(model)
    counts: dict[str, int] = {}
    for n in model["nodes"]:
        if n["location"] and n["kind"] in ("resource", "data"):
            counts[n["location"]["file"]] = counts.get(n["location"]["file"], 0) + 1
    root: dict[str, Any] = {
        "name": title or ".",
        "path": ".",
        "type": "dir",
        "modules": by_dir.get(".", []),
        "children": [],
    }
    dirs = {".": root}
    for f in sorted(model["files"]):
        parts = PurePosixPath(f).parts
        parent = root
        for i in range(len(parts) - 1):
            path = "/".join(parts[: i + 1])
            if path not in dirs:
                d = {
                    "name": parts[i],
                    "path": path,
                    "type": "dir",
                    "modules": by_dir.get(path, []),
                    "children": [],
                }
                dirs[path] = d
                parent["children"].append(d)
            parent = dirs[path]
        parent["children"].append(
            {"name": parts[-1], "path": f, "type": "file", "resources": counts.get(f, 0)}
        )
    _sort_tree(root)
    return root


def _sort_tree(d: dict[str, Any]) -> None:
    d["children"].sort(key=lambda c: (c["type"] != "dir", c["name"]))
    for c in d["children"]:
        if c["type"] == "dir":
            _sort_tree(c)


def file_detail(model: dict[str, Any], path: str) -> dict[str, Any] | None:
    if path not in model["files"]:
        return None
    parent = str(PurePosixPath(path).parent)
    return {
        "path": path,
        "modules": _modules_by_dir(model).get(parent, []),
        "resources": _defined_in(model, path),
        "unknowns": [
            u for u in model["unknowns"] if u["location"] and u["location"]["file"] == path
        ],
    }


def summary_view(model: dict[str, Any], store: ModelStore | None = None) -> dict[str, Any]:
    out = {
        "title": store.root.name if store else "",
        "summary": model["summary"],
        "roots": model["roots"],
        "parse_errors": model["parse_errors"],
        "remote_modules": model.get("remote_modules", []),
        "tool": model.get("tool"),
    }
    if store is not None:
        out["model_path"] = str(store.model_path)
        out["generated"] = store.generated
        out["note"] = store.note
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
        return self._api(path, query)

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
            return json_response(repo_tree(model, self.store.root.name))
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
