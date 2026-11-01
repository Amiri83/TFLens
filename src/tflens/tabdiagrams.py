"""The diagrams of the Root view's Network and IAM views, drawn like the Flow view's.

Built from model.json alone (deterministic, offline, no LLM), with the same renderer as
the flows (:func:`tflens.flow.flow_svg`: columns by depth, numbered labelled steps, a
``file:line`` code reference on every box):

* **network**: VPC -> subnets -> security groups -> the resources placed in the subnets
  or guarded by the groups (instances, ECS services, load balancers, Lambda functions,
  endpoints, ...). Security group **rules** (NETWORK edges: SG -> SG, CIDR -> SG,
  SG -> CIDR) are the labelled steps: what can reach what. The CIDRs, prefix lists and
  groups outside the model are a column of their own, left of the VPC. Containment is
  drawn quiet, at right angles, clear of the boxes;
* **iam**: role / user / group -> its attached and inline policies -> the resources the
  policies grant actions on (ACCESS edges), labelled *can write* / *can read* /
  *can access* (the Flow view's IAM-only style) or *denies*.
"""

from __future__ import annotations

from typing import Any

from .flow import (
    COMPUTE_TYPES,
    READ_ACTIONS,
    WRITE_ACTIONS,
    _granted,
    _loc,
    _statement,
    flow_svg,
    kind_of,
)
from .overview import _line
from .snippets import location_label

# the Network diagram's columns: what is outside the VPC (CIDRs, prefix lists, groups
# outside this model) leftmost, so no VPC or containment line cuts across it
EXTERNAL, VPC, SUBNET, GROUP, PLACED = range(5)
SG_TYPES = frozenset({"aws_security_group", "aws_default_security_group"})
# attributes (last path segment) that place a resource in a subnet / attach a group
PLACEMENT_ATTRS = ("subnet_id", "subnet_ids", "subnet_mapping", "subnets", "vpc_zone_identifier")
SG_ATTRS = ("security_group_ids", "security_groups", "vpc_security_group_ids")
PRINCIPAL_TYPES = ("aws_iam_role", "aws_iam_user", "aws_iam_group")
NETWORK_LEGEND = (
    ("security group rule", "rule", ""),
    ("public rule", "rule", "public"),
    ("contains / placed in / guarded by", "contains", ""),
)
IAM_LEGEND = (
    ("attached / inline policy", "contains", ""),
    ("IAM only (can write / can read / can access)", "send", "iam_only"),
    ("denies", "deny", ""),
)


def _ev(e: dict[str, Any]) -> dict[str, Any] | None:
    return _loc(e["evidence"][0] if e["evidence"] else None)


def _last(attr: str) -> str:
    """``network_configuration.subnets`` -> ``subnets`` (``subnet_ids[0]`` too)."""
    return attr.rsplit(".", 1)[-1].split("[", 1)[0]


