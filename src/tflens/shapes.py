"""Root shapes: what kind of Terraform root each one is, and the view that explains it.

Built from model.json alone (deterministic, offline, no LLM). Each root gets a **shape**
from its resource counts, its backend and the structure detection, with the reason
stated in words:

* **app / pipeline**: runs code (Lambda, ECS, ...) fed by triggers or queues ->
  the **Flow** view (entry point -> compute -> what it sends to, with evidence);
* **platform / bootstrap**: shared services other roots use (ECR, DNS, certificates,
  KMS, IAM, log buckets, SSM parameters), little to no compute -> the **Provides** view
  (what it creates, the outputs it exposes, who reads them, per environment);
* **network**: VPCs, subnets, security groups, endpoints, transit gateways -> the
  **Network** view (VPC -> subnets -> endpoints / security groups, what can reach what,
  and a diagram: VPC -> subnets -> security groups -> resources, the rules labelled);
* **module library**: no backend, no provider configuration, nothing calls it -> the
  **Module catalog** (inputs, outputs, resources, example usage, who calls it);
* **other**: none of the above (a composition of remote modules, a data store, ...) ->
  the Provides view.

Every root also has an **IAM** view (role -> policies -> what they can access, with a
diagram); it is never a primary view.

A repository with roots of several shapes is *mixed*: each root keeps its own view and
the summary lists the roots grouped by shape, each with a one-line description.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from typing import Any

from .flow import COMPUTE_TYPES, build_flows, kind_of
from .flow import label_of as _flow_label
from .overview import FRIENDLY, _line, _plural
from .snippets import location_label
from .tabdiagrams import DIAGRAMS as TAB_DIAGRAMS
from .tabdiagrams import iam_sections
from .visuals import backend_text

SHAPE_ORDER = ("app", "platform", "network", "library", "other")
SHAPES: dict[str, dict[str, str]] = {
    "app": {"label": "app / pipeline", "view": "flow"},
    "platform": {"label": "platform / bootstrap", "view": "provides"},
    "network": {"label": "network", "view": "network"},
    "library": {"label": "module library", "view": "catalog"},
    "other": {"label": "other", "view": "provides"},
}
VIEWS = {
    "flow": "Flow",
    "provides": "Provides",
    "network": "Network",
    "iam": "IAM",
    "catalog": "Module catalog",
}
# Views with one diagram of the root under their sections (tflens.tabdiagrams).
DIAGRAM_VIEWS = ("network", "iam")

# What feeds code: queues, topics, streams, schedules, APIs, load balancers and the
# resources that wire them to it.
TRIGGER_TYPES = frozenset(
    {
        "aws_sqs_queue",
        "aws_sns_topic",
        "aws_sns_topic_subscription",
        "aws_kinesis_stream",
        "aws_kinesis_firehose_delivery_stream",
        "aws_msk_cluster",
        "aws_lambda_event_source_mapping",
        "aws_lambda_permission",
        "aws_cloudwatch_event_rule",
        "aws_cloudwatch_event_target",
        "aws_scheduler_schedule",
        "aws_pipes_pipe",
        "aws_s3_bucket_notification",
        "aws_api_gateway_rest_api",
        "aws_apigatewayv2_api",
        "aws_apigatewayv2_integration",
        "aws_lb",
        "aws_alb",
        "aws_lb_listener",
        "aws_lb_target_group",
    }
)
NETWORK_TYPES = frozenset(
    {
        "aws_vpc",
        "aws_subnet",
        "aws_security_group",
        "aws_security_group_rule",
        "aws_vpc_security_group_ingress_rule",
        "aws_vpc_security_group_egress_rule",
        "aws_default_security_group",
        "aws_vpc_endpoint",
        "aws_route_table",
        "aws_route",
        "aws_route_table_association",
        "aws_default_route_table",
        "aws_internet_gateway",
        "aws_egress_only_internet_gateway",
        "aws_nat_gateway",
        "aws_eip",
        "aws_network_acl",
        "aws_network_acl_rule",
        "aws_vpc_peering_connection",
        "aws_vpc_dhcp_options",
        "aws_flow_log",
        "aws_ec2_transit_gateway",
        "aws_ec2_transit_gateway_vpc_attachment",
        "aws_ec2_transit_gateway_route_table",
        "aws_ec2_transit_gateway_route",
        "aws_vpn_gateway",
        "aws_customer_gateway",
        "aws_vpn_connection",
    }
)
# Resources that build the network itself (a security group alone does not).
VPC_BUILDERS = frozenset({"aws_vpc", "aws_subnet", "aws_ec2_transit_gateway"})
# Platform services: (category, label, type prefixes).
PLATFORM_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ecr", "container registry (ECR)", ("aws_ecr_",)),
    ("dns", "DNS (Route 53)", ("aws_route53",)),
    ("certs", "certificates (ACM)", ("aws_acm_", "aws_acmpca_")),
    ("kms", "encryption keys (KMS)", ("aws_kms_",)),
    ("iam", "IAM", ("aws_iam_",)),
    ("buckets", "S3 buckets", ("aws_s3_bucket",)),
    ("params", "SSM parameters", ("aws_ssm_parameter",)),
    ("secrets", "secrets", ("aws_secretsmanager_",)),
    (
        "audit",
        "audit / logging",
        ("aws_cloudtrail", "aws_cloudwatch_log_group", "aws_config_", "aws_guardduty_"),
    ),
    ("accounts", "accounts", ("aws_organizations_",)),
)
# Network types named in one-line descriptions, in this order.
NETWORK_NAMED = (
    "aws_vpc",
    "aws_subnet",
    "aws_vpc_endpoint",
    "aws_security_group",
    "aws_ec2_transit_gateway",
    "aws_nat_gateway",
    "aws_internet_gateway",
)
EXTRA_NAMES: dict[str, tuple[str, str]] = {
    "aws_ec2_transit_gateway": ("transit gateway", "transit gateways"),
    "aws_ec2_transit_gateway_vpc_attachment": ("TGW attachment", "TGW attachments"),
    "aws_nat_gateway": ("NAT gateway", "NAT gateways"),
    "aws_internet_gateway": ("internet gateway", "internet gateways"),
    "aws_route_table": ("route table", "route tables"),
    "aws_lambda_event_source_mapping": ("event source mapping", "event source mappings"),
    "aws_sns_topic_subscription": ("SNS subscription", "SNS subscriptions"),
    "aws_ssm_parameter": ("SSM parameter", "SSM parameters"),
    "aws_route53_zone": ("Route 53 zone", "Route 53 zones"),
    "aws_acm_certificate": ("ACM certificate", "ACM certificates"),
}
# Backend settings that name a state (region and lock table do not).
_STATE_KEYS = ("bucket", "key", "prefix", "path", "organization", "storage_account_name")


# ------------------------------------------------------------------ helpers


def _names(rtype: str) -> tuple[str, str]:
    if rtype in FRIENDLY:
        return FRIENDLY[rtype]
    if rtype in EXTRA_NAMES:
        return EXTRA_NAMES[rtype]
    word = rtype.removeprefix("aws_").replace("_", " ")
    return word, word + "s"


def _count_text(by_type: Counter[str] | dict[str, int], order: tuple[str, ...] = ()) -> str:
    """``2 Lambda functions, 1 ECS service`` (``order`` first, then by name)."""
    types = [t for t in order if by_type.get(t)]
    types += sorted(t for t in by_type if by_type[t] and t not in types)
    parts = []
    for t in types:
        one, many = _names(t)
        parts.append(f"{by_type[t]} {one if by_type[t] == 1 else many}")
    return ", ".join(parts)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def label_of(n: dict[str, Any]) -> str:
    """The name people use: a ``Name`` tag, then the Flow view's label, then a domain."""
    tags = n["attributes"].get("tags")
    if isinstance(tags, dict) and isinstance(tags.get("Name"), str) and tags["Name"]:
        return tags["Name"]
    label = _flow_label(n)
    if label != n["name"] or n["kind"] == "external":
        return label
    for key in ("domain_name", "description"):
        value = n["attributes"].get(key)
        if isinstance(value, str) and value:
            return value
    return n["name"]


