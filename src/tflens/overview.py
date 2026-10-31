"""The Overview page: what a repository deploys, written out deterministically.

Built from model.json alone (so a recorded run shows it too). Every line carries the
location it is about (``file:line``), and resource / relationship lines the graph node or
edge they describe, so the UI can open the evidence. The same structure renders as
Markdown (``overview.md`` of a run, ``tflens --overview``).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .snippets import location_label

KEY_KINDS = ("ACCESS", "TRIGGER", "NETWORK")
TOP_EDGES = 5
MAX_RESOURCES_PER_SERVICE = 25
MAX_UNKNOWNS = 10


def _line(
    text: str,
    loc: dict[str, Any] | None,
    level: int = 0,
    node: str | None = None,
    edge: str | None = None,
    status: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {"text": text, "location": loc, "level": level}
    if node:
        out["node"] = node
    if edge:
        out["edge"] = edge
    if status:
        out["status"] = status  # unknown | failed | skipped: shown highlighted
    return out


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _edge_score(edge: dict[str, Any]) -> int:
    """Higher = more important (broad access, open network first); ties by edge id."""
    attrs = edge["attributes"]
    if edge["kind"] == "ACCESS":
        actions = attrs.get("actions")
        n = len(actions) if isinstance(actions, list) else 1
        wildcard = isinstance(actions, list) and any("*" in str(a) for a in actions)
        return n + (1000 if wildcard else 0)
    if edge["kind"] == "NETWORK":
        if "0.0.0.0/0" in edge["source"] or "::/0" in edge["source"]:
            return 1000
        return 100 if edge["source"].startswith("external:") else 0
    return 0


def _short(node_id: str, root: str) -> str:
    prefix = f"{root}::"
    return node_id[len(prefix) :] if node_id.startswith(prefix) else node_id


def build_overview(model: dict[str, Any], title: str = "") -> dict[str, Any]:
    structure = model.get("structure") or {"roots": [], "environments": []}
    discovery = model.get("discovery") or {}
    nodes = {n["id"]: n for n in model["nodes"]}
    failed = discovery.get("status") == "failed"
    out: dict[str, Any] = {
        "title": title,
        "status": discovery.get("status", "ok"),
        "reason": discovery.get("reason", ""),
        "summary": [],
        "roots": [],
        "environments": [],
        "discovery": _discovery_lines(model, discovery),
        "inventory": discovery.get("inventory") if failed else None,
    }
    if failed:
        out["summary"].append(_line(f"Analysis failed: {out['reason']}", None, status="failed"))
        return out
    roots = structure["roots"]
    out["summary"] = _summary(model, roots, structure["environments"], nodes)
    out["roots"] = [_root_section(model, r, nodes) for r in roots]
    out["environments"] = [
        _line(
            f"{e['name']}: {', '.join(e['kinds'])} — "
            + (f"{e['root']}/" if e["root"] != "." else "whole repository")
            + (f" with {', '.join(e['var_files'])}" if e["var_files"] else ""),
            e["evidence"][0] if e["evidence"] else None,
        )
        for e in structure["environments"]
    ]
    return out


def _first_signal(root: dict[str, Any]) -> dict[str, Any] | None:
    return next((s["location"] for s in root["signals"] if s["location"]), None)


def _summary(
    model: dict[str, Any],
    roots: list[dict[str, Any]],
    environments: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    lines = []
    tg = sum(1 for r in roots if r["kind"] == "terragrunt")
    head = f"{_plural(len(roots), 'root')} detected"
    if tg:
        head += f" ({tg} Terragrunt unit{'s' if tg != 1 else ''})"
    first = _first_signal(roots[0]) if roots else None
    lines.append(_line(head + ":", first))
    for r in roots:
        b = r["backend"]
        text = f"{r['dir']}/ — {r['kind']}, {b['type']} backend"
        if not r["analyzed"]:
            text += f"; not analyzed: {r['status']}"
        lines.append(_line(text, _first_signal(r), 1, status="" if r["analyzed"] else "unknown"))
    resources = [
        nodes[i] for r in roots for i in r["resources"] if i in nodes and nodes[i]["location"]
    ]
    by_service: dict[str, list[dict[str, Any]]] = {}
    for n in resources:
        by_service.setdefault(n["service"] or "other", []).append(n)
    if resources:
        counts = ", ".join(
            f"{s} {len(v)}"
            for s, v in sorted(by_service.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        )
        lines.append(
            _line(
                f"Deploys {_plural(len(resources), 'resource')} across "
                f"{_plural(len(by_service), 'service')}: {counts}.",
                resources[0]["location"],
            )
        )
        for service in sorted(by_service):
            types = Counter(n["type"] for n in by_service[service])
            n0 = by_service[service][0]
            lines.append(
                _line(
                    f"{service}: " + ", ".join(f"{t} ×{c}" for t, c in sorted(types.items())),
                    n0["location"],
                    1,
                    node=n0["id"],
                )
            )
    else:
        lines.append(
            _line(
                "No resources are defined in the analyzed code"
                + (": the roots deploy modules that are not in the repository" if roots else ""),
                first,
                status="unknown",
            )
        )
    calls = [m for r in roots for m in r["modules"]]
    if calls:
        kinds = Counter(m["kind"] for m in calls)
        lines.append(
            _line(
                f"{_plural(len(calls), 'module call')}: "
                + ", ".join(f"{c} {k}" for k, c in sorted(kinds.items())),
                calls[0]["location"],
            )
        )
    if environments:
        lines.append(
            _line(
                f"{_plural(len(environments), 'environment')}: "
                + ", ".join(
                    f"{e['name']}" + (f" ({e['root']})" if e["root"] != "." else "")
                    for e in environments
                ),
                environments[0]["evidence"][0] if environments[0]["evidence"] else first,
            )
        )
    edges = Counter(e["kind"] for e in model["edges"] if e["kind"] in KEY_KINDS)
    if edges:
        lines.append(
            _line(
                "Relationships: " + ", ".join(f"{k} {edges[k]}" for k in KEY_KINDS if edges[k]),
                first,
            )
        )
    if model["unknowns"]:
        u = model["unknowns"][0]
        lines.append(
            _line(
                f"{_plural(len(model['unknowns']), 'value')} cannot be known statically "
                "(see Unknowns per root).",
                u["location"],
                status="unknown",
            )
        )
    return lines


def _root_section(
    model: dict[str, Any], root: dict[str, Any], nodes: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    fallback = _first_signal(root)
    sections = [
        {
            "title": "Why this is a root",
            "lines": [_line(s["signal"], s["location"]) for s in root["signals"]],
        },
        {"title": "Backend (state)", "lines": _backend_lines(root["backend"], fallback)},
        {"title": "Terraform and providers", "lines": _provider_lines(root, fallback)},
        {"title": "Modules", "lines": _module_lines(root, fallback)},
        {"title": "Resources by service", "lines": _resource_lines(root, nodes, fallback)},
        {"title": "Key relationships", "lines": _edge_lines(model, root, fallback)},
        {"title": "Unknowns", "lines": _unknown_lines(model, root, fallback)},
    ]
    if root["environments"]:
        sections.insert(
            2,
            {
                "title": "Environments",
                "lines": [_line(", ".join(root["environments"]), fallback)],
            },
        )
    head = f"{root['dir']}/" + (" (Terragrunt unit)" if root["kind"] == "terragrunt" else "")
    return {
        "dir": root["dir"],
        "title": head,
        "analyzed": root["analyzed"],
        "status": root["status"],
        "location": fallback,
        "sections": sections,
    }


def _backend_lines(b: dict[str, Any], fallback: dict[str, Any] | None) -> list[dict[str, Any]]:
    loc = b["location"] or fallback
    lines = [_line(f"{b['type']} backend — {b['origin']}", loc)]
    if b.get("note"):
        lines.append(_line(b["note"], loc, 1))
    for s in b["settings"]:
        text = f"{s['key']} = {s['value']}"
        if not s["known"]:
            text += f" — {s.get('reason', '')}"
        elif s.get("note"):
            text += f" ({s['note']})"
        lines.append(_line(text, s["location"] or loc, 1, status="" if s["known"] else "unknown"))
    for f in b["config_files"]:
        lines.append(
            _line(
                f"partial configuration {f['file']}",
                {"file": f["file"], "start_line": 1, "end_line": 1},
                1,
            )
        )
    ws = b["workspace"]
    text = f"workspace: {ws['value']}" + (f" — {ws['note']}" if ws.get("note") else "")
    refs = ws.get("references") or []
    lines.append(_line(text, ws.get("location") or (refs[0] if refs else loc), 1))
    return lines


def _provider_lines(root: dict[str, Any], fallback: dict[str, Any] | None) -> list[dict[str, Any]]:
    lines = []
    for rv in root["required_version"]:
        lines.append(
            _line(f"Terraform {rv['value']} (required_version in {rv['module']}/)", rv["location"])
        )
    for p in root["providers"]:
        if p.get("note"):
            lines.append(_line(p["note"], p.get("location") or fallback))
            continue
        cons = [c for c in p["constraints"] if c["version"]]
        text = f"provider {p['name']}" + (f" ({p['source']})" if p["source"] else "")
        text += ": " + (
            ", ".join(f"{c['version']} in {c['module']}/" for c in cons)
            if cons
            else "no version constraint"
        )
        if p["locked"]:
            text += f"; locked at {p['locked']['version']}"
        loc = (
            cons[0]["location"] if cons else (p["locked"]["location"] if p["locked"] else fallback)
        )
        lines.append(_line(text, loc, status="" if cons else "unknown"))
    for c in root["provider_configs"]:
        text = f'provider "{c["name"]}" configured in {c["module"]}/'
        if c["alias"]:
            text += f", alias {c['alias']}"
        if c["region"]:
            text += f", region {c['region']}"
            if c["region"] == "UNKNOWN":
                text += f" ({c.get('region_reason', '')})"
        lines.append(
            _line(text, c["location"], 1, status="unknown" if c["region"] == "UNKNOWN" else "")
        )
    if not lines:
        lines.append(
            _line("No provider requirements or configurations found", fallback, status="unknown")
        )
    return lines


def _module_lines(root: dict[str, Any], fallback: dict[str, Any] | None) -> list[dict[str, Any]]:
    lines = []
    for m in root["modules"]:
        text = f"{m['address']} — {m['kind']} {m['source'] or '(no literal source)'}"
        if m["version"]:
            text += f" @ {m['version']}"
        if m["dir"]:
            text += f" → {m['dir']}/"
        if not m["analyzed"]:
            text += f"; not analyzed: {m['status']}"
        lines.append(_line(text, m["location"], status="" if m["analyzed"] else "unknown"))
        for i in m["inputs"]:
            lines.append(_line(f"{i['name']} = {i['value']}", i["location"], 1))
    if not lines:
        lines.append(_line("No module calls", fallback))
    return lines


def _resource_lines(
    root: dict[str, Any], nodes: dict[str, dict[str, Any]], fallback: dict[str, Any] | None
) -> list[dict[str, Any]]:
    by_service: dict[str, list[dict[str, Any]]] = {}
    for i in root["resources"]:
        n = nodes.get(i)
        if n is not None:
            by_service.setdefault(n["service"] or "other", []).append(n)
    lines = []
    for service in sorted(by_service):
        items = by_service[service]
        lines.append(_line(f"{service} ({len(items)})", items[0]["location"], node=items[0]["id"]))
        for n in items[:MAX_RESOURCES_PER_SERVICE]:
            props = n["properties"]
            label = props.get("name") or props.get("function_name") or props.get("bucket")
            text = _short(n["id"], root["dir"])
            if isinstance(label, str) and label and label != n["name"]:
                text += f" ({label})"
            lines.append(_line(text, n["location"], 1, node=n["id"]))
        if len(items) > MAX_RESOURCES_PER_SERVICE:
            lines.append(
                _line(f"… {len(items) - MAX_RESOURCES_PER_SERVICE} more", items[0]["location"], 1)
            )
    if not lines:
        lines.append(
            _line(
                "No resources defined here" + ("" if root["analyzed"] else f" ({root['status']})"),
                fallback,
                status="unknown",
            )
        )
    return lines


def _edge_lines(
    model: dict[str, Any], root: dict[str, Any], fallback: dict[str, Any] | None
) -> list[dict[str, Any]]:
    ids = set(root["resources"]) | set(root["data_sources"])
    lines = []
    for kind in KEY_KINDS:
        edges = [
            e
            for e in model["edges"]
            if e["kind"] == kind and (e["source"] in ids or e["target"] in ids)
        ]
        edges.sort(key=lambda e: (-_edge_score(e), e["id"]))
        for e in edges[:TOP_EDGES]:
            loc = e["evidence"][0] if e["evidence"] else fallback
            if loc is not None:
                loc = {k: loc[k] for k in ("file", "start_line", "end_line")}
            text = f"{kind}: {_short(e['source'], root['dir'])} → {_short(e['target'], root['dir'])} — {e['label']}"
            lines.append(_line(text, loc, edge=e["id"]))
        if len(edges) > TOP_EDGES:
            lines.append(
                _line(f"… {len(edges) - TOP_EDGES} more {kind} relationships", fallback, 1)
            )
    if not lines:
        lines.append(_line("No ACCESS, TRIGGER or NETWORK relationships found", fallback))
    return lines


def _unknown_lines(
    model: dict[str, Any], root: dict[str, Any], fallback: dict[str, Any] | None
) -> list[dict[str, Any]]:
    ids = set(root["resources"]) | set(root["data_sources"])
    prefix = f"{root['dir']}::"
    # one analyzed root: node ids carry no "<root>::" prefix, every unknown is this root's
    single = root["analyzed"] and len(model["roots"]) == 1
    mine = [
        u
        for u in model["unknowns"]
        if u["subject"] in ids
        or u["subject"].startswith(prefix)
        or (single and "::" not in u["subject"])
    ]
    lines = []
    if not root["analyzed"]:
        lines.append(_line(f"Not analyzed: {root['status']}", fallback, status="unknown"))
    for s in root["backend"]["settings"]:
        if not s["known"]:
            lines.append(
                _line(
                    f"backend {s['key']}: {s.get('reason', '')}",
                    s["location"] or root["backend"]["location"] or fallback,
                    status="unknown",
                )
            )
    for u in mine[:MAX_UNKNOWNS]:  # includes module calls that could not be analyzed
        lines.append(
            _line(
                f"{_short(u['subject'], root['dir'])} {u['attribute']}: {u['reason']}",
                u["location"] or fallback,
                node=u["subject"],
                status="unknown",
            )
        )
    if len(mine) > MAX_UNKNOWNS:
        lines.append(_line(f"… {len(mine) - MAX_UNKNOWNS} more unknown values", fallback))
    if not lines:
        lines.append(_line("None", fallback))
    return lines


def _discovery_lines(model: dict[str, Any], discovery: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    analyzable = discovery.get("analyzable", [])
    total = len(discovery.get("all_files", []))
    first = {"file": analyzable[0], "start_line": 1, "end_line": 1} if analyzable else None
    lines.append(
        _line(
            f"{_plural(len(analyzable), 'analyzable file')} of {_plural(total, 'file')} found",
            first,
        )
    )
    if discovery.get("truncated"):
        lines.append(
            _line("The file listing is truncated (too many files)", None, status="unknown")
        )
    for s in discovery.get("skipped", []):
        lines.append(
            _line(
                f"skipped {s['path']}/ — {s['reason']}",
                {"file": s["path"], "start_line": 0, "end_line": 0},
                1,
                status="failed" if s["kind"] == "unreadable" else "skipped",
            )
        )
    for e in model.get("parse_errors", []):
        lines.append(
            _line(
                f"parse error in {e['file']}: {e['message']}",
                {"file": e["file"], "start_line": 1, "end_line": 1},
                1,
                status="failed",
            )
        )
    return lines


# ------------------------------------------------------------------ markdown


def _md_loc(loc: dict[str, Any] | None) -> str:
    if not loc:
        return ""
    if not loc.get("start_line"):
        return f" (`{loc['file']}/`)"
    return f" (`{location_label(loc)}`)"


def _md_lines(lines: list[dict[str, Any]]) -> list[str]:
    return [f"{'  ' * ln['level']}- {ln['text']}{_md_loc(ln['location'])}" for ln in lines]


def render_overview_markdown(ov: dict[str, Any], inventory_text: str = "") -> str:
    out = [f"# Overview: {ov['title']}" if ov["title"] else "# Overview", ""]
    if ov["status"] == "failed":
        out += [f"**Analysis failed:** {ov['reason']}", ""]
        if inventory_text:
            out += ["```", inventory_text, "```", ""]
    out += ["## What this repository deploys", "", *_md_lines(ov["summary"]), ""]
    for r in ov["roots"]:
        out += [f"## Root {r['title']}", ""]
        for s in r["sections"]:
            out += [f"### {s['title']}", "", *_md_lines(s["lines"]), ""]
    if ov["environments"]:
        out += ["## Environments", "", *_md_lines(ov["environments"]), ""]
    out += ["## Discovery", "", *_md_lines(ov["discovery"]), ""]
    return "\n".join(out).rstrip() + "\n"