class _Diagram:
    """Nodes with a depth, and numbered steps: the shape :func:`flow_svg` draws."""

    def __init__(self, model: dict[str, Any]) -> None:
        from .shapes import label_of  # not at the top: shapes imports this module

        self.label_of = label_of
        self.model = model
        self.nodes = {n["id"]: n for n in model["nodes"]}
        self.out: dict[str, dict[str, Any]] = {}
        self.rules: list[dict[str, Any]] = []
        self.quiet: list[dict[str, Any]] = []

    def type_of(self, nid: str) -> str:
        n = self.nodes.get(nid)
        return n["type"] if n else ""

    def name(self, nid: str) -> str:
        n = self.nodes.get(nid)
        if n is not None:
            return self.label_of(n)
        return self.out[nid]["label"] if nid in self.out else nid

    def node(
        self,
        nid: str,
        depth: int,
        role: str,
        loc: dict[str, Any] | None = None,
        label: str = "",
        kind_label: str = "",
    ) -> None:
        """Add a node once (its first depth wins); ``loc`` is its code reference when it
        has no block of its own (an external CIDR, an inline policy)."""
        if nid in self.out:
            return
        n = self.nodes.get(nid)
        own = n["location"] if n else None
        self.out[nid] = {
            "id": nid,
            "label": label or (self.label_of(n) if n else nid),
            "kind_label": kind_label or (kind_of(n) if n else ""),
            "depth": depth,
            "location": own,
            "code_ref": own or loc,
            "role": role,
        }

    def step(
        self,
        source: str,
        target: str,
        kind: str,
        status: str,
        verb: str,
        evidence: list[dict[str, Any]],
        quiet: bool = False,
        what: str = "",
    ) -> None:
        detail = " + ".join(
            f"{e['what']} {location_label(e['location'])}" for e in evidence if e["location"]
        )
        s = {
            "kind": kind,
            "status": status,
            "verb": verb,
            "source": source,
            "target": target,
            "summary": (
                f"{self.name(source)} → {self.name(target)}: {verb}"
                + (f" ({what})" if what else "")
                if kind == "rule"
                else f"{self.name(source)} {verb} {self.name(target)}"
                + (f": {what}" if what else "")
            ),
            "what": what,
            "evidence": evidence,
            "quiet": quiet,
            "detail": detail,
        }
        (self.quiet if quiet else self.rules).append(s)

    def result(self, title: str) -> dict[str, Any]:
        """Labelled steps first (numbered 1..), then the quiet structure; nodes by depth,
        then name: the same model always gives the same diagram."""
        key = lambda s: (self.name(s["target"]), self.name(s["source"]), s["verb"], s["target"])  # noqa: E731
        steps = sorted(self.rules, key=key) + sorted(self.quiet, key=key)
        for i, s in enumerate(steps, 1):
            s["n"] = i
            s["text"] = f"{i}. {s['summary']}" + (f" ({s['detail']})" if s["detail"] else "")
            del s["detail"]
        nodes = sorted(self.out.values(), key=lambda n: (n["depth"], n["label"], n["id"]))
        return {"title": title, "nodes": nodes, "steps": steps}


# ------------------------------------------------------------------ network


