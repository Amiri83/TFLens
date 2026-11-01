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


def counts(nodes: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    """``(instances, uncertain)`` per recognized resource type: the instances that would
    be created, and how many blocks among them have a count that is not known (each
    counted once)."""
    instances: dict[str, int] = {}
    uncertain: dict[str, int] = {}
    for n in nodes:
        if n["kind"] != "resource" or n["type"] not in FRIENDLY or not n["location"]:
            continue
        c = created(n)
        if c is None:
            uncertain[n["type"]] = uncertain.get(n["type"], 0) + 1
            c = 1
        instances[n["type"]] = instances.get(n["type"], 0) + c
    order = list(FRIENDLY)
    return (
        {t: instances[t] for t in sorted(instances, key=order.index)},
        {t: uncertain[t] for t in sorted(uncertain, key=order.index)},
    )


def _named(nodes: list[dict[str, Any]], rtype: str, attr: str) -> list[dict[str, Any]]:
    out = []
    for n in nodes:
        if n["kind"] == "resource" and n["type"] == rtype:
            value = (n.get("attributes") or {}).get(attr)
            out.append(
                {
                    "id": n["id"],
                    "name": value if isinstance(value, str) else n["name"],
                    "created": created(n),
                    "location": n["location"],
                }
            )
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
    }
    if env is not None:
        out = {
            "name": env["name"],
            "root": env["root"],
            "var_files": list(env["var_files"]),
            "backend_hcl": list(env.get("backend_hcl", [])),
            "var_file_cli": list(env.get("var_file_cli", [])),
            "evidence": env["evidence"][0] if env["evidence"] else None,
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
    for r in rows:
        for t, n in r["counts"].items():
            total_counts[t] = total_counts.get(t, 0) + n
    order = list(FRIENDLY)
    types = sorted(total_counts, key=order.index)
    return {
        "active": active,
        "types": [{"type": t, "label": FRIENDLY[t][1]} for t in types],
        "environments": rows,
        "total": {
            "counts": {t: total_counts[t] for t in types},
            "unknown_count": sum(r["unknown_count"] for r in rows),
            "lambda_functions": sum(r["counts"].get("aws_lambda_function", 0) for r in rows),
            "ecs_services": sum(r["counts"].get("aws_ecs_service", 0) for r in rows),
        },
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
        lines.append(_line(f"{r['id']}{mark}: {env_counts_text(r)} — {files}", r["evidence"]))
        for t, n in r["uncertain"].items():
            lines.append(
                _line(
                    f"{_plural(n, FRIENDLY[t][0], FRIENDLY[t][1])} with a count not known "
                    "statically (counted once)",
                    r["evidence"],
                    1,
                    status="unknown",
                )
            )
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


def env_counts_text(row: dict[str, Any]) -> str:
    """``1 ECS service · 4 Lambda functions · 0 unknowns`` for a row (or the total)."""
    parts = [
        f"{n} {FRIENDLY[t][0] if n == 1 else FRIENDLY[t][1]}" for t, n in row["counts"].items()
    ]
    unknown = row["unknown_count"]
    parts.append(f"{unknown} unknown{'' if unknown == 1 else 's'}")
    return " · ".join(parts)