def platform_category(rtype: str) -> str | None:
    for cat, _label, prefixes in PLATFORM_CATEGORIES:
        if rtype.startswith(prefixes):
            return cat
    return None


_CATEGORY_LABELS = {cat: label for cat, label, _p in PLATFORM_CATEGORIES}


def _title(root: str) -> str:
    return "./" if root == "." else f"{root}/"


def _prefix(model: dict[str, Any], root: str) -> str:
    return f"{root}::" if len(model.get("roots", [])) > 1 else ""


def _owned(model: dict[str, Any], root: str, n: dict[str, Any]) -> bool:
    if n["kind"] == "external":
        return False
    if len(model.get("roots", [])) > 1:
        return n["id"].startswith(f"{root}::")
    return root in model.get("roots", [])


def root_entries(model: dict[str, Any]) -> list[dict[str, Any]]:
    return list((model.get("structure") or {}).get("roots", []))


def _entry(model: dict[str, Any], root: str) -> dict[str, Any] | None:
    return next((r for r in root_entries(model) if r["dir"] == root), None)


def root_model(model: dict[str, Any], root: str) -> dict[str, Any]:
    """The part of a model that belongs to one root: its nodes, the external nodes it
    touches, the edges between them, its structure entry and its module tree."""
    own = {n["id"] for n in model["nodes"] if _owned(model, root, n)}
    edges = [e for e in model["edges"] if e["source"] in own or e["target"] in own]
    touched = {e["source"] for e in edges} | {e["target"] for e in edges}
    nodes = [
        n
        for n in model["nodes"]
        if n["id"] in own or (n["kind"] == "external" and n["id"] in touched)
    ]
    ids = {n["id"] for n in nodes}
    structure = dict(model.get("structure") or {})
    structure["roots"] = [r for r in structure.get("roots", []) if r["dir"] == root]
    structure["environments"] = [
        e for e in structure.get("environments", []) if _env_covers(e, root)
    ]
    prefix = _prefix(model, root)
    return {
        **model,
        "roots": [r for r in model.get("roots", []) if r == root],
        "nodes": nodes,
        "edges": [e for e in edges if e["source"] in ids and e["target"] in ids],
        "modules": [m for m in model.get("modules", []) if m["id"].startswith(prefix)],
        "structure": structure,
    }


