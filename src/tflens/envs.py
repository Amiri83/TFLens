"""Per-environment analysis: each detected environment's tfvars applied to its root(s).

An environment is what :func:`detect.detect_environments` found (tfvars files, partial
backend configurations, ``-var-file`` / ``-backend-config`` options in CI). For each one
the repository is evaluated again with that environment's variables, so ``count`` /
``for_each`` / conditionals resolve the way ``terraform plan -var-file=<env>.tfvars``
would: only the blocks that would be created are counted (a Lambda function in a wrapper
module and its shared base module is one function, not two), and every value that is
still unknown is listed with the reason and a short category.

The result is the ``environment_analysis`` section of model.json (deterministic: sorted,
repository-relative paths): per environment the counts of the resource types people name
(:data:`overview.FRIENDLY`), the Lambda functions and ECS services, the unknowns, and a
``total`` over all environments.
"""

from __future__ import annotations

from typing import Any

from .analyze import analyze
from .engine import Engine
from .loader import RepoSource
from .model import Graph
from .overview import FRIENDLY, _line, _plural

# Each environment is a full analysis: bounded for repositories with many environments.
MAX_ENVIRONMENTS = 12
MAX_UNKNOWNS_LISTED = 500

# (category, substrings of the reason): the first match wins.
_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("conditional without default", ("conditional depends on unknown",)),
    ("missing variable", ("has no default and no value", "undeclared variable")),
    ("dynamic block", ("dynamic block", "dynamic statement")),
    ("per-instance value", ("count.index", "each.key")),
    (
        "module not analyzed",
        ("non-local source", "not fetched", "has no .tf files", "not a literal string"),
    ),
    ("known after apply", ("only known after apply", "self.*")),
    ("workspace", ("terraform.workspace",)),
    ("file / template", ("templatefile", "file ", "not rendered")),
)


def unknown_category(reason: str) -> str:
    """A short name for why a value is unknown (missing variable, conditional without
    default, dynamic block, ...)."""
    low = reason.lower()
    for category, needles in _CATEGORIES:
        if any(n in low for n in needles):
            return category
    return "other"


def created(node: dict[str, Any]) -> int | None:
    """Instances terraform would create for a node (None: its count is not known)."""
    props = node.get("properties") or {}
    return props.get("created", 1)


def env_id(env: dict[str, Any], environments: list[dict[str, Any]]) -> str:
    """The name of an environment, qualified by its directory when another directory has
    an environment of the same name (``stacks/shared:prod``)."""
    same = [e for e in environments if e["name"] == env["name"]]
    return env["name"] if len(same) <= 1 else f"{env['root']}:{env['name']}"


