"""Markdown report rendered from the model (so it never disagrees with model.json)."""

from __future__ import annotations

import json
import re
from typing import Any

from .snippets import SourceLines, format_snippet, location_label, snippet_lines

TYPE_DESCRIPTIONS = {
    "aws_instance": "EC2 instance",
    "aws_lambda_function": "Lambda function",
    "aws_sqs_queue": "SQS queue",
    "aws_sns_topic": "SNS topic",
    "aws_dynamodb_table": "DynamoDB table",
    "aws_s3_bucket": "S3 bucket",
    "aws_iam_role": "IAM role",
    "aws_iam_user": "IAM user",
    "aws_iam_group": "IAM group",
    "aws_iam_policy": "IAM managed policy (customer-defined)",
    "aws_iam_instance_profile": "IAM instance profile",
    "aws_security_group": "VPC security group",
    "aws_vpc_endpoint": "VPC endpoint",
    "aws_vpc": "VPC",
    "aws_subnet": "VPC subnet",
}

_OUT_HEADINGS = (
    ("ACCESS", "IAM access"),
    ("TRIGGER", "Sends events to"),
    ("NETWORK", "Network (egress / membership / endpoints)"),
    ("RUNS_ON", "Runs on"),
    ("CONFIG", "References (configuration)"),
)
_IN_HEADINGS = (
    ("ACCESS", "IAM access granted to"),
    ("TRIGGER", "Triggered by"),
    ("NETWORK", "Network (ingress / members)"),
    ("CONFIG", "Referenced by (configuration)"),
)


def _code(s: Any) -> str:
    return f"`{str(s).replace('`', '')}`"


_loc = location_label


def _value(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict) and set(v) == {"$unknown"}:
        return f"unknown ({v['$unknown']})"
    if isinstance(v, dict) and "$ref" in v:
        target = f"{v['$ref']}.{v['attr']}" if v.get("attr") else v["$ref"]
        return f"{target} = {_value(v['value'])}" if "value" in v else f"${{{target}}}"
    if isinstance(v, dict) and "$template" in v:
        return v["$template"]
    return json.dumps(v, sort_keys=True)


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def _evidence(edge: dict[str, Any]) -> str:
    return ", ".join(_code(_loc(e)) for e in edge["evidence"])


def _edge_line(
    edge: dict[str, Any], other: str, nodes: dict[str, dict[str, Any]], refs: dict[str, int]
) -> str:
    name = nodes.get(other, {}).get("name", other)
    who = _code(other) if not other.startswith("external:") else f"{_code(name)} (external)"
    parts = [f"- {who} — {edge['label']}"]
    via = [v for v in edge["via"] if v != other]
    if via:
        parts.append(f" (via {', '.join(_code(v) for v in via)})")
    attrs = edge["attributes"]
    if attrs.get("public"):
        parts.append(" **[public]**")
    if attrs.get("conditions"):
        parts.append(" [conditional]")
    parts.append(f" — evidence {_evidence(edge)}")
    if edge["id"] in refs:
        n = refs[edge["id"]]
        parts.append(f" [E{n}](#evidence-e{n})")
    return "".join(parts)


def describe_node(node: dict[str, Any]) -> str:
    desc = TYPE_DESCRIPTIONS.get(node["type"], node["type"])
    if node["kind"] == "data":
        desc = f"data source {node['type']}"
    props = node["properties"]
    ident = next(
        (props[k] for k in ("function_name", "bucket", "name") if isinstance(props.get(k), str)),
        None,
    )
    return f"{desc} {_code(ident)}" if ident else desc


