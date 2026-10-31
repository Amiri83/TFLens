"""The Overview page: what a repository deploys, written out deterministically.

Built from model.json alone (so a recorded run shows it too). Every line carries the
location it is about (``file:line``), and resource / relationship lines the graph node or
edge they describe, so the UI can open the evidence. The same structure renders as
Markdown (``overview.md`` of a run, ``tflens --overview``).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .report import describe_node
from .snippets import SourceLines, format_snippet, location_label, snippet_lines

KEY_KINDS = ("ACCESS", "TRIGGER", "NETWORK")
TOP_EDGES = 5
MAX_RESOURCES_PER_SERVICE = 25
MAX_UNKNOWNS = 10
MAX_DRILLDOWNS = 60
MAX_DRILL_EDGES = 8
MAX_SNIPPET_LINES = 15
MAX_COMMITS_SHOWN = 20

# Resource types people name when saying what a repository deploys: (singular, plural),
# in the order they are listed. Helper resources (policies, attachments, mappings, bucket
# settings) are left out of the purpose sentence.
FRIENDLY: dict[str, tuple[str, str]] = {
    "aws_lambda_function": ("Lambda function", "Lambda functions"),
    "aws_instance": ("EC2 instance", "EC2 instances"),
    "aws_autoscaling_group": ("Auto Scaling group", "Auto Scaling groups"),
    "aws_ecs_cluster": ("ECS cluster", "ECS clusters"),
    "aws_ecs_service": ("ECS service", "ECS services"),
    "aws_eks_cluster": ("EKS cluster", "EKS clusters"),
    "aws_sfn_state_machine": ("Step Functions state machine", "Step Functions state machines"),
    "aws_api_gateway_rest_api": ("API Gateway REST API", "API Gateway REST APIs"),
    "aws_apigatewayv2_api": ("API Gateway HTTP API", "API Gateway HTTP APIs"),
    "aws_lb": ("load balancer", "load balancers"),
    "aws_cloudfront_distribution": ("CloudFront distribution", "CloudFront distributions"),
    "aws_sqs_queue": ("SQS queue", "SQS queues"),
    "aws_sns_topic": ("SNS topic", "SNS topics"),
    "aws_cloudwatch_event_rule": ("EventBridge rule", "EventBridge rules"),
    "aws_kinesis_stream": ("Kinesis stream", "Kinesis streams"),
    "aws_dynamodb_table": ("DynamoDB table", "DynamoDB tables"),
    "aws_s3_bucket": ("S3 bucket", "S3 buckets"),
    "aws_db_instance": ("RDS instance", "RDS instances"),
    "aws_rds_cluster": ("RDS cluster", "RDS clusters"),
    "aws_elasticache_cluster": ("ElastiCache cluster", "ElastiCache clusters"),
    "aws_efs_file_system": ("EFS file system", "EFS file systems"),
    "aws_ecr_repository": ("ECR repository", "ECR repositories"),
    "aws_secretsmanager_secret": ("Secrets Manager secret", "Secrets Manager secrets"),
    "aws_kms_key": ("KMS key", "KMS keys"),
    "aws_vpc": ("VPC", "VPCs"),
    "aws_subnet": ("subnet", "subnets"),
    "aws_vpc_endpoint": ("VPC endpoint", "VPC endpoints"),
    "aws_security_group": ("security group", "security groups"),
    "aws_iam_role": ("IAM role", "IAM roles"),
    "aws_iam_user": ("IAM user", "IAM users"),
}
# Supporting types: counted on their own line, not in the "deploys ..." sentence.
SUPPORTING = frozenset(
    {
        "aws_vpc",
        "aws_subnet",
        "aws_vpc_endpoint",
        "aws_security_group",
        "aws_iam_role",
        "aws_iam_user",
        "aws_kms_key",
    }
)


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


def build_overview(
    model: dict[str, Any], title: str = "", source: SourceLines | None = None
) -> dict[str, Any]:
    """The overview of a model. With ``source`` (file -> lines) the resource drill-downs
    carry their code snippets."""
    structure = model.get("structure") or {"roots": [], "environments": []}
    discovery = model.get("discovery") or {}
    crawled = model.get("crawl") or {}
    nodes = {n["id"]: n for n in model["nodes"]}
    failed = discovery.get("status") == "failed"
    out: dict[str, Any] = {
        "title": title,
        "status": discovery.get("status", "ok"),
        "reason": discovery.get("reason", ""),
        "purpose": [],
        "summary": [],
        "roots": [],
        "resources": [],
        "environments": [],
        "docs": _doc_lines(crawled),
        "ci": _ci_lines(crawled),
        "history": _history_lines(crawled),
        "discovery": _discovery_lines(model, discovery),
        "inventory": discovery.get("inventory") if failed else None,
    }
    if failed:
        out["summary"].append(_line(f"Analysis failed: {out['reason']}", None, status="failed"))
        out["purpose"] = _readme_lines(crawled)
        return out
    roots = structure["roots"]
    out["purpose"] = purpose_lines(model, nodes)
    out["summary"] = _summary(model, roots, structure["environments"], nodes)
    out["roots"] = [_root_section(model, r, nodes) for r in roots]
    out["resources"] = drilldowns(model, nodes, source)
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


def _label(n: dict[str, Any]) -> str:
    props = n["properties"]
    for key in ("function_name", "bucket", "name"):
        if isinstance(props.get(key), str) and props[key]:
            return props[key]
    return n["name"]


def _friendly(rtype: str, count: int) -> str:
    one, many = FRIENDLY[rtype]
    return f"{count} {one if count == 1 else many}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _readme_lines(crawled: dict[str, Any]) -> list[dict[str, Any]]:
    top = next((d for d in crawled.get("docs") or [] if "/" not in d["path"]), None)
    if top is None:
        return []
    lines = [_line(f"README ({top['path']}): {top['title']}", top["title_location"])]
    if top["excerpt"] and top["excerpt_location"] != top["title_location"]:
        lines.append(_line(top["excerpt"], top["excerpt_location"], 1))
    return lines


def resource_counts(model: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Recognized resource types with their nodes, in :data:`FRIENDLY` order."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for n in model["nodes"]:
        if n["kind"] == "resource" and n["type"] in FRIENDLY and n["location"]:
            by_type.setdefault(n["type"], []).append(n)
    return [(t, by_type[t]) for t in FRIENDLY if t in by_type]


def purpose_text(model: dict[str, Any]) -> str:
    """``deploys 2 Lambda functions, 1 SQS queue and 1 S3 bucket`` ('' if nothing known)."""
    main = [_friendly(t, len(ns)) for t, ns in resource_counts(model) if t not in SUPPORTING]
    return f"deploys {_join(main)}" if main else ""


def purpose_lines(model: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """A guess at what the repository is for: README + what it deploys + how."""
    crawled = model.get("crawl") or {}
    lines = _readme_lines(crawled)
    counts = resource_counts(model)
    main = [(t, ns) for t, ns in counts if t not in SUPPORTING]
    support = [(t, ns) for t, ns in counts if t in SUPPORTING]
    if main:
        text = f"Guess: this repository {purpose_text(model)}"
        if support:
            text += ", with " + _join([_friendly(t, len(ns)) for t, ns in support])
        lines.append(_line(text + ".", main[0][1][0]["location"], node=main[0][1][0]["id"]))
    elif counts:
        text = "Guess: supporting infrastructure only: " + _join(
            [_friendly(t, len(ns)) for t, ns in counts]
        )
        lines.append(_line(text + ".", counts[0][1][0]["location"]))
    for t, ns in main + support:
        names = ", ".join(_label(n) for n in ns[:5]) + (
            f", … {len(ns) - 5} more" if len(ns) > 5 else ""
        )
        lines.append(
            _line(f"{_friendly(t, len(ns))}: {names}", ns[0]["location"], 1, node=ns[0]["id"])
        )
    resources = [n for n in nodes.values() if n["kind"] == "resource" and n["location"]]
    if not resources:
        roots = (model.get("structure") or {}).get("roots", [])
        calls = [m for r in roots for m in r["modules"]]
        if calls:
            kinds = Counter(m["kind"] for m in calls)
            lines.append(
                _line(
                    f"Guess: defines no resources itself; it composes {_plural(len(calls), 'module')} ("
                    + ", ".join(f"{c} {k}" for k, c in sorted(kinds.items()))
                    + ")",
                    calls[0]["location"],
                    status="unknown",
                )
            )
            for m in calls[:10]:
                text = f"{m['address']}: {m['kind']} {m['source'] or '?'}"
                text += f" @ {m['version']}" if m["version"] else ""
                lines.append(_line(text, m["location"], 1))
        else:
            first = roots[0]["signals"][0]["location"] if roots and roots[0]["signals"] else None
            lines.append(
                _line("Guess: defines no resources and calls no modules", first, status="unknown")
            )
    ci = crawled.get("ci") or []
    if ci:
        systems: dict[str, list[str]] = {}
        for c in ci:
            label = c["name"]
            if c.get("projects"):
                label = _plural(len(c["projects"]), "project")
            systems.setdefault(c["system"], []).append(label)
        text = "Applied by CI: " + "; ".join(f"{s} ({', '.join(v)})" for s, v in systems.items())
        lines.append(_line(text, ci[0]["location"]))
    return lines


def drilldowns(
    model: dict[str, Any], nodes: dict[str, dict[str, Any]], source: SourceLines | None = None
) -> list[dict[str, Any]]:
    """Per recognized resource: what it is, triggered by, triggers, can access, accessed
    by, network, and (with ``source``) the code that defines it."""
    ins: dict[str, list[dict[str, Any]]] = {}
    outs: dict[str, list[dict[str, Any]]] = {}
    for e in model["edges"]:
        if e["kind"] in KEY_KINDS:
            outs.setdefault(e["source"], []).append(e)
            ins.setdefault(e["target"], []).append(e)
    order = {t: i for i, t in enumerate(FRIENDLY)}
    items = [n for t, ns in resource_counts(model) for n in ns]
    items.sort(key=lambda n: (order[n["type"]], n["id"]))
    out = []
    for n in items[:MAX_DRILLDOWNS]:
        what = describe_node(n).replace("`", "")
        if n["module"]:
            what += f", in {n['module']}"
        lines = [_line(f"What: {what}", n["location"], node=n["id"])]
        groups = (
            ("Triggered by", [e for e in ins.get(n["id"], []) if e["kind"] == "TRIGGER"], "source"),
            ("Triggers", [e for e in outs.get(n["id"], []) if e["kind"] == "TRIGGER"], "target"),
            ("Can access", [e for e in outs.get(n["id"], []) if e["kind"] == "ACCESS"], "target"),
            ("Accessed by", [e for e in ins.get(n["id"], []) if e["kind"] == "ACCESS"], "source"),
            ("Network", [e for e in ins.get(n["id"], []) if e["kind"] == "NETWORK"], "source"),
            ("Network to", [e for e in outs.get(n["id"], []) if e["kind"] == "NETWORK"], "target"),
        )
        related = False
        for title, edges, end in groups:
            for e in edges[:MAX_DRILL_EDGES]:
                related = True
                other = nodes.get(e[end])
                name = _label(other) if other and other["kind"] == "resource" else e[end]
                loc = e["evidence"][0] if e["evidence"] else n["location"]
                loc = {k: loc[k] for k in ("file", "start_line", "end_line")}
                lines.append(_line(f"{title}: {name} — {e['label']}", loc, 1, edge=e["id"]))
            if len(edges) > MAX_DRILL_EDGES:
                lines.append(_line(f"… {len(edges) - MAX_DRILL_EDGES} more", n["location"], 1))
        if not related:
            lines.append(_line("No TRIGGER, ACCESS or NETWORK relationships", n["location"], 1))
        snippet = []
        if source is not None:
            snippet = snippet_lines(n["location"], source, context=0)[:MAX_SNIPPET_LINES]
        out.append(
            {
                "node": n["id"],
                "title": f"{FRIENDLY[n['type']][0]} {_label(n)}",
                "location": n["location"],
                "lines": lines,
                "snippet": snippet,
            }
        )
    return out


def _doc_lines(crawled: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for d in crawled.get("docs") or []:
        lines.append(_line(f"{d['path']}: {d['title']}", d["title_location"]))
        if d["excerpt"] and d["excerpt_location"] != d["title_location"]:
            lines.append(_line(d["excerpt"], d["excerpt_location"], 1))
        for h in [h for h in d["headings"][1:] if h["level"] <= 2][:6]:
            lines.append(_line(f"§ {h['text']}", h["location"], 1))
    for n in crawled.get("notes") or []:
        lines.append(_line(f"{n['path']}: {n['reason']}", None, status="skipped"))
    return lines


def _ci_lines(crawled: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for c in crawled.get("ci") or []:
        lines.append(_line(f"{c['system']}: {c['name']} ({c['path']})", c["location"]))
        for p in c.get("projects") or []:
            text = f"project {p['name'] or p['dir']}: dir {p['dir']}, workspace {p['workspace']}"
            lines.append(_line(text, p["location"], 1))
        for cmd in c["commands"][:10]:
            lines.append(_line(cmd["text"], cmd["location"], 1))
        if len(c["commands"]) > 10:
            lines.append(_line(f"… {len(c['commands']) - 10} more commands", c["location"], 1))
        if not c["commands"] and not c.get("projects"):
            lines.append(_line("no terraform / terragrunt command found", c["location"], 1))
    for lk in crawled.get("locks") or []:
        lines.append(
            _line(
                f"provider lock file {lk['file']}",
                {"file": lk["file"], "start_line": 1, "end_line": 1},
            )
        )
        for p in lk["providers"]:
            lines.append(_line(f"{p['source']} = {p['version']}", p["location"], 1))
    return lines


def _history_lines(crawled: dict[str, Any]) -> dict[str, Any]:
    git = crawled.get("git") or {"status": "skipped", "reason": "not crawled", "commits": []}
    readable = set(crawled.get("readable") or [])
    out: dict[str, Any] = {"status": git["status"], "summary": [], "commits": [], "hot_files": []}
    if git["status"] != "ok":
        out["summary"].append(
            _line(f"Git history not read: {git['reason']}", None, status="skipped")
        )
        return out
    commits = git["commits"]

    def first_file(c: dict[str, Any]) -> dict[str, Any] | None:
        f = next((f for f in c["files"] if f in readable), None)
        return {"file": f, "start_line": 1, "end_line": 1} if f else None

    authors = ", ".join(f"{a['name']} ({a['commits']})" for a in git.get("authors", []))
    head = f" (HEAD {git['head'][:8]})" if git.get("head") else ""
    text = f"{_plural(len(commits), 'commit')} read{head}"
    if git.get("truncated"):
        text += f" (the last {len(commits)} only)"
    if authors:
        text += f"; authors: {authors}"
    out["summary"].append(_line(text, first_file(commits[0]) if commits else None))
    if git.get("reason"):
        out["summary"].append(_line(git["reason"], None, status="unknown"))
    for c in commits[:MAX_COMMITS_SHOWN]:
        line = _line(
            f"{c['short']} {c['date'][:10]} {c['author']}: {c['subject']} "
            f"({_plural(len(c['files']), 'file')})",
            first_file(c),
        )
        line["commit"] = c["sha"]
        out["commits"].append(line)
    for h in git.get("hot_files", []):
        loc = {"file": h["path"], "start_line": 1, "end_line": 1} if h["path"] in readable else None
        text = f"{h['path']}: changed in {_plural(h['commits'], 'commit')}"
        out["hot_files"].append(_line(text + ("" if h["exists"] else " (no longer present)"), loc))
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
            "lines": [_line(s["signal"], s["location"]) for s in root["signals"]]
            + (
                [_line(f"role: {root['role']} — {root['role_reason']}", fallback)]
                if root.get("role")
                else []
            ),
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


def render_overview_markdown(ov: dict[str, Any], inventory_text: str = "", level: int = 1) -> str:
    """Markdown of an overview; ``level`` is the heading level of its title (2 when the
    overview is embedded in handoff.md)."""
    h = "#" * level
    out = [f"{h} Overview: {ov['title']}" if ov["title"] else f"{h} Overview", ""]
    if ov["status"] == "failed":
        out += [f"**Analysis failed:** {ov['reason']}", ""]
        if inventory_text:
            out += ["```", inventory_text, "```", ""]
    if ov.get("purpose"):
        out += [f"{h}# Purpose (guess)", "", *_md_lines(ov["purpose"]), ""]
    out += [f"{h}# What this repository deploys", "", *_md_lines(ov["summary"]), ""]
    for r in ov["roots"]:
        out += [f"{h}# Root {r['title']}", ""]
        for s in r["sections"]:
            out += [f"{h}## {s['title']}", "", *_md_lines(s["lines"]), ""]
    if ov.get("resources"):
        out += [f"{h}# Resources", ""]
        for d in ov["resources"]:
            out += [f"{h}## {d['title']}{_md_loc(d['location'])}", "", *_md_lines(d["lines"]), ""]
            if d["snippet"]:
                out += ["```hcl", format_snippet(d["snippet"]), "```", ""]
    if ov["environments"]:
        out += [f"{h}# Environments", "", *_md_lines(ov["environments"]), ""]
    if ov.get("docs"):
        out += [f"{h}# Documentation", "", *_md_lines(ov["docs"]), ""]
    if ov.get("ci"):
        out += [f"{h}# CI / CD and lock files", "", *_md_lines(ov["ci"]), ""]
    hist = ov.get("history")
    if hist:
        out += [f"{h}# History", "", *_md_lines(hist["summary"]), ""]
        if hist["hot_files"]:
            out += [f"{h}## Most-changed files", "", *_md_lines(hist["hot_files"]), ""]
        if hist["commits"]:
            out += [f"{h}## Recent commits", "", *_md_lines(hist["commits"]), ""]
    out += [f"{h}# Discovery", "", *_md_lines(ov["discovery"]), ""]
    return "\n".join(out).rstrip() + "\n"
