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

import posixpath
from typing import Any

from .analyze import analyze
from .detect import env_segment
from .engine import Engine, in_scope
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


def _layout_env(env: dict[str, Any]) -> bool:
    """An environment that is its own directory (envs/dev, live/prod/...), not tfvars
    applied to a root."""
    seg = env_segment(env["root"])
    return (
        bool({"dir", "terragrunt"} & set(env["kinds"]))
        and seg is not None
        and (seg[1] == env["root"])
    )


def env_roots(env: dict[str, Any], roots: list[str]) -> list[str]:
    """The roots an environment applies to: its own, else every root under its directory."""
    if env["roots"]:
        return list(env["roots"])
    return [r for r in roots if env["root"] == "." or in_scope(r, env["root"])]


def stacks(environments: list[dict[str, Any]], roots: list[str]) -> list[dict[str, Any]]:
    """The repository's stacks, each with its root(s) and its environments (ids).

    A stack is a root deployed on its own: every root is one, wherever it is. Its
    environments are the tfvars / backend configurations bound to it (the nearest root
    above them). Environments that are directories of their own (``envs/dev``,
    ``envs/prod``) are one stack, ``envs/*``, with one root per environment."""
    out: dict[str, dict[str, Any]] = {}
    claimed: dict[str, str] = {}
    for e in environments:
        layout = _layout_env(e)
        if layout:
            parent = posixpath.dirname(e["root"])
            sid = f"{parent}/*" if parent else "*"
        else:
            sid = e["root"]
        stack = out.setdefault(
            sid,
            {
                "id": sid,
                "layout": "environment directories" if layout else "tfvars per environment",
                "roots": [],
                "environments": [],
                "env_roots": {},
            },
        )
        eid = env_id(e, environments)
        mine = [r for r in env_roots(e, roots) if claimed.setdefault(r, sid) == sid]
        stack["environments"].append(eid)
        stack["env_roots"][eid] = mine
        stack["roots"] = sorted(set(stack["roots"]) | set(mine))
    for r in roots:
        if r not in claimed:
            out[r] = {
                "id": r,
                "layout": "no environment",
                "roots": [r],
                "environments": [],
                "env_roots": {},
            }
    return [out[k] for k in sorted(out, key=lambda s: (s == ".", s))]


def select_stacks(
    stack_list: list[dict[str, Any]],
    environments: list[dict[str, Any]],
    choices: dict[str, str],
) -> dict[str, Any]:
    """Apply one environment per stack: ``choices`` (stack id -> environment id, '' for
    none), else the stack's default (dev, else its first). Returns the stacks with their
    ``selected`` environment and the engine settings: each selected environment's tfvars
    for its roots, and the roots of the stacks' other environments (not analyzed)."""
    by_id = {env_id(e, environments): e for e in environments}
    root_var_files: dict[str, list[str]] = {}
    root_envs: dict[str, str] = {}
    exclude: set[str] = set()
    out = []
    for s in stack_list:
        envs = [by_id[i] for i in s["environments"] if i in by_id]
        if s["id"] in choices:
            chosen = by_id.get(choices[s["id"]]) if choices[s["id"]] else None
            if choices[s["id"]] and (chosen is None or chosen not in envs):
                known = ", ".join(s["environments"]) or "none"
                raise ValueError(
                    f"unknown environment {choices[s['id']]!r} for stack {s['id']} "
                    f"(detected: {known})"
                )
            default = False
        else:
            chosen = default_env(envs)
            default = True
        sel = env_id(chosen, environments) if chosen is not None else None
        for eid, rs in s["env_roots"].items():
            for r in rs:
                if eid == sel:
                    root_var_files[r] = list(chosen["var_files"]) if chosen else []
                    root_envs[r] = chosen["name"] if chosen else ""
                elif (
                    sel is not None
                    and s["layout"] == "environment directories"
                    and r not in s["env_roots"].get(sel, [])
                ):
                    exclude.add(r)
        out.append({**s, "selected": sel, "default": default and sel is not None})
    exclude -= set(root_var_files)
    return {
        "stacks": out,
        "root_var_files": root_var_files,
        "root_envs": root_envs,
        "exclude_roots": exclude,
    }


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