def _env_covers(env: dict[str, Any], root: str) -> bool:
    return root in env["roots"] or (not env["roots"] and env["root"] == ".")


def _module_dir(model: dict[str, Any], d: str | None) -> dict[str, Any] | None:
    dirs = (model.get("structure") or {}).get("module_dirs", [])
    return next((m for m in dirs if m["dir"] == d), None)


def _backend_phrase(entry: dict[str, Any], with_location: bool = True) -> str:
    b = entry["backend"]
    where = f" ({location_label(b['location'])})" if b.get("location") and with_location else ""
    if b["type"] == "local" and b.get("origin", "").startswith("default"):
        return "no backend block (local state)"
    return f"backend {backend_text(b)}{where}"


# ------------------------------------------------------------------ classification


def _counts(sub: dict[str, Any]) -> dict[str, Any]:
    resources = [n for n in sub["nodes"] if n["kind"] == "resource"]
    by_type = Counter(n["type"] for n in resources)
    platform: dict[str, int] = {}
    for t, c in by_type.items():
        cat = platform_category(t)
        if cat:
            platform[cat] = platform.get(cat, 0) + c
    return {
        "resources": len(resources),
        "data_sources": sum(1 for n in sub["nodes"] if n["kind"] == "data"),
        "by_type": dict(sorted(by_type.items())),
        "compute": {t: c for t, c in sorted(by_type.items()) if t in COMPUTE_TYPES},
        "triggers": {t: c for t, c in sorted(by_type.items()) if t in TRIGGER_TYPES},
        "network": {t: c for t, c in sorted(by_type.items()) if t in NETWORK_TYPES},
        "platform": {cat: platform[cat] for cat, _l, _p in PLATFORM_CATEGORIES if cat in platform},
        "trigger_edges": sum(1 for e in sub["edges"] if e["kind"] == "TRIGGER"),
    }