def network_diagram(model: dict[str, Any]) -> dict[str, Any]:
    """VPC -> subnets -> security groups -> resources, and the security group rules."""
    d = _Diagram(model)
    resources = [n for n in model["nodes"] if n["kind"] == "resource"]
    for n in resources:
        if n["type"] == "aws_vpc":
            d.node(n["id"], VPC, "network")
    edges = model["edges"]
    config = [e for e in edges if e["kind"] == "CONFIG" and e["source"] in d.nodes]
    # VPC -> subnets / security groups
    for e in config:
        a = e["attributes"].get("attribute", "")
        if a == "vpc_id" and d.type_of(e["target"]) == "aws_vpc":
            src = e["source"]
            if d.type_of(src) == "aws_subnet":
                d.node(src, SUBNET, "network")
            elif d.type_of(src) in SG_TYPES:
                d.node(src, GROUP, "guard")
            else:
                continue
            ev = [{"what": "vpc_id", "location": _ev(e), "edge": e["id"]}]
            d.step(e["target"], src, "contains", "", "contains", ev, quiet=True)
    for n in resources:  # subnets and groups outside a VPC created here
        if n["type"] == "aws_subnet":
            d.node(n["id"], SUBNET, "network")
        elif n["type"] in SG_TYPES:
            d.node(n["id"], GROUP, "guard")
    # subnet -> what is placed in it; security group -> what it guards
    guards: set[tuple[str, str]] = set()
    for e in config:
        a, src, tgt = _last(e["attributes"].get("attribute", "")), e["source"], e["target"]
        if (
            d.type_of(src) == "aws_route_table_association"
            or d.out.get(src, {}).get("depth", PLACED) < PLACED
        ):
            continue  # a VPC / subnet / group referencing another is not placed in it
        if a in PLACEMENT_ATTRS and d.type_of(tgt) == "aws_subnet":
            d.node(src, PLACED, "compute" if _is_compute(d, src) else "target")
            ev = [{"what": e["label"], "location": _ev(e), "edge": e["id"]}]
            d.step(tgt, src, "contains", "", "has in it", ev, quiet=True)
        elif a in SG_ATTRS and d.type_of(tgt) in SG_TYPES and (tgt, src) not in guards:
            guards.add((tgt, src))
            d.node(src, PLACED, "compute" if _is_compute(d, src) else "target")
            ev = [{"what": e["label"], "location": _ev(e), "edge": e["id"]}]
            d.step(tgt, src, "contains", "", "guards", ev, quiet=True)
    for e in edges:  # attachments the extractors record (Lambda vpc_config, ...)
        if e["kind"] == "NETWORK" and e["attributes"].get("attachment"):
            src, tgt = e["source"], e["target"]
            if d.type_of(tgt) in SG_TYPES and (tgt, src) not in guards and src in d.nodes:
                guards.add((tgt, src))
                d.node(src, PLACED, "compute" if _is_compute(d, src) else "target")
                ev = [{"what": e["label"], "location": _ev(e), "edge": e["id"]}]
                d.step(tgt, src, "contains", "", "guards", ev, quiet=True)
    # the rules: what can reach what
    rules = [e for e in edges if e["kind"] == "NETWORK" and not e["attributes"].get("attachment")]
    for e in sorted(rules, key=lambda e: e["id"]):
        loc = _ev(e)
        for nid in (e["source"], e["target"]):
            if nid not in d.out:  # a CIDR, a prefix list, a group outside this model
                n = d.nodes.get(nid)
                d.node(
                    nid,
                    EXTERNAL,
                    "entry",
                    loc,
                    kind_label=kind_of(n) if n else "external",
                )
        public = e["attributes"].get("public") is True
        via = ", ".join(e["via"])
        ev = [{"what": e["label"] + (f" ({via})" if via else ""), "location": loc, "edge": e["id"]}]
        d.step(
            e["source"],
            e["target"],
            "rule",
            "public" if public else "",
            e["label"],
            ev,
            what=e["attributes"].get("description") or "",
        )
    return d.result("Network: VPC → subnets → security groups → resources")


def _is_compute(d: _Diagram, nid: str) -> bool:
    return d.type_of(nid) in COMPUTE_TYPES


# ------------------------------------------------------------------ IAM