def node_root(node_id: str, roots: list[str]) -> str | None:
    """The root a node (or unknown subject) of an analysis belongs to: its ``<root>::``
    prefix, else the only root analyzed (None: not known)."""
    if "::" in node_id:
        return node_id.split("::", 1)[0]
    return roots[0] if len(roots) == 1 else None


def _module_calls(node: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The module calls a resource is created through, outermost first, with their
    file:line: part of the resource's item, not items of their own."""
    out = []
    parent = by_id.get(node.get("parent") or "")
    while parent is not None and parent["kind"] == "module" and len(out) < 32:
        out.append({"id": parent["id"], "location": parent["location"]})
        parent = by_id.get(parent.get("parent") or "")
    return out[::-1]


def inventory(
    nodes: list[dict[str, Any]],
    env_name: str | None = None,
    roots: list[str] | None = None,
    stack_of: dict[str, str] | None = None,
    qualify: bool = False,
) -> dict[str, dict[str, Any]]:
    """Per inventory row: ``{count, uncertain, items}``: the instances known to be
    created, the blocks whose count is not known, and each block with its name,
    file:line and why it is counted so (count expression, value, source).

    Only resource blocks are counted: a module call is not an item of its own, so a
    module call and the resources inside it are one item. With ``roots`` (the roots
    analyzed) each item names its root, its stack (``stack_of``: root -> stack id) and
    ``env_name``; ``qualify`` prefixes ids with ``<root>::`` when the analysis did not (one
    root analyzed in a repository with several), so ids compare across analyses."""
    out = {
        key: {"count": 0, "uncertain": 0, "items": []}
        for key, _, _ in INVENTORY + OPTIONAL_INVENTORY
    }
    by_id = {n["id"]: n for n in nodes}
    seen: set[str] = set()
    for n in nodes:
        key = inventory_key(n)
        if key is None or n["id"] in seen:
            continue
        seen.add(n["id"])
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
        if roots is not None:
            root = node_root(n["id"], roots)
            if qualify and root is not None and "::" not in n["id"]:
                item["id"] = f"{root}::{n['id']}"
            item["root"] = root
            item["stack"] = (stack_of or {}).get(root or "", root)
            item["env"] = env_name
        via = _module_calls(n, by_id)
        if via:
            item["via"] = via
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
    """``S3 buckets: dev 0, intg 0, uat 0, prod 1`` / ``aws_s3_bucket.archive only in
    prod`` for each inventory row that differs between environments (the columns: one
    per environment name; older models: one per environment row)."""
    out = []
    envs = analysis.get("columns") or analysis["environments"]
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
    env: dict[str, Any] | None,
    nodes: list[dict[str, Any]],
    unknowns: list[dict[str, Any]],
    **where: Any,
) -> dict[str, Any]:
    """Counts, functions, services and unknowns of one analysis (model dicts); ``where``
    is passed to :func:`inventory` (env_name, roots, stack_of, qualify)."""
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
        "inventory": inventory(nodes, **where),
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


def analyze_roots(
    repo: RepoSource, root_var_files: dict[str, list[str]], env: str = ""
) -> tuple[Engine, Graph]:
    """These roots only (every other root left out), each with its own tfvars: one
    environment applied to every stack that has it."""
    probe = Engine(repo)
    probe.discover_roots()
    every = set(probe.root_modules) | set(probe.units)
    engine = Engine(repo, root_var_files=root_var_files, exclude_roots=every - set(root_var_files))
    engine.root_envs = {r: env for r in root_var_files} if env else {}
    engine.run()
    return engine, analyze(repo, engine)


# Columns ordered the way environments are promoted (dev before uat before prod); names
# not listed here follow, alphabetically.
_PROMOTION = (
    "sandbox", "sbx", "dev", "develop", "development", "test", "testing", "qa", "int",
    "intg", "integ", "integration", "sit", "e2e", "uat", "perf", "nonprod", "non-prod",
    "demo", "stage", "staging", "stg", "preprod", "pre-prod", "prod", "production", "prd",
    "dr",
)  # fmt: skip
NO_ENV_COLUMN = "(no environment)"


def column_order(name: str) -> tuple[int, str]:
    low = name.lower()
    return (_PROMOTION.index(low) if low in _PROMOTION else len(_PROMOTION), low)


