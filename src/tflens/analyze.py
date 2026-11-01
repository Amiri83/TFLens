"""Build the graph: one node per resource/data source/module, typed edges with evidence.

Every resource and data source is walked generically (CONFIG edges for attribute
references, UNKNOWN entries with reasons). Service-specific extractors then add
ACCESS, TRIGGER, NETWORK and RUNS_ON edges. Finally, access granted to IAM roles
is propagated to the compute resources that run as those roles.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .engine import Engine, ModuleInstance
from .loader import Block, RepoSource
from .model import Edge, Evidence, Graph, Node
from .policy import Statement, as_list, parse_policy, statement_spans
from .values import (
    Interp,
    JsonEncoded,
    Location,
    Ref,
    Unknown,
    collect_refs,
    collect_unknowns,
    display,
    render,
    scalar_str,
    to_json,
    unwrap,
)

# Meta-arguments that are not part of the resource's configuration graph.
_SKIP_ATTRS = {"provider", "lifecycle"}


@dataclass(frozen=True)
class IdentitySpec:
    """How a resource is named inside ARNs (used to match literal ARNs/names)."""

    attr: str
    service: str
    extract: Callable[[str], str | None]


def _after(prefix: str) -> Callable[[str], str | None]:
    def run(res: str) -> str | None:
        if not res.startswith(prefix):
            return None
        return res[len(prefix) :].rsplit("/", 1)[-1]

    return run


def _dynamodb(res: str) -> str | None:
    if not res.startswith("table/"):
        return None
    return res.split("/")[1]


def _lambda(res: str) -> str | None:
    if not res.startswith("function:"):
        return None
    return res.split(":")[1]


IDENTITY: dict[str, IdentitySpec] = {
    "aws_s3_bucket": IdentitySpec("bucket", "s3", lambda r: r.split("/", 1)[0]),
    "aws_sqs_queue": IdentitySpec("name", "sqs", lambda r: r),
    "aws_sns_topic": IdentitySpec("name", "sns", lambda r: r.split(":", 1)[0]),
    "aws_dynamodb_table": IdentitySpec("name", "dynamodb", _dynamodb),
    "aws_lambda_function": IdentitySpec("function_name", "lambda", _lambda),
    "aws_cloudwatch_event_rule": IdentitySpec("name", "events", _after("rule/")),
    "aws_iam_role": IdentitySpec("name", "iam", _after("role/")),
    "aws_iam_user": IdentitySpec("name", "iam", _after("user/")),
    "aws_iam_group": IdentitySpec("name", "iam", _after("group/")),
    "aws_iam_policy": IdentitySpec("name", "iam", _after("policy/")),
    "aws_iam_instance_profile": IdentitySpec("name", "iam", _after("instance-profile/")),
}

# IAM action prefixes that apply to a resource type (used to label ACCESS edges).
IAM_PREFIX: dict[str, tuple[str, ...]] = {
    "aws_s3_bucket": ("s3",),
    "aws_sqs_queue": ("sqs",),
    "aws_sns_topic": ("sns",),
    "aws_dynamodb_table": ("dynamodb",),
    "aws_lambda_function": ("lambda",),
    "aws_iam_role": ("iam", "sts"),
    "aws_instance": ("ec2", "ssm"),
}

RELATIONSHIP_TYPES = {
    "aws_iam_role_policy",
    "aws_iam_role_policy_attachment",
    "aws_iam_user_policy",
    "aws_iam_user_policy_attachment",
    "aws_iam_group_policy",
    "aws_iam_group_policy_attachment",
    "aws_iam_policy_attachment",
    "aws_lambda_event_source_mapping",
    "aws_lambda_permission",
    "aws_lb_listener",
    "aws_lb_target_group_attachment",
    "aws_cloudwatch_event_target",
    "aws_apigatewayv2_integration",
    "aws_api_gateway_integration",
    "aws_sns_topic_subscription",
    "aws_sns_topic_policy",
    "aws_sqs_queue_policy",
    "aws_s3_bucket_policy",
    "aws_s3_bucket_notification",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_versioning",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_security_group_rule",
    "aws_vpc_security_group_ingress_rule",
    "aws_vpc_security_group_egress_rule",
}
POLICY_TYPES = {"aws_iam_policy"}

_SERVICE_PREFIXES = (
    ("aws_instance", "ec2"),
    ("aws_ami", "ec2"),
    ("aws_ebs", "ec2"),
    ("aws_security_group", "vpc"),
    ("aws_vpc_security_group", "vpc"),
)


def service_of(rtype: str) -> str:
    for prefix, svc in _SERVICE_PREFIXES:
        if rtype.startswith(prefix):
            return svc
    parts = rtype.split("_")
    return parts[1] if len(parts) > 1 and parts[0] == "aws" else parts[0]


def pattern_of(value: Any) -> str | None:
    """Concrete string for a value, with unknown parts replaced by ``*`` (or None)."""
    v = unwrap(value)
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        return scalar_str(v)
    if isinstance(v, Interp):
        out: list[str] = []
        known = False
        for p in v.parts:
            s = pattern_of(p)
            if s is None:
                if not out or out[-1] != "*":
                    out.append("*")
            else:
                known = True
                out.append(s)
        return "".join(out) if known else None
    return None


def _why(value: Any) -> str:
    v = unwrap(value)
    if isinstance(v, Unknown):
        return v.reason
    if isinstance(v, Ref):
        return f"{v.expr()} is only known after apply"
    if v is None:
        return "value is null"
    return f"value {display(v)} is not a resolvable string"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


@dataclass
class Item:
    inst: ModuleInstance
    block: Block
    type: str
    name: str
    id: str
    data: bool = False

    @property
    def loc(self) -> Location:
        return self.block.loc


class Analyzer:
    def __init__(self, repo: RepoSource, engine: Engine) -> None:
        self.repo = repo
        self.engine = engine
        self.graph = Graph()
        self.items: list[Item] = []
        self.by_id: dict[str, Item] = {}
        # principal-bearing resource -> [(role/profile id, evidence, via)]
        self.runs_as: dict[str, list[tuple[str, list[Evidence], list[str]]]] = {}
        self._identity: dict[str, list[tuple[str, str, str]]] | None = None

    # ------------------------------------------------------------------ driver
    def run(self) -> Graph:
        from .extractors import EXTRACTORS

        for inst in self.engine.instances:
            self._collect(inst)
        for item in self.items:
            self.graph.add_node(self._node(item))
        for item in self.items:
            self._walk(item, item.block, "")
        for item in self.items:
            handler = EXTRACTORS.get((item.data, item.type))
            if handler is not None:
                handler(self, item)
        self._derive_access()
        self.graph.finalize()
        return self.graph

    def _collect(self, inst: ModuleInstance) -> None:
        if inst.module_id is not None:
            call = inst.call
            assert call is not None and inst.parent is not None
            props: dict[str, Any] = {
                "source": inst.parent.child_source.get(call.labels[0]),
                "dir": inst.src.dir,
            }
            remote = inst.parent.child_remote.get(call.labels[0])
            if remote is not None:
                props["remote_source"] = remote.source
                props["remote_version"] = remote.version
            self.graph.add_node(
                Node(
                    id=inst.module_id,
                    kind="module",
                    type="module",
                    name=inst.address.rsplit(".", 1)[-1],
                    service="terraform",
                    category="module",
                    parent=inst.parent.module_id,
                    module=inst.parent.module_id or "",
                    location=call.loc,
                    properties=props,
                )
            )
        for name in sorted(inst.child_status):
            call = inst.src.module_calls[name]
            child_id = f"{inst.prefix}{inst.address + '.' if inst.address else ''}module.{name}"
            self.unknown(child_id, "source", inst.child_status[name], call.loc)
        for data, table in ((False, inst.src.resources), (True, inst.src.data)):
            for rtype, name in sorted(table):
                item = Item(
                    inst, table[(rtype, name)], rtype, name, inst.node_id(rtype, name, data), data
                )
                self.items.append(item)
                self.by_id[item.id] = item

    def _node(self, item: Item) -> Node:
        attrs: dict[str, Any] = {}
        for name, attr in item.block.attrs.items():
            attrs[name] = to_json(item.inst.eval_attr(attr, item.id))
        for name, blocks in item.block.blocks.items():
            attrs[name] = [to_json(item.inst.eval_block(b, item.id)) for b in blocks]
        for name in item.block.dynamic:
            attrs.setdefault(
                name, {"$unknown": f"dynamic block {name!r} is not expanded statically"}
            )
        if item.data:
            category = "policy" if item.type == "aws_iam_policy_document" else "data"
        elif item.type in RELATIONSHIP_TYPES:
            category = "relationship"
        elif item.type in POLICY_TYPES:
            category = "policy"
        else:
            category = "resource"
        props: dict[str, Any] = {}
        for meta in ("count", "for_each"):
            if meta in item.block.attrs:
                props["instances"] = (
                    f"{meta} = {display(item.inst.eval_attr(item.block.attrs[meta]))}"
                )
        return Node(
            id=item.id,
            kind="data" if item.data else "resource",
            type=item.type,
            name=item.name,
            service=service_of(item.type),
            category=category,
            parent=item.inst.module_id,
            module=item.inst.module_id or "",
            location=item.loc,
            properties=props,
            attributes=attrs,
        )

    # ------------------------------------------------------------ generic walk
    def _walk(self, item: Item, block: Block, prefix: str) -> None:
        for name in sorted(block.attrs):
            if not prefix and name in _SKIP_ATTRS:
                continue
            attr = block.attrs[name]
            value = item.inst.eval_attr(attr, item.id)
            path = f"{prefix}{name}"
            for upath, unk in collect_unknowns(value, path):
                self.unknown(item.id, upath, unk.reason, attr.loc)
            self._config_refs(item, path, value, attr.loc)
        for name in sorted(block.dynamic):
            self.unknown(
                item.id, f"{prefix}{name}", "dynamic block is not expanded statically", block.loc
            )
        for name in sorted(block.blocks):
            if not prefix and name in _SKIP_ATTRS:
                continue
            children = block.blocks[name]
            for i, child in enumerate(children):
                sub = f"{prefix}{name}." if len(children) == 1 else f"{prefix}{name}[{i}]."
                self._walk(item, child, sub)

    def _config_refs(self, item: Item, path: str, value: Any, loc: Location) -> None:
        if isinstance(value, dict):
            for k in sorted(value):
                self._config_refs(item, f"{path}.{k}", value[k], loc)
            return
        if path == "container_definitions" and item.type == "aws_ecs_task_definition":
            value = self._container_env_refs(item, value, loc)
        by_target: dict[str, list[str]] = {}
        for ref in collect_refs(value):
            if ref.target != item.id and ref.target in self.graph.nodes:
                by_target.setdefault(ref.target, []).append(ref.expr())
        for target in sorted(by_target):
            attrs: dict[str, Any] = {"attribute": path, "refs": sorted(set(by_target[target]))}
            label = path
            ev_loc = loc
            if path.startswith("environment.variables."):
                env = path[len("environment.variables.") :]
                attrs["env_var"] = env
                label = f"env {env}"
                ev_loc = self.narrow(loc, env)
            self.edge("CONFIG", item.id, target, label, [self.ev(ev_loc)], attrs)

    def _container_env_refs(self, item: Item, value: Any, loc: Location) -> Any:
        """ECS ``container_definitions``: one CONFIG edge per environment variable / secret
        that references a resource (labelled like Lambda env vars); returns the rest."""
        containers = unwrap(value)
        if isinstance(containers, JsonEncoded):
            containers = unwrap(containers.value)
        if not isinstance(containers, list):
            return value
        rest: list[Any] = []
        for c in containers:
            c = unwrap(c)
            if not isinstance(c, dict):
                rest.append(c)
                continue
            for key, value_key, kind in (
                ("environment", "value", "env"),
                ("secrets", "valueFrom", "secret"),
            ):
                for entry in as_list(c.get(key)):
                    entry = unwrap(entry)
                    name = render(entry.get("name")) if isinstance(entry, dict) else None
                    if name is None:
                        rest.append(entry)
                        continue
                    targets = sorted(
                        {
                            r.target
                            for r in collect_refs(entry.get(value_key))
                            if r.target != item.id and r.target in self.graph.nodes
                        }
                    )
                    for target in targets:
                        attrs = {
                            "attribute": f"container_definitions.{key}",
                            "refs": sorted({r.expr() for r in collect_refs(entry)}),
                            "env_var": name,
                            "container": render(c.get("name")) or "?",
                        }
                        ev = [self.ev(self.narrow(loc, name))]
                        self.edge("CONFIG", item.id, target, f"{kind} {name}", ev, attrs)
            rest.append({k: v for k, v in c.items() if k not in ("environment", "secrets")})
        return rest

    # ------------------------------------------------------------------ helpers
    def get(self, item: Item, name: str) -> Any:
        if name not in item.block.attrs:
            return None
        if item.data:
            return item.inst.data_attr(item.type, item.name, name)[1]
        return item.inst.resource_attr(item.type, item.name, name)[1]

    def attr_loc(self, item: Item, name: str) -> Location:
        attr = item.block.attrs.get(name)
        return attr.loc if attr is not None else item.loc

    def nested(self, item: Item, name: str) -> list[tuple[Block, dict[str, Any]]]:
        return [(b, item.inst.eval_block(b, item.id)) for b in item.block.blocks.get(name, [])]

    def props(self, node_id: str, **values: Any) -> None:
        node = self.graph.nodes[node_id]
        for k, v in values.items():
            if v is not None:
                node.properties[k] = to_json(v)

    def narrow(self, loc: Location, key: str) -> Location:
        """The line of ``loc`` that sets ``key`` (an env var name, a map key), else ``loc``."""
        pattern = re.compile(rf'^\s*(?:\{{\s*)?(?:name\s*[=:]\s*)?"?{re.escape(key)}"?\s*[=:,]')
        lines = self.repo.lines(loc.file)
        for n in range(loc.start_line, min(loc.end_line, len(lines)) + 1):
            if pattern.search(lines[n - 1]):
                return Location(loc.file, n, n)
        return loc

    def ev(self, loc: Location, note: str = "") -> Evidence:
        return Evidence(loc.file, loc.start_line, loc.end_line, self.repo.snippet(loc), note)

    def edge(
        self,
        kind: str,
        source: str,
        target: str,
        label: str,
        evidence: list[Evidence],
        attributes: dict[str, Any] | None = None,
        via: list[str] | None = None,
    ) -> None:
        self.graph.add_edge(
            Edge(
                kind, source, target, label, dict(attributes or {}), list(evidence), list(via or [])
            )
        )

    def unknown(self, subject: str, attribute: str, reason: str, loc: Location | None) -> None:
        self.graph.add_unknown(subject, attribute, reason, loc)

    def external(self, kind: str, value: str) -> str:
        label = value
        if kind == "cidr" and value in ("0.0.0.0/0", "::/0"):
            label = f"{value} (internet)"
        elif kind == "wildcard":
            label = "* (any resource)"
        elif kind == "principal" and value == "*":
            label = "* (anyone)"
        elif kind == "aws_managed_policy":
            label = value.rsplit("/", 1)[-1]
        return self.graph.external(kind, value, label)

    # --------------------------------------------------------------- resolution
    def resolve(
        self,
        item: Item,
        value: Any,
        attr: str,
        loc: Location,
        types: set[str] | None = None,
        external: str = "arn",
        data_ok: bool = False,
    ) -> list[str]:
        """Resolve a value (or list of values) to node ids.

        Resource references win; otherwise literal names/ARNs are matched against
        the identities of resources in the same root; anything else becomes an
        external node. Values that cannot be resolved are recorded as UNKNOWN.
        """
        out: list[str] = []
        for el in as_list(value):
            ids = self._ref_targets(item, el, types, data_ok)
            if ids:
                out.extend(ids)
                continue
            pattern = pattern_of(el)
            if pattern is None:
                self.unknown(item.id, attr, _why(el), loc)
                continue
            if pattern == "*":
                out.append(self.external("wildcard" if external == "arn" else external, "*"))
                continue
            matches = self.match(item.inst.prefix, pattern, types)
            out.extend(matches or [self.external(external, pattern)])
        return _dedupe(out)

    def _ref_targets(
        self, item: Item, value: Any, types: set[str] | None, data_ok: bool
    ) -> list[str]:
        ids = []
        for ref in collect_refs(value):
            node = self.graph.nodes.get(ref.target)
            if node is None or ref.target == item.id:
                continue
            if node.kind == "data" and not data_ok:
                continue
            if types is not None and node.type not in types and node.kind != "data":
                continue
            ids.append(ref.target)
        return _dedupe(ids)

    def identities(self, prefix: str) -> list[tuple[str, str, str]]:
        if self._identity is None:
            self._identity = {}
            for item in self.items:
                spec = IDENTITY.get(item.type)
                if spec is None or item.data:
                    continue
                name = render(self.get(item, spec.attr))
                if name:
                    self._identity.setdefault(item.inst.prefix, []).append(
                        (item.type, name, item.id)
                    )
        return self._identity.get(prefix, [])

    def match(self, prefix: str, pattern: str, types: set[str] | None) -> list[str]:
        out = []
        arn = pattern.split(":", 5) if pattern.startswith("arn:") else None
        if arn is not None and len(arn) < 6:
            return []
        plain = pattern.rsplit("/", 1)[-1] if "://" in pattern else pattern
        for rtype, ident, node_id in self.identities(prefix):
            if types is not None and rtype not in types:
                continue
            spec = IDENTITY[rtype]
            if arn is not None:
                if not fnmatch.fnmatchcase(spec.service, arn[2]):
                    continue
                name = spec.extract(arn[5])
                if name and fnmatch.fnmatchcase(ident, name):
                    out.append(node_id)
            elif types is not None and fnmatch.fnmatchcase(ident, plain):
                out.append(node_id)
        return out

    # ----------------------------------------------------------------- policies
    def statements(self, holder: Item, value: Any, loc: Location) -> list[Statement]:
        sts, errors = parse_policy(value, loc)
        base = sts[0].location if sts else None
        if base is not None and all(st.location == base for st in sts):
            # jsonencode/heredoc policies: narrow each statement to its own lines
            lines = self.repo.lines(base.file)[base.start_line - 1 : base.end_line]
            spans = statement_spans(lines, base.start_line)
            if len(spans) == len(sts):
                for st, (a, b) in zip(sts, spans, strict=True):
                    st.location = Location(base.file, a, b)
        for err in errors:
            self.unknown(holder.id, "policy", err, loc)
        good = []
        for st in sts:
            for err in st.errors:
                self.unknown(holder.id, "policy.Statement", err, st.location or loc)
            if not st.errors:
                good.append(st)
        return good

    def _statement_label(self, st: Statement, target: str) -> tuple[str, list[str]]:
        actions = st.actions
        node = self.graph.nodes.get(target)
        prefixes = IAM_PREFIX.get(node.type, ()) if node else ()
        if prefixes:
            sel = [a for a in actions if a == "*" or a.split(":", 1)[0].lower() in prefixes]
            actions = sel or actions
        if st.not_actions:
            return f"{st.effect}: NOT {', '.join(st.not_actions)}", actions
        return f"{st.effect}: {', '.join(actions)}", actions

    def _statement_attrs(self, st: Statement, actions: list[str], holder: Item) -> dict[str, Any]:
        attrs: dict[str, Any] = {"effect": st.effect, "actions": actions, "policy": holder.id}
        if st.sid:
            attrs["sid"] = st.sid
        if st.not_actions:
            attrs["not_actions"] = st.not_actions
        if st.not_resources:
            attrs["not_resources"] = [display(r) for r in st.not_resources]
        if st.conditions is not None:
            attrs["conditions"] = to_json(st.conditions)
        return attrs

    def grant_identity(
        self,
        principals: list[str],
        holder: Item,
        value: Any,
        loc: Location,
        via: list[str],
        extra: list[Evidence] | None = None,
    ) -> None:
        """Identity-based policy: principals -> resources named in each statement."""
        for st in self.statements(holder, value, loc):
            st_loc = st.location or loc
            targets: list[str] = []
            for r in st.resources:
                targets.extend(self.resolve(holder, r, "policy.Resource", st_loc))
            if st.not_resources:
                targets.append(self.external("wildcard", "*"))
            for target in _dedupe(targets):
                label, actions = self._statement_label(st, target)
                attrs = self._statement_attrs(st, actions, holder)
                ev = [self.ev(st_loc, "policy statement"), *(extra or [])]
                for p in principals:
                    if p != target:
                        self.edge("ACCESS", p, target, label, ev, attrs, via)

    def grant_resource(
        self,
        resource_id: str,
        holder: Item,
        value: Any,
        loc: Location,
        via: list[str],
    ) -> None:
        """Resource-based policy (trust, bucket/queue/topic/endpoint policy): principals -> resource."""
        for st in self.statements(holder, value, loc):
            st_loc = st.location or loc
            sources: list[str] = []
            for ptype in sorted(st.principals):
                for pval in st.principals[ptype]:
                    sources.extend(self.resolve_principal(holder, ptype, pval, st_loc))
            sources.extend(self._source_arn_condition(holder, st, st_loc))
            label, actions = self._statement_label(st, resource_id)
            attrs = self._statement_attrs(st, actions, holder)
            attrs["resource_policy"] = True
            for src in _dedupe(sources):
                if src != resource_id:
                    self.edge(
                        "ACCESS",
                        src,
                        resource_id,
                        label,
                        [self.ev(st_loc, "policy statement")],
                        attrs,
                        via,
                    )

    def _source_arn_condition(self, holder: Item, st: Statement, loc: Location) -> list[str]:
        cond = unwrap(st.conditions)
        out: list[str] = []
        if not isinstance(cond, dict):
            return out
        for test in sorted(cond):
            mapping = unwrap(cond[test])
            if not isinstance(mapping, dict):
                continue
            for key in sorted(mapping):
                if key.lower().endswith("sourcearn"):
                    for v in as_list(mapping[key]):
                        ids = self._ref_targets(holder, v, None, False)
                        p = pattern_of(v)
                        if not ids and p and p.startswith("arn:"):
                            ids = self.match(holder.inst.prefix, p, None)
                        out.extend(ids)
        return out

    def resolve_principal(self, holder: Item, ptype: str, value: Any, loc: Location) -> list[str]:
        if ptype == "*":
            return [self.external("principal", "*")]
        if ptype == "AWS":
            ids = self._ref_targets(holder, value, None, False)
            if ids:
                return ids
            p = pattern_of(value)
            if p is None:
                self.unknown(holder.id, "policy.Principal", _why(value), loc)
                return []
            if p == "*":
                return [self.external("principal", "*")]
            matches = self.match(holder.inst.prefix, p, {"aws_iam_role", "aws_iam_user"})
            if matches:
                return matches
            if p.isdigit() or (p.startswith("arn:") and p.endswith(":root")):
                acct = p if p.isdigit() else p.split(":")[4]
                return [self.external("aws_account", acct)]
            return [self.external("aws_principal", p)]
        p = pattern_of(value)
        if p is None:
            self.unknown(holder.id, "policy.Principal", _why(value), loc)
            return []
        kind = {"Service": "aws_service", "Federated": "federated"}.get(ptype, ptype.lower())
        return [self.external(kind, p)]

    def attach_policy(
        self, principals: list[str], holder: Item, value: Any, loc: Location, via: list[str]
    ) -> None:
        """Managed-policy attachment: expand repository policies, reference others."""
        targets = self.resolve(
            holder, value, "policy_arn", loc, {"aws_iam_policy"}, "aws_managed_policy", data_ok=True
        )
        for t in targets:
            policy = self.by_id.get(t)
            if policy is not None and not policy.data and policy.type == "aws_iam_policy":
                self.grant_identity(
                    principals,
                    policy,
                    self.get(policy, "policy"),
                    self.attr_loc(policy, "policy"),
                    via + [t],
                    [self.ev(loc, "policy attachment")],
                )
                continue
            for p in principals:
                self.edge(
                    "ACCESS",
                    p,
                    t,
                    "attached policy (permissions not expanded: policy is not defined in this repository)",
                    [self.ev(loc, "policy attachment")],
                    {"policy_arn": pattern_of(value) or display(value)},
                    via,
                )

    # ------------------------------------------------------------ derivation
    def _roles_of(
        self, node_id: str, seen: set[str]
    ) -> list[tuple[str, list[Evidence], list[str]]]:
        out = []
        for target, ev, via in self.runs_as.get(node_id, []):
            if target in seen:
                continue
            node = self.graph.nodes.get(target)
            if node is not None and node.type == "aws_iam_role":
                out.append((target, ev, via))
            else:
                for role, ev2, via2 in self._roles_of(target, seen | {target}):
                    out.append((role, ev + ev2, via + [target] + via2))
        return out

    def _derive_access(self) -> None:
        access = [e for e in self.graph.edges if e.kind == "ACCESS"]
        by_source: dict[str, list[Edge]] = {}
        for e in access:
            by_source.setdefault(e.source, []).append(e)
        for compute in sorted(self.runs_as):
            node = self.graph.nodes.get(compute)
            if node is None or node.type in ("aws_iam_instance_profile", "aws_launch_template"):
                continue  # links to a role, not code: what runs through them gets the access
            for role, ev, via in self._roles_of(compute, {compute}):
                for e in by_source.get(role, []):
                    if e.target == compute:
                        continue
                    attrs = dict(e.attributes)
                    attrs["derived_via_role"] = role
                    self.edge(
                        "ACCESS",
                        compute,
                        e.target,
                        e.label,
                        ev + e.evidence,
                        attrs,
                        via + [role] + e.via,
                    )


def analyze(repo: RepoSource, engine: Engine) -> Graph:
    return Analyzer(repo, engine).run()
