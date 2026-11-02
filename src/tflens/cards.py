"""One card per stack for the Root view: what it is for (only from its own README, and
only when the stack is confirmed), its environments, its backend per environment, the
resources it declares by type, the modules it calls and the remote-state links into and
out of it. Pure functions over the model, no I/O."""

from __future__ import annotations

import posixpath
from collections import Counter
from typing import Any

from .envs import node_root
from .labels import type_label
from .overview import FRIENDLY
from .visuals import backend_text, env_matrix_table, root_node_id


def _readme(model: dict[str, Any], roots: list[str]) -> dict[str, Any] | None:
    """The README in a stack's own directory (never the repository's top-level one)."""
    for d in (model.get("crawl") or {}).get("docs") or []:
        base = posixpath.basename(d["path"]).lower()
        if base.startswith("readme") and posixpath.dirname(d["path"]) in roots:
            return d
    return None


def _purpose(model: dict[str, Any], stack: dict[str, Any], status: str) -> dict[str, Any]:
    doc = _readme(model, stack["roots"])
    if doc is None:
        return {"text": None, "note": "no README in the stack's directory"}
    if status != "confirmed":
        return {"text": None, "note": f"stack {status}: its README is not used"}
    text = doc.get("excerpt") or doc.get("title") or ""
    if not text:
        return {"text": None, "note": f"{doc['path']} has no prose to use"}
    loc = doc.get("excerpt_location") if doc.get("excerpt") else doc.get("title_location")
    return {"text": text, "location": loc, "source": doc["path"], "status": "confirmed"}


def _link(lk: dict[str, Any]) -> dict[str, Any]:
    return {
        "data_source": lk["data_source"],
        "from_stack": lk.get("from_stack"),
        "to_stack": lk.get("to_stack"),
        "env": lk.get("env"),
        "backend": lk.get("backend"),
        "status": lk.get("status"),
        "location": lk.get("location"),
    }


def stack_cards(model: dict[str, Any]) -> list[dict[str, Any]]:
    structure = model.get("structure") or {"roots": [], "environments": []}
    roots_by_dir = {r["dir"]: r for r in structure.get("roots") or []}
    all_roots = list(model.get("roots") or [])
    stacks = [s for s in model.get("stacks") or [] if not s.get("library")]
    if not stacks:
        stacks = [
            {"id": d, "roots": [d], "layout": ""}
            for d, r in roots_by_dir.items()
            if r.get("role") != "library"
        ]
    validation = (model.get("pipeline") or {}).get("validation") or {}
    stack_status = {v["name"]: v["status"] for v in validation.get("stacks") or []}
    env_status = {v["name"]: v["status"] for v in validation.get("envs") or []}
    matrix = {r["stack"]: r for r in env_matrix_table(model)["rows"]}
    links = model.get("stack_links") or []
    cards = []
    for s in stacks:
        here = set(s["roots"]) | {s["id"]}
        status = stack_status.get(s["id"], "confirmed")
        by_type: Counter[str] = Counter(
            n["type"]
            for n in model["nodes"]
            if n["kind"] == "resource" and node_root(n["id"], all_roots) in here
        )
        envs = sorted({e["name"] for e in structure.get("environments") or [] if e["root"] in here})
        row = matrix.get(s["id"]) or {"cells": {}, "default": None}
        modules = []
        for d in s["roots"]:
            for m in (roots_by_dir.get(d) or {}).get("modules") or []:
                modules.append(
                    {
                        "address": m["address"],
                        "source": m.get("source") or "(no literal source)",
                        "version": m.get("version") or "",
                        "kind": m.get("kind", ""),
                        "analyzed": m.get("analyzed", True),
                        "node": (f"{d}::{m['address']}" if len(all_roots) > 1 else m["address"]),
                        "location": m.get("location"),
                    }
                )
        root = roots_by_dir.get(s["roots"][0]) if s["roots"] else None
        cards.append(
            {
                "id": s["id"],
                "roots": s["roots"],
                "node": root_node_id(s["roots"][0]) if s["roots"] else None,
                "layout": s.get("layout", ""),
                "status": status,
                "purpose": _purpose(model, s, status),
                "environments": [
                    {"name": e, "status": env_status.get(e, "confirmed")} for e in envs
                ],
                "backend": {
                    "text": backend_text(root["backend"]) if root else "",
                    "location": root["backend"].get("location") if root else None,
                    "per_env": row["cells"],
                },
                "resources": [
                    {
                        "type": t,
                        "label": FRIENDLY[t][0] if t in FRIENDLY else type_label(t),
                        "count": c,
                    }
                    for t, c in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
                ],
                "resource_total": sum(by_type.values()),
                "modules": modules,
                "outbound": [_link(lk) for lk in links if lk.get("from_stack") in here],
                "inbound": [_link(lk) for lk in links if lk.get("to_stack") in here],
            }
        )
    return cards