def env_columns(
    environments: list[dict[str, Any]], stack_list: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One column per environment name (``dev | intg | uat | prod``): the stacks that
    have an environment of that name, each root with that environment's tfvars, and the
    stacks that have environments but none of that name (``missing``)."""
    by_id = {env_id(e, environments): e for e in environments}
    cols: dict[str, dict[str, Any]] = {}
    for s in stack_list:
        for eid in s["environments"]:
            e = by_id.get(eid)
            if e is None:
                continue
            col = cols.setdefault(
                e["name"],
                {"id": e["name"], "envs": [], "stacks": [], "root_var_files": {}},
            )
            col["envs"].append(eid)
            if s["id"] not in col["stacks"]:
                col["stacks"].append(s["id"])
            for r in s["env_roots"].get(eid, []):
                col["root_var_files"][r] = list(e["var_files"])
    with_envs = [s["id"] for s in stack_list if s["environments"]]
    out = []
    for name in sorted(cols, key=column_order):
        col = cols[name]
        col["missing"] = [s for s in with_envs if s not in col["stacks"]]
        out.append(col)
    return out


def _rows_of(items: list[dict[str, Any]], key: str, roots: list[str], mine: set[str]):
    return [x for x in items if node_root(x[key], roots) in mine]


def analyze_environments(
    repo: RepoSource,
    environments: list[dict[str, Any]],
    active: str | None = None,
    stack_list: list[dict[str, Any]] | None = None,
    library_roots: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """The ``environment_analysis`` section (None when no environment was detected).

    ``columns``: one per environment name, every stack that has it analyzed together with
    its own tfvars (``dev`` = the dev of every stack). Each real resource is counted once:
    a stack's roots belong to one column per environment, a module call and the
    resources inside it are one item, and reusable modules nothing calls
    (``library_roots``) are not deployed from here. Stacks with no environment have
    their own column (variable defaults). ``environments``: one row per stack's
    environment, its part of the column. ``total`` sums the columns and is never shown
    by default."""
    if not environments:
        return None
    probe = Engine(repo)
    probe.discover_roots()
    all_roots = sorted(set(probe.root_modules) | set(probe.units))
    if stack_list is None:
        stack_list = stacks(environments, all_roots)
    stack_of_env = {e: s["id"] for s in stack_list for e in s["environments"]}
    stack_of_root = {r: s["id"] for s in stack_list for r in s["roots"]}
    qualify = len(all_roots) > 1
    columns_all = env_columns(environments, stack_list)
    columns_run = columns_all[:MAX_ENVIRONMENTS]
    unbound = sorted(
        r for s in stack_list if not s["environments"] for r in s["roots"] if r not in library_roots
    )
    if unbound:
        columns_run.append(
            {
                "id": NO_ENV_COLUMN,
                "envs": [],
                "stacks": sorted({stack_of_root.get(r, r) for r in unbound}),
                "root_var_files": {r: [] for r in unbound},
                "missing": [],
                "no_environment": True,
            }
        )
    by_id = {env_id(e, environments): e for e in environments}
    row_by_id: dict[str, dict[str, Any]] = {}
    columns = []
    for col in columns_run:
        engine, graph = analyze_roots(
            repo, col["root_var_files"], "" if col.get("no_environment") else col["id"]
        )
        roots = list(engine.roots)
        nodes, unknowns = _graph_dicts(graph)
        where = {"roots": roots, "stack_of": stack_of_root, "qualify": qualify}
        env_name = None if col.get("no_environment") else col["id"]
        summary = summarize(None, nodes, unknowns, env_name=env_name, **where)
        for eid in col["envs"]:
            env = by_id[eid]
            stack = stack_of_env.get(eid)
            mine = {r for s in stack_list if s["id"] == stack for r in s["env_roots"].get(eid, [])}
            row = summarize(
                env,
                _rows_of(nodes, "id", roots, mine),
                _rows_of(unknowns, "subject", roots, mine),
                env_name=env["name"],
                **where,
            )
            row["id"] = eid
            row["stack"] = stack
            row["column"] = col["id"]
            row["roots"] = sorted(mine)
            row["not_created"] = len(_rows_of(graph.not_created, "subject", roots, mine))
            row_by_id[eid] = row
        rows_here = [row_by_id[e] for e in col["envs"]]
        columns.append(
            {
                "id": col["id"],
                "environments": list(col["envs"]),
                "stacks": list(col["stacks"]),
                "missing": list(col["missing"]),
                "roots": roots,
                "no_environment": bool(col.get("no_environment")),
                "wired": any(r["wired"] for r in rows_here) if rows_here else True,
                "not_created": len(graph.not_created),
                **summary,
            }
        )
    rows = [row_by_id[i] for i in (env_id(e, environments) for e in environments) if i in row_by_id]
    total_counts: dict[str, int] = {}
    total_uncertain: dict[str, int] = {}
    for c in columns:
        for t, n in c["counts"].items():
            total_counts[t] = total_counts.get(t, 0) + n
        for t, n in c["uncertain"].items():
            total_uncertain[t] = total_uncertain.get(t, 0) + n
    order = list(FRIENDLY)
    types = sorted(total_counts, key=order.index)
    selected = [s["selected"] for s in stack_list if s.get("selected")]
    return {
        "active": active,
        "selected": selected or ([active] if active else []),
        "types": [{"type": t, "label": FRIENDLY[t][1]} for t in types],
        "columns": columns,
        "environments": rows,
        "inventory": inventory_rows([(c["id"], c["inventory"]) for c in columns]),
        "library_roots": sorted(library_roots),
        # optional: the sum over the columns (never part of the default views)
        "total": {
            "counts": {t: total_counts[t] for t in types},
            "uncertain": {t: total_uncertain[t] for t in types if t in total_uncertain},
            "unknown_count": sum(c["unknown_count"] for c in columns),
            "lambda_functions": sum(c["counts"].get("aws_lambda_function", 0) for c in columns),
            "ecs_services": sum(c["counts"].get("aws_ecs_service", 0) for c in columns),
        },
        "not_wired": [r["id"] for r in rows if not r["wired"]],
        "skipped": [c["id"] for c in columns_all[MAX_ENVIRONMENTS:]],
    }


def env_count_lines(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Overview lines: the counts per environment (one line per environment name, every
    stack with its own tfvars; never a sum across environments), per stack when there
    are several, and each environment's remaining unknowns with the reason."""
    analysis = model.get("environment_analysis")
    if not analysis or not analysis["environments"]:
        return []
    active = (model.get("environment") or {}).get("id")
    selected = set(analysis.get("selected") or [])
    rows = {r["id"]: r for r in analysis["environments"]}
    columns = analysis.get("columns") or [
        {"id": r["id"], "environments": [r["id"]], "stacks": [r.get("stack")], **r}
        for r in analysis["environments"]
    ]
    lines = []
    for c in columns:
        mine = [rows[e] for e in c["environments"] if e in rows]
        if len(mine) == 1 and not c.get("no_environment"):
            lines += _row_lines(mine[0], active, selected, 0)
            continue
        stacks_text = ", ".join(c["stacks"]) or "no stack"
        evidence = mine[0]["evidence"] if mine else None
        lines.append(_line(f"{c['id']} ({stacks_text}): {env_counts_text(c)}", evidence))
        if c.get("missing"):
            lines.append(_line(f"no {c['id']} environment in: {', '.join(c['missing'])}", None, 1))
        for r in mine:
            lines += _row_lines(r, active, selected, 1)
        if c.get("no_environment"):
            lines += uncertain_lines(c)
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


def _row_lines(
    r: dict[str, Any], active: str | None, selected: set[str], level: int
) -> list[dict[str, Any]]:
    """One environment of one stack: its counts and files, then its unknowns."""
    files = ", ".join(r["backend_hcl"] + r["var_files"]) or "no files"
    mark = " (shown)" if r["id"] == active or r["id"] in selected else ""
    if not r.get("wired", True):
        mark += " (not wired)"
    label = r["id"] if not level else (r.get("stack") or r["id"])
    lines = [_line(f"{label}{mark}: {env_counts_text(r)} — {files}", r["evidence"], level)]
    lines += uncertain_lines(r, level + 1)
    for u in r["unknowns"]:
        lines.append(
            _line(
                f"unknown {u['subject']}.{u['attribute']} ({u['category']}): {u['reason']}",
                u["location"],
                level + 1,
                node=u["subject"],
                status="unknown",
            )
        )
    if r["unknown_count"] > len(r["unknowns"]):
        more = r["unknown_count"] - len(r["unknowns"])
        lines.append(_line(f"… {more} more unknowns", r["evidence"], level + 1, status="unknown"))
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