def find_env(environments: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """The environment with this id (or, failing that, the first with this name)."""
    for e in environments:
        if env_id(e, environments) == wanted:
            return e
    return next((e for e in environments if e["name"] == wanted), None)


def default_env(environments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The environment applied when none is asked for: ``dev`` when there is one, else
    the first detected (None when there is none), so names resolve instead of showing
    the blocks' labels."""
    return next((e for e in environments if e["name"].lower() == "dev"), None) or next(
        iter(environments), None
    )


def counts(nodes: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    """``(instances, uncertain)`` per recognized resource type: the instances known to be
    created, and how many blocks have a count that is not known. An uncertain block is
    never assumed to be 1: it is only in ``uncertain`` (shown ``4 (+3 uncertain)``)."""
    instances: dict[str, int] = {}
    uncertain: dict[str, int] = {}
    for n in nodes:
        if n["kind"] != "resource" or n["type"] not in FRIENDLY or not n["location"]:
            continue
        c = created(n)
        instances.setdefault(n["type"], 0)
        if c is None:
            uncertain[n["type"]] = uncertain.get(n["type"], 0) + 1
        else:
            instances[n["type"]] += c
    order = list(FRIENDLY)
    return (
        {t: instances[t] for t in sorted(instances, key=order.index)},
        {t: uncertain[t] for t in sorted(uncertain, key=order.index)},
    )


def count_text(known: int, uncertain: int = 0) -> str:
    """``4``, or ``4 (+3 uncertain)`` when blocks with a count not known exist."""
    return f"{known} (+{uncertain} uncertain)" if uncertain else str(known)


def why_counted(node: dict[str, Any]) -> dict[str, Any] | None:
    """The node's count / for_each explanation (``countwhy.count_why``), if it has one."""
    return (node.get("properties") or {}).get("count_why")


# -- service inventory ----------------------------------------------------------------------

# The Environments view's rows: (key, label, resource types); the optional rows are only
# listed when an environment has one.
INVENTORY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lambda", "Lambda functions", ("aws_lambda_function",)),
    ("ecs_cluster", "ECS clusters", ("aws_ecs_cluster",)),
    ("ecs_service", "ECS services", ("aws_ecs_service",)),
    ("ec2", "EC2 instances", ("aws_instance",)),
    ("alb", "Application load balancers (ALB)", ("aws_lb", "aws_alb")),
    ("nlb", "Network load balancers (NLB)", ("aws_lb", "aws_alb")),
    ("sns", "SNS topics", ("aws_sns_topic",)),
    ("sqs", "SQS queues", ("aws_sqs_queue",)),
    ("dynamodb", "DynamoDB tables", ("aws_dynamodb_table",)),
    ("eventbridge", "EventBridge rules", ("aws_cloudwatch_event_rule",)),
    ("log_group", "CloudWatch log groups", ("aws_cloudwatch_log_group",)),
    ("ecr", "ECR repositories", ("aws_ecr_repository",)),
    ("s3", "S3 buckets", ("aws_s3_bucket",)),
    ("ebs", "EBS volumes", ("aws_ebs_volume",)),
)
OPTIONAL_INVENTORY: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("gwlb", "Gateway load balancers", ("aws_lb", "aws_alb")),
    ("clb", "Classic load balancers (ELB)", ("aws_elb",)),
    ("lb_unknown", "Load balancers (type not known)", ("aws_lb", "aws_alb")),
)
_LB_KINDS = {"application": "alb", "network": "nlb", "gateway": "gwlb"}
_NAME_ATTRS = ("function_name", "name", "bucket", "family", "name_prefix", "bucket_prefix")


def inventory_key(node: dict[str, Any]) -> str | None:
    """The inventory row of a resource node (None: not listed)."""
    if node["kind"] != "resource" or not node["location"]:
        return None
    t = node["type"]
    if t in ("aws_lb", "aws_alb"):
        lb_type = (node.get("attributes") or {}).get("load_balancer_type", "application")
        return _LB_KINDS.get(lb_type, "lb_unknown") if isinstance(lb_type, str) else "lb_unknown"
    if t == "aws_elb":
        return "clb"
    for key, _, types in INVENTORY:
        if t in types:
            return key
    return None


def display_name(node: dict[str, Any]) -> str:
    """The name a resource is created with (``dev-api``), else its block label."""
    attrs = node.get("attributes") or {}
    for key in _NAME_ATTRS:
        if isinstance(attrs.get(key), str) and attrs[key]:
            return attrs[key]
    return node["name"]


def inventory(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per inventory row: ``{count, uncertain, items}``: the instances known to be
    created, the blocks whose count is not known, and each block with its name,
    file:line and why it is counted so (count expression, value, source)."""
    out = {
        key: {"count": 0, "uncertain": 0, "items": []}
        for key, _, _ in INVENTORY + OPTIONAL_INVENTORY
    }
    for n in nodes:
        key = inventory_key(n)
        if key is None:
            continue
        cell = out[key]
        c = created(n)
        if c is None:
            cell["uncertain"] += 1
        else:
            cell["count"] += c
        item: dict[str, Any] = {
            "id": n["id"],
            "type": n["type"],
            "name": display_name(n),
            "created": c,
            "location": n["location"],
        }
        why = why_counted(n)
        if why is not None:
            item["why"] = why
        cell["items"].append(item)
    for cell in out.values():
        cell["items"].sort(key=lambda i: i["id"])
    return out


def inventory_rows(columns: list[tuple[str, dict[str, dict[str, Any]]]]) -> list[dict[str, Any]]:
    """The rows of the inventory table (columns: ``(environment id, inventory)``): each
    with whether the environments differ (counts, or blocks only some of them create)
    and, per such block, the environments that create it (``only``)."""
    rows = []
    for key, label, _ in INVENTORY + OPTIONAL_INVENTORY:
        cells = {col: inv[key] for col, inv in columns}
        optional = key in {k for k, _, _ in OPTIONAL_INVENTORY}
        if optional and not any(c["count"] or c["uncertain"] for c in cells.values()):
            continue
        ids = {col: {i["id"] for i in c["items"]} for col, c in cells.items()}
        every = set.intersection(*ids.values()) if ids else set()
        only = {
            i: sorted(col for col, s in ids.items() if i in s)
            for i in sorted(set().union(*ids.values()) - every)
        }
        values = {(c["count"], c["uncertain"]) for c in cells.values()}
        rows.append(
            {
                "key": key,
                "label": label,
                "differs": len(columns) > 1 and (len(values) > 1 or bool(only)),
                "only": only,
            }
        )
    return rows


def difference_lines(analysis: dict[str, Any]) -> list[str]:
    """``S3 buckets: dev 0, intg 0, prod 1, uat 0`` / ``aws_s3_bucket.archive only in
    prod`` for each inventory row that differs between environments."""
    out = []
    envs = analysis["environments"]
    for row in analysis.get("inventory", []):
        if not row["differs"]:
            continue
        cells = [(r["id"], r["inventory"][row["key"]]) for r in envs]
        out.append(
            f"{row['label']}: "
            + ", ".join(f"{e} {count_text(c['count'], c['uncertain'])}" for e, c in cells)
        )
        for item, where in row["only"].items():
            out.append(f"  {item} only in {', '.join(where)}")
    return out


def _named(nodes: list[dict[str, Any]], rtype: str, attr: str) -> list[dict[str, Any]]:
    out = []
    for n in nodes:
        if n["kind"] == "resource" and n["type"] == rtype:
            value = (n.get("attributes") or {}).get(attr)
            entry = {
                "id": n["id"],
                "name": value if isinstance(value, str) else n["name"],
                "created": created(n),
                "location": n["location"],
            }
            if why_counted(n) is not None:
                entry["why"] = why_counted(n)
            out.append(entry)
    return sorted(out, key=lambda d: d["id"])


def unknown_entries(unknowns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**u, "category": unknown_category(u["reason"])} for u in unknowns]


def summarize(
    env: dict[str, Any] | None, nodes: list[dict[str, Any]], unknowns: list[dict[str, Any]]
) -> dict[str, Any]:
    """Counts, functions, services and unknowns of one analysis (model dicts)."""
    instances, uncertain = counts(nodes)
    entries = unknown_entries(unknowns)
    by_category: dict[str, int] = {}
    for u in entries:
        by_category[u["category"]] = by_category.get(u["category"], 0) + 1
    out: dict[str, Any] = {
        "counts": instances,
        "uncertain": uncertain,
        "lambda_functions": _named(nodes, "aws_lambda_function", "function_name"),
        "ecs_services": _named(nodes, "aws_ecs_service", "name"),
        "unknown_count": len(entries),
        "unknown_categories": {k: by_category[k] for k in sorted(by_category)},
        "unknowns": entries[:MAX_UNKNOWNS_LISTED],
        "uncertain_items": uncertain_items(nodes),
        "inventory": inventory(nodes),
    }
    if env is not None:
        out = {
            "name": env["name"],
            "root": env["root"],
            "var_files": list(env["var_files"]),
            "backend_hcl": list(env.get("backend_hcl", [])),
            "var_file_cli": list(env.get("var_file_cli", [])),
            "evidence": env["evidence"][0] if env["evidence"] else None,
            "wired": env.get("wired", True),
            "wiring": list(env.get("wiring", [])),
            "not_wired_reason": env.get("not_wired_reason"),
            "state": env.get("state"),
            **out,
        }
    return out


def _graph_dicts(graph: Graph) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = [graph.nodes[k].to_dict() for k in sorted(graph.nodes)]
    return nodes, [u.to_dict() for u in graph.unknowns]


def analyze_environment(repo: RepoSource, env: dict[str, Any]) -> tuple[Engine, Graph]:
    """The repository evaluated with one environment's tfvars, limited to its directory."""
    scope = None if env["root"] == "." else env["root"]
    engine = Engine(repo, list(env["var_files"]), None, scope)
    engine.run()
    return engine, analyze(repo, engine)


def analyze_environments(
    repo: RepoSource, environments: list[dict[str, Any]], active: str | None = None
) -> dict[str, Any] | None:
    """The ``environment_analysis`` section (None when no environment was detected)."""
    if not environments:
        return None
    rows = []
    for env in environments[:MAX_ENVIRONMENTS]:
        _, graph = analyze_environment(repo, env)
        nodes, unknowns = _graph_dicts(graph)
        row = summarize(env, nodes, unknowns)
        row["id"] = env_id(env, environments)
        row["not_created"] = len(graph.not_created)
        rows.append(row)
    total_counts: dict[str, int] = {}
    total_uncertain: dict[str, int] = {}
    for r in rows:
        for t, n in r["counts"].items():
            total_counts[t] = total_counts.get(t, 0) + n
        for t, n in r["uncertain"].items():
            total_uncertain[t] = total_uncertain.get(t, 0) + n
    order = list(FRIENDLY)
    types = sorted(total_counts, key=order.index)
    return {
        "active": active,
        "types": [{"type": t, "label": FRIENDLY[t][1]} for t in types],
        "environments": rows,
        "inventory": inventory_rows([(r["id"], r["inventory"]) for r in rows]),
        "total": {
            "counts": {t: total_counts[t] for t in types},
            "uncertain": {t: total_uncertain[t] for t in types if t in total_uncertain},
            "unknown_count": sum(r["unknown_count"] for r in rows),
            "lambda_functions": sum(r["counts"].get("aws_lambda_function", 0) for r in rows),
            "ecs_services": sum(r["counts"].get("aws_ecs_service", 0) for r in rows),
        },
        "not_wired": [r["id"] for r in rows if not r["wired"]],
        "skipped": [e["name"] for e in environments[MAX_ENVIRONMENTS:]],
    }


def env_count_lines(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Overview lines: the counts per environment (its tfvars applied), the total, and
    each environment's remaining unknowns with the reason."""
    analysis = model.get("environment_analysis")
    if not analysis or not analysis["environments"]:
        return []
    active = (model.get("environment") or {}).get("id")
    lines = []
    for r in analysis["environments"]:
        files = ", ".join(r["backend_hcl"] + r["var_files"]) or "no files"
        mark = " (shown)" if r["id"] == active else ""
        if not r.get("wired", True):
            mark += " (not wired)"
        lines.append(_line(f"{r['id']}{mark}: {env_counts_text(r)} — {files}", r["evidence"]))
        lines += uncertain_lines(r)
        for u in r["unknowns"]:
            lines.append(
                _line(
                    f"unknown {u['subject']}.{u['attribute']} ({u['category']}): {u['reason']}",
                    u["location"],
                    1,
                    node=u["subject"],
                    status="unknown",
                )
            )
        if r["unknown_count"] > len(r["unknowns"]):
            more = r["unknown_count"] - len(r["unknowns"])
            lines.append(_line(f"… {more} more unknowns", r["evidence"], 1, status="unknown"))
    lines.append(_line(f"total: {env_counts_text(analysis['total'])}", None))
    diffs = difference_lines(analysis)
    if diffs:
        lines.append(_line("differences between environments:", None))
        lines += [_line(d.strip(), None, 2 if d.startswith(" ") else 1) for d in diffs]
    if analysis["skipped"]:
        lines.append(
            _line(
                f"not analyzed per environment (more than {MAX_ENVIRONMENTS}): "
                + ", ".join(analysis["skipped"]),
                None,
                status="skipped",
            )
        )
    return lines


def uncertain_lines(row: dict[str, Any], level: int = 1) -> list[dict[str, Any]]:
    """Per type with blocks whose count is not known: how many (not counted), and per
    block why (count expression, value, source variable / local / default)."""
    lines = []
    for t, n in row["uncertain"].items():
        lines.append(
            _line(
                f"+{_plural(n, FRIENDLY[t][0], FRIENDLY[t][1])} uncertain: count not known "
                "statically (not counted)",
                row.get("evidence"),
                level,
                status="unknown",
            )
        )
        for item in row.get("uncertain_items", []):
            if item["type"] != t:
                continue
            why = item.get("why")
            steps = (why or {}).get("steps", [])
            step = next((s for s in steps if s["instances"] is None), None)
            lines.append(
                _line(
                    "why counted: " + (why["text"] if why else f"{item['id']}: count not known"),
                    step["location"] if step else item["location"],
                    level + 1,
                    node=item["id"],
                    status="unknown",
                )
            )
    return lines


def uncertain_items(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recognized resources whose count is not known, with why (sorted by id)."""
    out = []
    for n in nodes:
        if n["kind"] != "resource" or n["type"] not in FRIENDLY or not n["location"]:
            continue
        if created(n) is None:
            item = {"id": n["id"], "type": n["type"], "location": n["location"]}
            if why_counted(n) is not None:
                item["why"] = why_counted(n)
            out.append(item)
    return sorted(out, key=lambda i: i["id"])


def env_counts_text(row: dict[str, Any]) -> str:
    """``1 ECS service · 4 (+3 uncertain) Lambda functions · 0 unknowns`` for a row (or
    the total)."""
    uncertain = row.get("uncertain") or {}
    parts = []
    for t, n in row["counts"].items():
        u = uncertain.get(t, 0)
        label = FRIENDLY[t][0] if n == 1 and not u else FRIENDLY[t][1]
        parts.append(f"{count_text(n, u)} {label}")
    unknown = row["unknown_count"]
    parts.append(f"{unknown} unknown{'' if unknown == 1 else 's'}")
    return " · ".join(parts)