def _fence(body: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def _evidence_section(
    model: dict[str, Any], refs: dict[str, int], source: SourceLines | None
) -> list[str]:
    lines = [
        "## Evidence",
        "",
        "Source lines backing each relationship. Lines marked `>` are the evidence; "
        "unmarked lines are surrounding context.",
        "",
    ]
    for e in model["edges"]:
        if e["id"] not in refs:
            continue
        n = refs[e["id"]]
        lines += [
            f'<a id="evidence-e{n}"></a>',
            "",
            f"### E{n}: {e['kind']} {_code(e['source'])} → {_code(e['target'])}",
            "",
            e["label"],
            "",
        ]
        for ev in e["evidence"]:
            note = f" — {ev['note']}" if ev.get("note") else ""
            body = format_snippet(snippet_lines(ev, source))
            fence = _fence(body)
            lines += [f"{_code(_loc(ev))}{note}", "", f"{fence}hcl", body, fence, ""]
    return lines


def render_report(
    model: dict[str, Any],
    title: str = "Terraform repository",
    source: SourceLines | None = None,
) -> str:
    """Render report.md. ``source(file)`` returns a repository file's lines, used to
    add context around evidence snippets; without it only the evidence lines show."""
    refs: dict[str, int] = {}
    for e in model["edges"]:
        if e["evidence"]:
            refs[e["id"]] = len(refs) + 1
    nodes = {n["id"]: n for n in model["nodes"]}
    out_edges: dict[str, list[dict[str, Any]]] = {}
    in_edges: dict[str, list[dict[str, Any]]] = {}
    for e in model["edges"]:
        out_edges.setdefault(e["source"], []).append(e)
        in_edges.setdefault(e["target"], []).append(e)
    unknowns: dict[str, list[dict[str, Any]]] = {}
    for u in model["unknowns"]:
        unknowns.setdefault(u["subject"], []).append(u)

    s = model["summary"]
    lines = [f"# TFLens report: {title}", ""]
    lines += [
        "Static, read-only analysis. Values that cannot be determined from the code are "
        "reported as *unknown* with a reason; nothing is guessed.",
        "",
        "## Overview",
        "",
        f"- Terraform files: {s['files']}",
        f"- Module instances: {s['modules']} (roots: {', '.join(_code(r) for r in model['roots']) or 'none'})",
        f"- Nodes: {', '.join(f'{k} {v}' for k, v in s['nodes'].items()) or 'none'}",
        f"- Edges: {', '.join(f'{k} {v}' for k, v in s['edges'].items()) or 'none'}",
        f"- Unknown values: {s['unknowns']}",
        f"- Parse errors: {s['parse_errors']}",
        "",
    ]
    # the same section as overview.md (overview imports this module: import here)
    from .overview import render_structure_markdown, structure_lines

    lines += render_structure_markdown(structure_lines(model), level=2)
    by_service: dict[str, int] = {}
    for n in model["nodes"]:
        if n["kind"] == "resource":
            by_service[n["service"]] = by_service.get(n["service"], 0) + 1
    if by_service:
        lines += ["### Resources by service", "", "| Service | Resources |", "| --- | --- |"]
        lines += [f"| {svc} | {by_service[svc]} |" for svc in sorted(by_service)]
        lines.append("")

    lines += ["### Module tree", ""]

    def tree(m: dict[str, Any], depth: int) -> None:
        label = m["address"] or "(root)"
        where = f"dir {_code(m['dir'])}" if m["dir"] else f"source {_code(m['source'])}"
        remote = m.get("remote")
        if remote and m["analyzed"]:
            version = f" {remote['version']}" if remote["version"] else ""
            where += f", remote {_code(remote['source'])}{version} from `modules.json`"
        status = "" if m["analyzed"] else f" — not analyzed: {m['status']}"
        lines.append(
            f"{'  ' * depth}- {_code(label)} ({where}, {len(m['resources'])} blocks){status}"
        )
        for c in m["children"]:
            tree(c, depth + 1)

    for m in model["modules"]:
        tree(m, 0)
    lines.append("")

    if model["variables"]:
        lines += [
            "### Variables",
            "",
            "| Module | Variable | Value | Source |",
            "| --- | --- | --- | --- |",
        ]
        for v in model["variables"]:
            lines.append(
                f"| {_code(v['module'])} | {_code(v['name'])} | {_md_escape(_value(v['value']))} "
                f"| {_md_escape(v['source'])} |"
            )
        lines.append("")

    if model["parse_errors"]:
        lines += ["### Parse errors", ""]
        lines += [f"- {_code(e['file'])}: {e['message']}" for e in model["parse_errors"]]
        lines.append("")

    lines += ["## Resources", ""]
    primary = [n for n in model["nodes"] if n["kind"] == "resource" and n["category"] == "resource"]
    for n in primary:
        nid = n["id"]
        lines += [f"### {_code(nid)}", ""]
        lines.append(f"**What it is:** {describe_node(n)}.")
        props = {k: v for k, v in n["properties"].items()}
        if props:
            lines.append("")
            for k in sorted(props):
                lines.append(f"- {k}: {_md_escape(_value(props[k]))}")
        lines += [
            "",
            f"**Where defined:** {_code(_loc(n['location']))} (module {_code(n['module'] or '(root)')})",
            "",
        ]

        lines.append("**What it can access:**")
        lines.append("")
        found = False
        for kind, heading in _OUT_HEADINGS:
            edges = [e for e in out_edges.get(nid, []) if e["kind"] == kind]
            if edges:
                found = True
                lines.append(f"*{heading}*")
                lines += [_edge_line(e, e["target"], nodes, refs) for e in edges]
                lines.append("")
        if not found:
            lines += ["- nothing found in the code", ""]

        lines.append("**Who can access it:**")
        lines.append("")
        found = False
        for kind, heading in _IN_HEADINGS:
            edges = [e for e in in_edges.get(nid, []) if e["kind"] == kind]
            if edges:
                found = True
                lines.append(f"*{heading}*")
                lines += [_edge_line(e, e["source"], nodes, refs) for e in edges]
                lines.append("")
        exposure = []
        for e in out_edges.get(nid, []):
            if e["kind"] == "NETWORK" and e["attributes"].get("attachment"):
                for ing in in_edges.get(e["target"], []):
                    if ing["kind"] == "NETWORK" and ing["attributes"].get("direction") == "ingress":
                        exposure.append(
                            _edge_line(ing, ing["source"], nodes, refs)
                            + f" (through {_code(e['target'])})"
                        )
        if exposure:
            found = True
            lines.append("*Network reachability through attached security groups*")
            lines += exposure
            lines.append("")
        if not found:
            lines += ["- nothing found in the code", ""]

        if nid in unknowns:
            lines.append("**Unknown values:**")
            lines.append("")
            for u in unknowns[nid]:
                lines.append(
                    f"- {_code(u['attribute'])}: {u['reason']} ({_code(_loc(u['location']))})"
                )
            lines.append("")

    supporting = [
        n
        for n in model["nodes"]
        if n["kind"] in ("resource", "data") and n["category"] != "resource"
    ]
    if supporting:
        lines += [
            "## Supporting resources (relationships, policies, data sources)",
            "",
            "| Node | Type | Defined at |",
            "| --- | --- | --- |",
        ]
        for n in supporting:
            lines.append(f"| {_code(n['id'])} | {n['type']} | {_code(_loc(n['location']))} |")
        lines.append("")

    if model["unknowns"]:
        lines += [
            "## Unknown values",
            "",
            "| Subject | Attribute | Reason | Location |",
            "| --- | --- | --- | --- |",
        ]
        for u in model["unknowns"]:
            lines.append(
                f"| {_code(u['subject'])} | {_code(u['attribute'])} | {_md_escape(u['reason'])} "
                f"| {_code(_loc(u['location']))} |"
            )
        lines.append("")
    if refs:
        lines += _evidence_section(model, refs, source)
    return "\n".join(lines).rstrip() + "\n"