def iam_diagram(model: dict[str, Any]) -> dict[str, Any]:
    """Role / user / group -> attached and inline policies -> what they grant access to."""
    d = _Diagram(model)
    principals = sorted(
        (n for n in model["nodes"] if n["kind"] == "resource" and n["type"] in PRINCIPAL_TYPES),
        key=lambda n: (PRINCIPAL_TYPES.index(n["type"]), d.label_of(n), n["id"]),
    )
    for p in principals:
        d.node(p["id"], 0, "principal")
    access = sorted(
        (
            e
            for e in model["edges"]
            if e["kind"] == "ACCESS"
            and e["source"] in d.out
            and not e["attributes"].get("resource_policy")
        ),
        key=lambda e: e["id"],
    )
    for e in access:
        src, tgt, attrs = e["source"], e["target"], e["attributes"]
        attach = next((x for x in e["evidence"] if x.get("note") == "policy attachment"), None)
        if d.type_of(tgt) == "aws_managed_policy" or "policy_arn" in attrs:
            # a managed policy (not expanded): the policy itself is the end of the line
            d.node(tgt, 1, "policy", _loc(attach))
            ev = [{"what": "policy attachment", "location": _loc(attach), "edge": e["id"]}]
            d.step(src, tgt, "contains", "", "attaches", ev, quiet=True)
            continue
        policy = attrs.get("policy")
        stmt = _loc(_statement(e))
        if not isinstance(policy, str) or policy == src or policy not in d.nodes:
            pid = f"{src}#inline"
            d.node(pid, 1, "policy", stmt, f"{d.name(src)} (inline)", "inline policy")
        else:
            pid = policy
            d.node(pid, 1, "policy")
        if attach is not None:
            link_ev = [{"what": "policy attachment", "location": _loc(attach), "edge": e["id"]}]
        else:
            link_ev = [{"what": "policy", "location": d.out[pid]["code_ref"], "edge": e["id"]}]
        d.step(src, pid, "contains", "", "has", link_ev, quiet=True)
        d.node(tgt, 2, "compute" if _is_compute(d, tgt) else "target", stmt)
        actions = [a for a in attrs.get("actions") or [] if isinstance(a, str)]
        if attrs.get("effect") == "Deny":
            kind, status, verb = "deny", "", "denies"
        elif _granted(e, WRITE_ACTIONS):
            kind, status, verb = "send", "iam_only", "can write"
        elif _granted(e, READ_ACTIONS):
            kind, status, verb = "read", "iam_only", "can read"
        else:
            kind, status, verb = "access", "iam_only", "can access"
        what = f"{attrs.get('effect', 'Allow')} {', '.join(actions)}"
        ev = [{"what": f"IAM {', '.join(actions)}", "location": stmt, "edge": e["id"]}]
        d.step(pid, tgt, kind, status, verb, ev, what=what)
    # the quiet principal -> policy links are per statement: keep one per pair
    seen: set[tuple[str, str]] = set()
    unique = []
    for s in d.quiet:
        if (s["source"], s["target"]) not in seen:
            seen.add((s["source"], s["target"]))
            unique.append(s)
    d.quiet = unique
    return d.result("IAM: role → policies → resources it can access")


# ------------------------------------------------------------------ views


DIAGRAMS = {"network": (network_diagram, NETWORK_LEGEND), "iam": (iam_diagram, IAM_LEGEND)}


def tab_svg(model: dict[str, Any], name: str, title: str = "") -> str | None:
    """The SVG of the ``network`` or ``iam`` diagram (None: unknown name, or nothing to
    draw)."""
    if name not in DIAGRAMS:
        return None
    build, legend = DIAGRAMS[name]
    dg = build(model)
    if not dg["nodes"]:
        return None
    return flow_svg(dg, f"{dg['title']} — {title}" if title else dg["title"], legend)


def iam_sections(sub: dict[str, Any]) -> list[dict[str, Any]]:
    """The IAM view: each principal, its policies, and what each policy grants."""
    dg = iam_diagram(sub)
    nodes = {n["id"]: n for n in dg["nodes"]}
    lines: list[dict[str, Any]] = []
    for p in (n for n in dg["nodes"] if n["depth"] == 0):
        lines.append(_line(f"{p['kind_label']}: {p['label']}", p["code_ref"], 0, p["id"]))
        policies = [s for s in dg["steps"] if s["source"] == p["id"]]
        if not policies:
            lines.append(_line("no policy found in this repository", None, 1))
        for link in policies:
            pol = nodes[link["target"]]
            lines.append(
                _line(f"{pol['kind_label']}: {pol['label']}", pol["code_ref"], 1, pol["id"])
            )
            for s in (s for s in dg["steps"] if s["source"] == pol["id"]):
                ev = s["evidence"][0] if s["evidence"] else {}
                tgt = nodes[s["target"]]
                lines.append(
                    _line(
                        f"{s['verb']} {tgt['label']} ({s['what']})",
                        ev.get("location"),
                        2,
                        tgt["id"],
                        edge=ev.get("edge"),
                        status="unknown" if s["kind"] == "deny" else "",
                    )
                )
    if not lines:
        lines.append(_line("No IAM role, user or group is created in this root", None))
    return [{"id": "iam", "title": "Roles → policies → what they can access", "lines": lines}]