def classify_root(
    model: dict[str, Any], entry: dict[str, Any], sub: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``{dir, shape, view, reason, summary, counts, ...}`` of one structure root."""
    root = entry["dir"]
    sub = sub if sub is not None else root_model(model, root)
    c = _counts(sub)
    n_compute, n_trig = sum(c["compute"].values()), sum(c["triggers"].values())
    n_net, n_plat = sum(c["network"].values()), sum(c["platform"].values())
    core = [cat for cat in c["platform"] if cat != "iam"]
    builds_vpc = any(t in VPC_BUILDERS for t in c["network"])
    md = _module_dir(model, entry.get("module_dir") or root) or {}
    iface = md.get("interface") or {"variables": [], "outputs": []}
    n_vars, n_outs = len(iface["variables"]), len(iface["outputs"])
    compute_text = _count_text(c["compute"]) or "no compute"
    trig_text = _count_text(c["triggers"])
    if c["trigger_edges"]:
        edges = _plural(c["trigger_edges"], "TRIGGER edge")
        trig_text = f"{trig_text} ({edges})" if trig_text else edges
    if entry.get("role") == "library":
        shape = "library"
        why = (
            "no backend and no provider configuration, nothing in the repository calls it, "
            f"and it declares {_plural(n_vars, 'variable')} and {_plural(n_outs, 'output')}"
        )
    elif n_compute and (n_trig or c["trigger_edges"]):
        shape = "app"
        why = f"it runs {compute_text} fed by {trig_text}"
    elif n_compute and not builds_vpc and len(core) < 2:
        shape = "app"
        why = f"it runs {compute_text}; no trigger or queue is declared for it"
    elif n_net and len(core) < 2:
        shape = "network"
        why = f"it builds network resources ({_count_text(c['network'], NETWORK_NAMED)})"
        if n_compute:
            why += f" with little compute ({compute_text})"
    elif n_plat:
        shape = "platform"
        cats = [_CATEGORY_LABELS[cat] for cat in c["platform"]]
        why = f"it creates shared services ({', '.join(cats)})"
        why += f" with little compute ({compute_text})" if n_compute else " and runs no compute"
    else:
        shape = "other"
        why = "no compute, network or platform resources"
        if c["resources"]:
            why += f" (it defines {_count_text(c['by_type'])})"
        elif entry.get("modules"):
            why += f": it only calls {_plural(len(entry['modules']), 'module')}"
    unanalyzed = [m for m in entry.get("modules", []) if not m.get("analyzed")]
    counts = (
        f"{_plural(c['resources'], 'resource')}: {n_compute} compute, {n_trig} triggers/queues, "
        f"{n_net} network, {n_plat} platform"
    )
    if unanalyzed:
        counts += (
            f"; {_plural(len(unanalyzed), 'module call')} not analyzed "
            "(their resources are not counted)"
        )
    label = SHAPES[shape]["label"]
    reason = f"{label}: {why}. {counts}; {_backend_phrase(entry)}."
    if not entry.get("analyzed", True):
        reason += f" Not analyzed: {entry.get('status')}."
    view = SHAPES[shape]["view"]
    return {
        "dir": root,
        "title": _title(root),
        "shape": shape,
        "shape_label": label,
        "view": view,
        "view_label": VIEWS[view],
        "reason": reason,
        "summary": _summary(shape, sub, c, entry, iface),
        "counts": c,
        "analyzed": entry.get("analyzed", True),
        "location": next((s["location"] for s in entry.get("signals", []) if s["location"]), None),
        "views": [{"view": v, "label": VIEWS[v], "primary": v == view} for v in VIEWS],
    }


def _summary(
    shape: str,
    sub: dict[str, Any],
    c: dict[str, Any],
    entry: dict[str, Any],
    iface: dict[str, Any],
) -> str:
    """One line: what this root does."""
    if shape == "app":
        text = f"Runs {_count_text(c['compute'])}"
        chains = build_flows(sub)["chains"]
        if chains:
            text += f"; {_plural(len(chains), 'flow')}, main: {chains[0]['title']}"
        return text
    if shape == "platform":
        cats = [_CATEGORY_LABELS[cat] for cat in c["platform"]]
        if c["network"]:
            cats.append("network")
        text = f"Provides {_join(cats)}"
        outs = len(iface["outputs"])
        params = c["by_type"].get("aws_ssm_parameter", 0)
        exposed = []
        if outs:
            exposed.append(_plural(outs, "output"))
        if params:
            exposed.append(_plural(params, "SSM parameter"))
        return text + (f"; exposes {_join(exposed)}" if exposed else "; exposes no outputs")
    if shape == "network":
        named = {t: c["network"][t] for t in NETWORK_NAMED if t in c["network"]}
        return f"Builds {_count_text(named, NETWORK_NAMED) or _count_text(c['network'])}"
    if shape == "library":
        text = (
            f"Reusable module: {_plural(len(iface['variables']), 'input')}, "
            f"{_plural(len(iface['outputs']), 'output')}, {_plural(c['resources'], 'resource')}"
        )
        kinds = _count_text(c["by_type"])
        return text + (f" ({kinds})" if kinds else "")
    calls = entry.get("modules", [])
    if c["resources"]:
        text = f"Defines {_count_text(c['by_type'])}"
        return text + (f" and calls {_plural(len(calls), 'module')}" if calls else "")
    if calls:
        kinds = Counter(m["kind"] for m in calls)
        how = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        return f"Composes {_plural(len(calls), 'module')} ({how}); defines no resources itself"
    return "Defines no resources"


def build_shapes(model: dict[str, Any]) -> dict[str, Any]:
    """Every root with its shape, reason and primary view; the roots grouped by shape."""
    roots = [classify_root(model, r) for r in root_entries(model)]
    groups = []
    for shape in SHAPE_ORDER:
        members = [r for r in roots if r["shape"] == shape]
        if members:
            groups.append(
                {
                    "shape": shape,
                    "label": SHAPES[shape]["label"],
                    "view": SHAPES[shape]["view"],
                    "view_label": VIEWS[SHAPES[shape]["view"]],
                    "roots": [
                        {"dir": r["dir"], "title": r["title"], "summary": r["summary"]}
                        for r in members
                    ],
                }
            )
    lines = []
    for g in groups:
        lines.append(
            _line(
                f"{g['label']} ({_plural(len(g['roots']), 'root')}, {g['view_label']} view)", None
            )
        )
        for r in g["roots"]:
            root = next(x for x in roots if x["dir"] == r["dir"])
            lines.append(_line(f"{r['title']} — {r['summary']}", root["location"], 1))
    return {
        "roots": roots,
        "groups": groups,
        "mixed": len(groups) > 1,
        "lines": lines,
        "default_root": default_root(roots),
    }


def default_root(roots: list[dict[str, Any]]) -> str | None:
    """The root the Root view opens on: the first app / pipeline root, else the first
    platform, network, then module library root (SHAPE_ORDER); "other" roots last."""
    for shape in SHAPE_ORDER:
        for r in roots:
            if r["shape"] == shape:
                return r["dir"]
    return None


def render_shapes_markdown(shapes: dict[str, Any], level: int = 1) -> str:
    h = "#" * level
    out = [f"{h} Roots by shape", ""]
    if not shapes["roots"]:
        return "\n".join([*out, "No root found.", ""])
    if shapes["mixed"]:
        out += [
            f"Mixed repository: {_plural(len(shapes['roots']), 'root')} of "
            f"{len(shapes['groups'])} shapes, each with its own view.",
            "",
        ]
    for g in shapes["groups"]:
        out += [f"{h}# {g['label']} ({g['view_label']} view)", ""]
        for r in g["roots"]:
            root = next(x for x in shapes["roots"] if x["dir"] == r["dir"])
            out.append(f"- `{r['title']}` — {r['summary']}")
            out.append(f"  - Why: {root['reason']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ root views


def build_root_view(model: dict[str, Any], root: str, view: str = "") -> dict[str, Any] | None:
    """The view of one root: its primary view, or ``view`` when given (the switcher).
    None for an unknown root or view."""
    entry = _entry(model, root)
    if entry is None or (view and view not in VIEWS):
        return None
    sub = root_model(model, root)
    shape = classify_root(model, entry, sub)
    view = view or shape["view"]
    out: dict[str, Any] = {
        "root": root,
        "view": view,
        "view_label": VIEWS[view],
        "primary": view == shape["view"],
        "shape": {k: v for k, v in shape.items() if k != "counts"},
    }
    if view == "flow":
        out["flow"] = build_flows(sub)
    elif view == "provides":
        out["sections"] = provides_sections(model, entry, sub)
    elif view == "network":
        out["sections"] = network_sections(sub)
    elif view == "iam":
        out["sections"] = iam_sections(sub)
    else:
        out["sections"] = catalog_sections(model, entry, sub)
    if view in DIAGRAM_VIEWS and TAB_DIAGRAMS[view][0](sub)["nodes"]:
        out["diagram"] = view  # api/diagram?name=<view>&root=<dir>
    return out


def _nodes(sub: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in sub["nodes"]}


def _kind(n: dict[str, Any]) -> str:
    if n["kind"] == "resource" and (n["type"] in FRIENDLY or n["type"] in EXTRA_NAMES):
        return _names(n["type"])[0]
    return kind_of(n)


def _node_line(n: dict[str, Any], level: int, extra: str = "") -> dict[str, Any]:
    text = f"{_kind(n)}: {label_of(n)}" + (f" — {extra}" if extra else "")
    return _line(text, n["location"], level, n["id"])


def _ref_target(value: Any) -> str | None:
    return value.get("$ref") if isinstance(value, dict) else None


# -- Provides ----------------------------------------------------------------------------


def _group_of(rtype: str) -> str:
    cat = platform_category(rtype)
    if cat:
        return _CATEGORY_LABELS[cat]
    if rtype in NETWORK_TYPES:
        return "network"
    if rtype in COMPUTE_TYPES:
        return "compute"
    if rtype in TRIGGER_TYPES:
        return "triggers / queues"
    return "other"


def provides_sections(
    model: dict[str, Any], entry: dict[str, Any], sub: dict[str, Any]
) -> list[dict[str, Any]]:
    """What a root creates, the outputs it exposes, who reads them, per environment."""
    root = entry["dir"]
    nodes = _nodes(sub)
    resources = [n for n in sub["nodes"] if n["kind"] == "resource"]
    # 1. what it creates, by service group
    creates: list[dict[str, Any]] = []
    order = [label for _c, label, _p in PLATFORM_CATEGORIES]
    order += ["network", "compute", "triggers / queues", "other"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for n in resources:
        groups.setdefault(_group_of(n["type"]), []).append(n)
    for g in order:
        if g in groups:
            creates.append(_line(f"{g} ({len(groups[g])})", None))
            creates += [_node_line(n, 1) for n in groups[g]]
    unanalyzed = [m for m in entry.get("modules", []) if not m.get("analyzed")]
    for m in unanalyzed:
        creates.append(
            _line(
                f"{m['address']}: {m['kind']} {m['source']} — not analyzed, its resources "
                "are not listed",
                m["location"],
                status="unknown",
            )
        )
    if not creates:
        creates.append(_line("Creates no resources", None))
    # 2. outputs, and who reads them through terraform_remote_state
    consumers = remote_state_consumers(model, entry)
    md = _module_dir(model, entry.get("module_dir") or root) or {}
    outputs = (md.get("interface") or {}).get("outputs", [])
    values = _root_outputs(model, root)
    out_lines: list[dict[str, Any]] = []
    for o in outputs:
        target = _ref_target(values.get(o["name"]))
        text = f"{o['name']} = {o['value'] or '?'}"
        if target in nodes:
            text += f" ({label_of(nodes[target])})"
        if o["description"]:
            text += f" — {o['description']}"
        out_lines.append(_line(text, o["location"], 0, target if target in nodes else None))
        for c in consumers:
            for use in c["uses"]:
                if use["output"] == o["name"]:
                    out_lines.append(
                        _line(
                            f"read by {_title(c['root'])} {use['by']} (via {c['address']})",
                            use["location"],
                            1,
                            edge=use["edge"],
                        )
                    )
    if not out_lines:
        out_lines.append(_line("No outputs: other roots cannot read anything from its state", None))
    # 3. consumers: remote state and SSM parameters
    readers: list[dict[str, Any]] = []
    for c in consumers:
        used = sorted({u["output"] for u in c["uses"]})
        readers.append(
            _line(
                f"{_title(c['root'])} reads its state via {c['address']}"
                + (f" (outputs: {', '.join(used)})" if used else ""),
                c["location"],
                0,
                c["node"],
            )
        )
    for p in [n for n in resources if n["type"] == "aws_ssm_parameter"]:
        name = p["attributes"].get("name")
        src = _ref_target(p["attributes"].get("value"))
        text = f"SSM parameter {name if isinstance(name, str) else label_of(p)}"
        if src in nodes:
            text += f" (value from {label_of(nodes[src])})"
        readers.append(_line(text, p["location"], 0, p["id"]))
        found = ssm_readers(model, name) if isinstance(name, str) else []
        for r in found:
            readers.append(
                _line(f"read by {_title(r['root'])} {r['address']}", r["location"], 1, r["node"])
            )
        if not found:
            readers.append(_line("no reader of this parameter in this repository", None, 1))
    if not consumers:
        readers.append(
            _line(
                "No terraform_remote_state data source in this repository reads its state "
                "(consumers outside the repository are not visible)",
                None,
            )
        )
    # 4. per environment
    envs = [
        e for e in (model.get("structure") or {}).get("environments", []) if _env_covers(e, root)
    ]
    env_lines: list[dict[str, Any]] = []
    state = _backend_phrase(entry, with_location=False)
    for e in envs:
        files = ", ".join(e["var_files"]) or "no var files"
        loc = e["evidence"][0] if e["evidence"] else None
        env_lines.append(_line(f"{e['name']} ({', '.join(e['kinds'])}): {files}", loc))
        env_lines.append(_line(f"state: {state}", entry["backend"].get("location"), 1))
    if not envs:
        env_lines.append(
            _line(
                f"One environment (no tfvars or environment directories found); state: {state}",
                entry["backend"].get("location"),
            )
        )
    return [
        {"id": "creates", "title": "What it creates", "lines": creates},
        {"id": "outputs", "title": f"Outputs exposed ({len(outputs)})", "lines": out_lines},
        {
            "id": "consumers",
            "title": "Who reads it (remote state, SSM parameters)",
            "lines": readers,
        },
        {"id": "envs", "title": "Per environment", "lines": env_lines},
    ]


def _root_outputs(model: dict[str, Any], root: str) -> dict[str, Any]:
    want = f"{_prefix(model, root)}(root)"
    mod = next((m for m in model.get("modules", []) if m["id"] == want), None)
    return mod["outputs"] if mod else {}


def _root_of_node(model: dict[str, Any], nid: str) -> str:
    if "::" in nid:
        return nid.split("::", 1)[0]
    roots = model.get("roots", [])
    return roots[0] if roots else "."


def remote_state_consumers(model: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    """``data "terraform_remote_state"`` sources in other roots whose backend settings
    name this root's state; with the outputs they use (from CONFIG edges)."""
    b = entry["backend"]
    mine = {s["key"]: s["value"] for s in b["settings"] if s.get("known")}
    out = []
    for n in model["nodes"]:
        if n["kind"] != "data" or n["type"] != "terraform_remote_state":
            continue
        owner = _root_of_node(model, n["id"])
        if owner == entry["dir"]:
            continue
        attrs = n["attributes"]
        config = attrs.get("config")
        if attrs.get("backend") != b["type"] or not isinstance(config, dict):
            continue
        compared = [k for k in _STATE_KEYS if k in mine and isinstance(config.get(k), str)]
        if not compared or any(config[k] != mine[k] for k in compared):
            continue
        uses = []
        for e in model["edges"]:
            if e["kind"] != "CONFIG" or e["target"] != n["id"]:
                continue
            for ref in e["attributes"].get("refs", []):
                head = f"{n['id']}.outputs."
                if isinstance(ref, str) and ref.startswith(head):
                    by = e["source"].split("::", 1)[-1]
                    loc = e["evidence"][0] if e["evidence"] else None
                    uses.append(
                        {
                            "output": ref[len(head) :].split(".", 1)[0],
                            "by": f"{by}.{e['attributes'].get('attribute', '')}".rstrip("."),
                            "edge": e["id"],
                            "location": {k: loc[k] for k in ("file", "start_line", "end_line")}
                            if loc
                            else None,
                        }
                    )
        out.append(
            {
                "root": owner,
                "node": n["id"],
                "address": n["id"].split("::", 1)[-1],
                "location": n["location"],
                "compared": compared,
                "uses": uses,
            }
        )
    return out


def ssm_readers(model: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """``data "aws_ssm_parameter"`` sources (any root) reading the parameter ``name``."""
    return [
        {
            "root": _root_of_node(model, n["id"]),
            "node": n["id"],
            "address": n["id"].split("::", 1)[-1],
            "location": n["location"],
        }
        for n in model["nodes"]
        if n["kind"] == "data"
        and n["type"] == "aws_ssm_parameter"
        and n["attributes"].get("name") == name
    ]


# -- Network -----------------------------------------------------------------------------

_PLACEMENT = ("subnet_id", "subnet_ids", "subnet_mapping")
_SG_ATTRS = ("security_group_ids", "security_groups", "vpc_security_group_ids")
_VPC_CHILD_ORDER = (
    "aws_subnet",
    "aws_security_group",
    "aws_vpc_endpoint",
    "aws_route_table",
    "aws_internet_gateway",
    "aws_nat_gateway",
    "aws_ec2_transit_gateway_vpc_attachment",
)


def _attr_edges(sub: dict[str, Any], attrs: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        e
        for e in sub["edges"]
        if e["kind"] == "CONFIG" and e["attributes"].get("attribute") in attrs
    ]


def _cidr(n: dict[str, Any]) -> str:
    cidr = n["attributes"].get("cidr_block")
    return cidr if isinstance(cidr, str) else ""


def _network_detail(n: dict[str, Any]) -> str:
    a = n["attributes"]
    if n["type"] in ("aws_vpc", "aws_subnet"):
        parts = [_cidr(n)]
        if isinstance(a.get("availability_zone"), str):
            parts.append(a["availability_zone"])
        return " ".join(p for p in parts if p)
    if n["type"] == "aws_vpc_endpoint":
        kind = (
            a.get("vpc_endpoint_type") if isinstance(a.get("vpc_endpoint_type"), str) else "Gateway"
        )
        service = a.get("service_name") if isinstance(a.get("service_name"), str) else "?"
        return f"{service}, {kind}"
    return ""


def network_sections(sub: dict[str, Any]) -> list[dict[str, Any]]:
    """VPC -> subnets -> what is placed in them / security groups / endpoints; what is
    outside a VPC created here; what can reach what (NETWORK edges, with evidence)."""
    nodes = _nodes(sub)
    in_vpc: dict[str, list[str]] = {}
    for e in _attr_edges(sub, ("vpc_id",)):
        if nodes.get(e["target"], {}).get("type") == "aws_vpc":
            in_vpc.setdefault(e["target"], []).append(e["source"])
    placed: dict[str, list[str]] = {}
    for e in _attr_edges(sub, _PLACEMENT):
        if nodes.get(e["source"], {}).get("type") != "aws_route_table_association":
            placed.setdefault(e["target"], []).append(e["source"])
    guarded: dict[str, list[str]] = {}
    for e in _attr_edges(sub, _SG_ATTRS):
        guarded.setdefault(e["target"], []).append(e["source"])
    vpcs = [n for n in sub["nodes"] if n["type"] == "aws_vpc" and n["kind"] == "resource"]
    lines: list[dict[str, Any]] = []
    shown: set[str] = set()
    for v in vpcs:
        lines.append(_node_line(v, 0, _network_detail(v)))
        shown.add(v["id"])
        children = sorted(
            set(in_vpc.get(v["id"], [])),
            key=lambda i: (
                _VPC_CHILD_ORDER.index(nodes[i]["type"])
                if nodes[i]["type"] in _VPC_CHILD_ORDER
                else len(_VPC_CHILD_ORDER),
                i,
            ),
        )
        for cid in children:
            child = nodes[cid]
            lines.append(_node_line(child, 1, _network_detail(child)))
            shown.add(cid)
            if child["type"] == "aws_subnet":
                for pid in sorted(set(placed.get(cid, []))):
                    lines.append(_node_line(nodes[pid], 2, "placed in this subnet"))
            elif child["type"] == "aws_security_group":
                for pid in sorted(set(guarded.get(cid, []))):
                    lines.append(_node_line(nodes[pid], 2, "uses this security group"))
    if not vpcs:
        lines.append(_line("No VPC is created in this root", None))
    outside = [
        n
        for n in sub["nodes"]
        if n["kind"] == "resource"
        and n["type"] in NETWORK_TYPES
        and n["id"] not in shown
        and n["type"] not in ("aws_route_table_association", "aws_route")
    ]
    out_lines: list[dict[str, Any]] = []
    for n in outside:
        vpc = n["attributes"].get("vpc_id")
        extra = ""
        if vpc is not None and _ref_target(vpc) not in nodes:
            extra = f"vpc_id {vpc if isinstance(vpc, str) else 'from outside this root'}"
        out_lines.append(_node_line(n, 0, extra or _network_detail(n)))
    reach: list[dict[str, Any]] = []
    for e in sub["edges"]:
        if e["kind"] != "NETWORK" or e["attributes"].get("attachment"):
            continue
        src, tgt = nodes.get(e["source"]), nodes.get(e["target"])
        text = (
            f"{label_of(src) if src else e['source']} → {label_of(tgt) if tgt else e['target']}: "
            f"{e['label']}"
        )
        public = e["attributes"].get("public") is True
        if public:
            text += " (public)"
        loc = e["evidence"][0] if e["evidence"] else None
        reach.append(
            _line(
                text,
                {k: loc[k] for k in ("file", "start_line", "end_line")} if loc else None,
                0,
                edge=e["id"],
                status="unknown" if public else "",
            )
        )
    if not reach:
        reach.append(_line("No NETWORK edge (security group rule, endpoint, ...) found", None))
    sections = [
        {"id": "vpcs", "title": "VPCs, subnets, security groups, endpoints", "lines": lines}
    ]
    if out_lines:
        sections.append(
            {"id": "outside", "title": "Not inside a VPC created here", "lines": out_lines}
        )
    sections.append({"id": "reach", "title": "What can reach what", "lines": reach})
    return sections


# -- Module catalog ----------------------------------------------------------------------

_PLACEHOLDERS = (
    ("string", '"..."'),
    ("number", "0"),
    ("bool", "false"),
    ("list", "[]"),
    ("set", "[]"),
    ("tuple", "[]"),
    ("map", "{}"),
    ("object", "{}"),
)


def _placeholder(vtype: str | None) -> str:
    for prefix, value in _PLACEHOLDERS:
        if (vtype or "").startswith(prefix):
            return value
    return "null"


def example_usage(d: str, iface: dict[str, Any]) -> str:
    """A module block calling ``d`` with every required input (optional ones commented)."""
    name = posixpath.basename(d) if d != "." else "this"
    name = name.replace("-", "_") or "this"
    lines = [f'module "{name}" {{', f'  source = "./{d}"' if d != "." else '  source = "./"']
    required = [v for v in iface["variables"] if v["required"]]
    optional = [v for v in iface["variables"] if not v["required"]]
    if required or optional:
        lines.append("")
    width = max((len(v["name"]) for v in iface["variables"]), default=0)
    for v in required:
        lines.append(f"  {v['name']:<{width}} = {_placeholder(v['type'])}  # required")
    for v in optional:
        lines.append(f"  # {v['name']:<{width}} = {v.get('default_hcl') or v['default']}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def catalog_sections(
    model: dict[str, Any], entry: dict[str, Any], sub: dict[str, Any]
) -> list[dict[str, Any]]:
    """One section per module directory of the root (the root module first, then the
    local modules it calls): inputs, outputs, resources, who calls it, example usage."""
    root = entry["dir"]
    dirs = [entry.get("module_dir") or root]
    for m in entry.get("modules", []):
        if m["kind"] == "local" and m.get("dir") and m["dir"] not in dirs:
            dirs.append(m["dir"])
    sections = []
    for i, d in enumerate(dirs):
        md = _module_dir(model, d)
        if md is None:
            continue
        iface = md.get("interface") or {"variables": [], "outputs": []}
        lines = [_line(f"Inputs ({len(iface['variables'])})", None)]
        for v in iface["variables"]:
            text = f"{v['name']}: {v['type'] or 'any'}"
            text += " — required" if v["required"] else f" = {v['default']}"
            if v["description"]:
                text += f" — {v['description']}"
            lines.append(_line(text, v["location"], 1))
        lines.append(_line(f"Outputs ({len(iface['outputs'])})", None))
        for o in iface["outputs"]:
            text = f"{o['name']} = {o['value'] or '?'}"
            if o["description"]:
                text += f" — {o['description']}"
            lines.append(_line(text, o["location"], 1))
        defined = {}
        for n in sub["nodes"]:
            loc = n["location"]
            if (
                n["kind"] in ("resource", "data")
                and loc
                and (posixpath.dirname(loc["file"]) or ".") == d
            ):
                defined.setdefault((n["type"], n["name"], n["kind"]), n)
        lines.append(_line(f"Resources ({len(defined)})", None))
        lines += [_node_line(n, 1) for _k, n in sorted(defined.items())]
        calls = [
            m
            for m in entry.get("modules", [])
            if m.get("location") and (posixpath.dirname(m["location"]["file"]) or ".") == d
        ]
        if calls:
            lines.append(_line(f"Calls ({len(calls)})", None))
            for m in calls:
                lines.append(_line(f"{m['address']}: {m['kind']} {m['source']}", m["location"], 1))
        lines.append(_line("Called by", None))
        for c in md["called_by"]:
            lines.append(_line(f"{_title(c['caller'])} as {c['address']}", c["location"], 1))
        if not md["called_by"]:
            lines.append(
                _line(
                    "nothing in this repository calls it"
                    + (" (it is a root: used from other repositories)" if i == 0 else ""),
                    None,
                    1,
                )
            )
        sections.append(
            {
                "id": f"mod-{i}",
                "title": f"Module {_title(d)}",
                "location": md["location"],
                "lines": lines,
                "code": example_usage(d, iface),
                "code_title": "Example usage",
            }
        )
    return sections
