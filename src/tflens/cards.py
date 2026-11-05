"""One card per stack for the Root view: its environments, its backend per environment, the
resources it declares by type, the modules it calls and the remote-state links into and
out of it. Pure functions over the model, no I/O."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .envs import node_root
from .labels import type_label
from .overview import FRIENDLY
from .visuals import backend_text, env_matrix_table, root_node_id


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
    matrix = {r["stack"]: r for r in env_matrix_table(model)["rows"]}
    links = model.get("stack_links") or []
    cards = []
    for s in stacks:
        here = set(s["roots"]) | {s["id"]}
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
                "environments": [{"name": e} for e in envs],
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
