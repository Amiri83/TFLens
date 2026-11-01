"""The Flow view: how messages and data move through a repository, entry point to sink.

Built from model.json alone (deterministic, offline, no LLM), from edges the analyzer
already records with evidence:

* **entry points** are the sources of TRIGGER edges (event source mappings, Kafka topics,
  SNS subscriptions, S3 notifications, EventBridge rules, API integrations, load
  balancer listeners) that nothing in the repository writes to or triggers;
* a compute **sends to** a queue / topic / table / bucket / stream when it has both a
  reference to it (a CONFIG edge from an environment variable) and an IAM write action
  on it (an ACCESS edge); a reference alone is **unverified**, an IAM grant alone
  **can write**. **Reads from** is the same with read actions;
* a **chain** follows entry -> compute -> what it sends to -> what that triggers -> ...,
  numbered in breadth-first order. Dead-letter queues, alarms and log groups are side
  branches, never chain steps;
* **every compute gets a flow**: one that no chain from an entry point reaches starts its
  own chain (marked *no trigger*), so the flows cover all the code, each compute once:
  a compute is expanded in one flow only, every other flow that reaches it ends in a
  *continues in Flow N* link to that one;
* what a compute runs in and as (ECS cluster, task definition, containers, task /
  execution role, instance profile, launch template) is its **context**: boxes hung
  under it, never chain steps.

Every step carries the ``file:line`` of each piece of evidence it rests on, and every node
a ``code_ref`` (its own block, else the first evidence that names it).
"""

from __future__ import annotations

import fnmatch
from collections import deque
from typing import Any
from xml.sax.saxutils import escape

from .overview import FRIENDLY
from .snippets import location_label

# Resources that run code: the middle of a chain. A task definition is folded into the
# ECS service that runs it (its environment and task role are the service's).
COMPUTE_TYPES = frozenset(
    {
        "aws_lambda_function",
        "aws_ecs_service",
        "aws_instance",
        "aws_autoscaling_group",
        "aws_sfn_state_machine",
        "aws_batch_job_definition",
        "aws_glue_job",
    }
)
# Where data goes, with the direction a bare reference is taken to mean.
TARGET_TYPES: dict[str, str] = {
    "aws_sqs_queue": "send",
    "aws_sns_topic": "send",
    "aws_dynamodb_table": "send",
    "aws_s3_bucket": "send",
    "aws_kinesis_stream": "send",
    "aws_kinesis_firehose_delivery_stream": "send",
    "aws_cloudwatch_event_bus": "send",
    "aws_msk_cluster": "send",
    "aws_secretsmanager_secret": "read",
    "aws_ssm_parameter": "read",
}
WRITE_ACTIONS = (
    "sqs:SendMessage",
    "sqs:SendMessageBatch",
    "sns:Publish",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:DeleteItem",
    "dynamodb:TransactWriteItems",
    "s3:PutObject",
    "s3:DeleteObject",
    "kinesis:PutRecord",
    "kinesis:PutRecords",
    "firehose:PutRecord",
    "firehose:PutRecordBatch",
    "events:PutEvents",
    "kafka-cluster:WriteData",
    "secretsmanager:PutSecretValue",
    "ssm:PutParameter",
)
READ_ACTIONS = (
    "sqs:ReceiveMessage",
    "sns:Subscribe",
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    "s3:GetObject",
    "s3:ListBucket",
    "kinesis:GetRecords",
    "kinesis:GetShardIterator",
    "kafka-cluster:ReadData",
    "secretsmanager:GetSecretValue",
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
)
ROUTING_TYPES = frozenset({"aws_lb", "aws_alb", "aws_lb_target_group", "aws_alb_target_group"})
KIND_LABELS = {
    "aws_ecs_task_definition": "ECS task definition",
    "aws_lb_target_group": "target group",
    "aws_alb_target_group": "target group",
    "aws_alb": "load balancer",
    "aws_cloudwatch_log_group": "log group",
    "aws_cloudwatch_metric_alarm": "CloudWatch alarm",
    "aws_kinesis_firehose_delivery_stream": "Firehose stream",
    "aws_cloudwatch_event_bus": "EventBridge bus",
    "aws_msk_cluster": "MSK cluster",
    "aws_ssm_parameter": "SSM parameter",
    "aws_batch_job_definition": "Batch job",
    "aws_glue_job": "Glue job",
    "aws_launch_template": "launch template",
    "aws_iam_instance_profile": "instance profile",
    "aws_iam_group": "IAM group",
    "aws_iam_policy": "IAM policy",
    "aws_iam_role_policy": "inline role policy",
    "aws_iam_user_policy": "inline user policy",
    "aws_iam_group_policy": "inline group policy",
    # external nodes (by external kind)
    "kafka_topic": "Kafka topic",
    "aws_service": "AWS service",
    "aws_managed_policy": "AWS managed policy",
}
# verb of a link: (kind, status) -> text
VERBS = {
    ("send", "verified"): "sends to",
    ("send", "unverified"): "sends to (unverified)",
    ("send", "iam_only"): "can write",
    ("read", "verified"): "reads from",
    ("read", "unverified"): "reads from (unverified)",
    ("read", "iam_only"): "can read",
}
# Flow groups of the Flow tab, by the type of the first compute a chain reaches.
GROUP_ORDER = ("Lambda", "ECS", "EC2", "Network", "Other")
GROUP_OF_TYPE = {
    "aws_lambda_function": "Lambda",
    "aws_ecs_service": "ECS",
    "aws_instance": "EC2",
    "aws_autoscaling_group": "EC2",
}
# What a compute runs in / as: holder type -> ((CONFIG attribute, relation), ...).
CONTEXT_ATTRS: dict[str, tuple[tuple[str, str], ...]] = {
    "aws_lambda_function": (("role", "execution role"),),
    "aws_ecs_service": (("cluster", "cluster"),),
    "aws_ecs_task_definition": (
        ("task_role_arn", "task role"),
        ("execution_role_arn", "execution role"),
    ),
    "aws_instance": (("iam_instance_profile", "instance profile"),),
    "aws_autoscaling_group": (
        ("launch_template.id", "launch template"),
        ("launch_template.name", "launch template"),
    ),
    "aws_launch_template": (
        ("iam_instance_profile.name", "instance profile"),
        ("iam_instance_profile.arn", "instance profile"),
    ),
    "aws_iam_instance_profile": (("role", "IAM role"),),
}
_KIND_ORDER = {"trigger": 0, "send": 1, "read": 2}
_STATUS_ORDER = {"": 0, "verified": 0, "unverified": 1, "iam_only": 2}
_DLQ_ATTRS = ("redrive_policy", "dead_letter", "on_failure")


def _loc(ev: dict[str, Any] | None) -> dict[str, Any] | None:
    return {k: ev[k] for k in ("file", "start_line", "end_line")} if ev else None


def label_of(n: dict[str, Any]) -> str:
    """The name people use for a node (function name, bucket, queue name, ...)."""
    if n["kind"] == "external":
        return n["name"]
    for src in (n["properties"], n["attributes"]):
        for key in ("function_name", "bucket", "name", "alarm_name", "family"):
            if isinstance(src.get(key), str) and src[key]:
                return src[key]
    return n["name"]


def kind_of(n: dict[str, Any]) -> str:
    if n["kind"] == "external":
        return f"{KIND_LABELS.get(n['type'], n['type'].replace('_', ' '))}, external"
    if n["type"] in FRIENDLY:
        return FRIENDLY[n["type"]][0]
    return KIND_LABELS.get(n["type"], n["type"])


def _granted(edge: dict[str, Any], wanted: tuple[str, ...]) -> list[str]:
    """The actions of an Allow ACCESS edge that grant any of ``wanted`` (wildcards too)."""
    attrs = edge["attributes"]
    if attrs.get("effect") != "Allow" or attrs.get("not_actions"):
        return []
    actions = attrs.get("actions")
    if not isinstance(actions, list):
        return []
    want = [w.lower() for w in wanted]
    return [
        a
        for a in actions
        if isinstance(a, str) and any(fnmatch.fnmatchcase(w, a.lower()) for w in want)
    ]


def _statement(edge: dict[str, Any]) -> dict[str, Any] | None:
    """The policy statement an ACCESS edge rests on (its last evidence otherwise)."""
    evs = edge["evidence"]
    return next((e for e in evs if e.get("note") == "policy statement"), evs[-1] if evs else None)


class _Flows:
    def __init__(self, model: dict[str, Any]) -> None:
        self.model = model
        self.nodes = {n["id"]: n for n in model["nodes"]}
        self.out: dict[str, list[dict[str, Any]]] = {}
        self.ins: dict[str, list[dict[str, Any]]] = {}
        for e in model["edges"]:
            self.out.setdefault(e["source"], []).append(e)
            self.ins.setdefault(e["target"], []).append(e)
        self._links: dict[str, list[dict[str, Any]]] = {}

    def type_of(self, nid: str) -> str:
        n = self.nodes.get(nid)
        return n["type"] if n else ""

    def name(self, nid: str) -> str:
        n = self.nodes.get(nid)
        return label_of(n) if n else nid

    def edges(self, nid: str, kind: str, out: bool = True) -> list[dict[str, Any]]:
        table = self.out if out else self.ins
        return [e for e in table.get(nid, []) if e["kind"] == kind]

    def is_compute(self, nid: str) -> bool:
        return self.type_of(nid) in COMPUTE_TYPES

    def members(self, compute: str) -> list[str]:
        """The compute and what it runs as code (an ECS service's task definitions)."""
        out = [compute]
        if self.type_of(compute) == "aws_ecs_service":
            out += [
                e["target"]
                for e in self.edges(compute, "CONFIG")
                if self.type_of(e["target"]) == "aws_ecs_task_definition"
            ]
        return out

    # ------------------------------------------------------------- links
    def links(self, compute: str) -> list[dict[str, Any]]:
        """sends to / reads from links of a compute, with their tri-state verification."""
        if compute in self._links:
            return self._links[compute]
        members = self.members(compute)
        refs: dict[str, list[dict[str, Any]]] = {}
        access: dict[str, list[dict[str, Any]]] = {}
        for m in members:
            for e in self.edges(m, "CONFIG"):
                if e["attributes"].get("env_var") and self.type_of(e["target"]) in TARGET_TYPES:
                    refs.setdefault(e["target"], []).append(e)
        for e in self.edges(compute, "ACCESS"):
            if self.type_of(e["target"]) in TARGET_TYPES:
                access.setdefault(e["target"], []).append(e)
        triggers = {e["source"] for e in self.edges(compute, "TRIGGER", out=False)}
        links = []
        for target in sorted(set(refs) | set(access)):
            ref = refs.get(target, [])
            writes = [(e, a) for e in access.get(target, []) if (a := _granted(e, WRITE_ACTIONS))]
            reads = [(e, a) for e in access.get(target, []) if (a := _granted(e, READ_ACTIONS))]
            if not ref and target in triggers:
                continue  # the queue / stream that triggers it: already the step before
            # both granted: a send link and a read link (two parallel edges in the diagram)
            status = "verified" if ref else "iam_only"
            grants = [("send", writes), ("read", reads)]
            kinds = [(kind, status, iam) for kind, iam in grants if iam]
            if ref and not kinds:
                kinds = [(TARGET_TYPES[self.type_of(target)], "unverified", [])]
            for kind, status, iam in kinds:  # none: neither read nor write actions
                evidence = []
                for e in ref:
                    ev = e["evidence"][0] if e["evidence"] else None
                    evidence.append(
                        {
                            "what": e["label"],  # env NAME (or secret NAME: ECS secrets)
                            "location": _loc(ev),
                            "edge": e["id"],
                        }
                    )
                for e, actions in iam:
                    evidence.append(
                        {
                            "what": f"IAM {', '.join(actions)}",
                            "location": _loc(_statement(e)),
                            "edge": e["id"],
                        }
                    )
                links.append(
                    {
                        "kind": kind,
                        "status": status,
                        "source": compute,
                        "target": target,
                        "evidence": evidence,
                    }
                )
        links.sort(
            key=lambda lk: (
                _KIND_ORDER[lk["kind"]],
                _STATUS_ORDER[lk["status"]],
                self.name(lk["target"]),
            )
        )
        self._links[compute] = links
        return links

    def computes(self) -> list[str]:
        return sorted(n for n in self.nodes if self.is_compute(n))

    def written(self) -> set[str]:
        return {
            lk["target"] for c in self.computes() for lk in self.links(c) if lk["kind"] == "send"
        }

    # ------------------------------------------------------------- entries
    def trigger_sources(self) -> list[str]:
        sources = {
            e["source"]
            for e in self.model["edges"]
            if e["kind"] == "TRIGGER" and e["source"] in self.nodes
        }
        return sorted(sources, key=lambda s: (self.name(s), s))

    def entries(self) -> list[str]:
        """Trigger sources that nothing in the repository triggers or writes to."""
        triggered = {e["target"] for e in self.model["edges"] if e["kind"] == "TRIGGER"}
        written = self.written()
        return [
            s
            for s in self.trigger_sources()
            if s not in triggered and s not in written and not self.is_compute(s)
        ]

    # ------------------------------------------------------------- chains
    def chain(self, entry: str, owned: dict[str, str] | None = None) -> dict[str, Any]:
        """Breadth-first from ``entry``. A compute in ``owned`` (compute -> the entry of the
        flow that expands it) is a terminal link here: reached, never expanded."""
        owned = owned or {}
        steps: list[dict[str, Any]] = []
        depth = {entry: 0}
        links: dict[str, str] = {}
        queue = deque([entry])
        while queue:
            node = queue.popleft()
            if self.is_compute(node):
                moves = [
                    (lk["kind"], lk["status"], lk["target"], lk, None) for lk in self.links(node)
                ]
            else:
                moves = [
                    ("trigger", "", e["target"], None, e)
                    for e in sorted(
                        self.edges(node, "TRIGGER"), key=lambda e: (self.name(e["target"]), e["id"])
                    )
                    if e["target"] in self.nodes
                ]
            for kind, status, target, link, edge in moves:
                step = self._step(len(steps) + 1, node, target, kind, status, link, edge)
                steps.append(step)
                if target in depth:
                    continue
                depth[target] = depth[node] + 1
                if owned.get(target, entry) != entry:  # expanded in another flow
                    links[target] = owned[target]
                elif kind != "read":  # what it reads is a sink of this chain
                    queue.append(target)
        main = [nid for nid in depth if nid not in links]
        return {
            "entry": entry,
            "steps": steps,
            "depth": depth,
            "links": links,
            "side": self.side(main),
            "context": self.context(main),
        }

    def _step(
        self,
        n: int,
        source: str,
        target: str,
        kind: str,
        status: str,
        link: dict[str, Any] | None,
        edge: dict[str, Any] | None,
    ) -> dict[str, Any]:
        src, dst = self.name(source), self.name(target)
        note = ""
        if edge is not None:
            verb = "routes to" if self.type_of(source) in ROUTING_TYPES else "triggers"
            ev = edge["evidence"][0] if edge["evidence"] else None
            evidence = [{"what": edge["label"], "location": _loc(ev), "edge": edge["id"]}]
            ext = " (external)" if self.nodes[source]["kind"] == "external" else ""
            summary = f"{src}{ext} {verb} {dst}"
            detail = f"{edge['label']} {location_label(_loc(ev))}" if ev else edge["label"]
        else:
            assert link is not None
            verb = VERBS[(kind, status)]
            evidence = link["evidence"]
            base, _, suffix = verb.partition(" (")
            summary = f"{src} {base} {dst}" + (f", {suffix.rstrip(')')}" if suffix else "")
            detail = " + ".join(f"{e['what']} {location_label(e['location'])}" for e in evidence)
            if status == "unverified":
                note = f"no IAM {'write' if kind == 'send' else 'read'} action found"
            elif status == "iam_only":
                note = "no reference to it in its configuration"
            detail += f"; {note}" if note else ""
        return {
            "n": n,
            "kind": kind,
            "status": status,
            "verb": verb,
            "source": source,
            "target": target,
            "summary": summary,
            "evidence": evidence,
            "note": note,
            "text": f"{n}. {summary} ({detail})",
        }

    # ------------------------------------------------------------- side branches
    def side(self, main: list[str]) -> list[dict[str, Any]]:
        """Dead-letter queues, alarms and log groups of the chain's nodes."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, parent: str, node: str, text: str, loc: dict[str, Any] | None) -> None:
            if (parent, node) in seen or node in main:
                return
            seen.add((parent, node))
            out.append(
                {"kind": kind, "parent": parent, "node": node, "text": text, "location": loc}
            )

        queue = list(main)
        i = 0
        while i < len(queue):
            nid = queue[i]
            i += 1
            n = self.nodes.get(nid)
            if n is None or n["kind"] == "external":
                continue
            holders = [nid, *self.members(nid)[1:]] if self.is_compute(nid) else [nid]
            # an event source mapping's on_failure destination is a DLQ of its function
            holders += [
                v
                for e in self.edges(nid, "TRIGGER", out=False)
                for v in e["via"]
                if v in self.nodes
            ]
            for h in holders:
                for e in self.edges(h, "CONFIG"):
                    if any(k in e["attributes"].get("attribute", "") for k in _DLQ_ATTRS):
                        add(
                            "dlq",
                            nid,
                            e["target"],
                            f"{self.name(nid)} dead-letter queue: {self.name(e['target'])}",
                            _loc(e["evidence"][0] if e["evidence"] else None),
                        )
                        queue.append(e["target"])
                    elif self.type_of(e["target"]) == "aws_cloudwatch_log_group":
                        add(
                            "logs",
                            nid,
                            e["target"],
                            f"{self.name(nid)} logs to {self.name(e['target'])}",
                            _loc(e["evidence"][0] if e["evidence"] else None),
                        )
            for e in self.edges(nid, "CONFIG", out=False):
                if self.type_of(e["source"]) == "aws_cloudwatch_metric_alarm":
                    add(
                        "alarm",
                        nid,
                        e["source"],
                        f"{self.name(e['source'])} alarms on {self.name(nid)}",
                        _loc(e["evidence"][0] if e["evidence"] else None),
                    )
            if n["type"] == "aws_lambda_function":
                fn = self.name(nid)
                for lg in self.nodes.values():
                    if (
                        lg["type"] == "aws_cloudwatch_log_group"
                        and lg["attributes"].get("name") == f"/aws/lambda/{fn}"
                        and lg["module"] == n["module"]
                    ):
                        add("logs", nid, lg["id"], f"{fn} logs to {label_of(lg)}", lg["location"])
        return out

    # ------------------------------------------------------------- context
    def context(self, main: list[str]) -> list[dict[str, Any]]:
        """What the chain's computes run in and as: ECS cluster, task definitions and
        their containers, task / execution roles, instance profiles, launch templates."""
        out: list[dict[str, Any]] = []
        seen = set(main)

        def add(parent: str, node: str, relation: str, loc: dict[str, Any] | None, **extra: Any):
            if node in seen:
                return False
            seen.add(node)
            out.append(
                {"parent": parent, "node": node, "relation": relation, "location": loc, **extra}
            )
            return True

        def by_attribute(holder: str) -> None:
            attrs = dict(CONTEXT_ATTRS.get(self.type_of(holder), ()))
            for e in sorted(self.edges(holder, "CONFIG"), key=lambda e: (e["target"], e["id"])):
                relation = attrs.get(e["attributes"].get("attribute", ""))
                if relation and e["target"] in self.nodes:
                    loc = _loc(e["evidence"][0] if e["evidence"] else None)
                    if add(holder, e["target"], relation, loc):
                        walk(e["target"])

        def walk(holder: str) -> None:
            """cluster -> task definition -> containers -> task / execution role;
            instance (or ASG -> launch template) -> instance profile -> role."""
            if self.type_of(holder) == "aws_ecs_task_definition":
                for name, image in self.containers(holder):
                    add(
                        holder,
                        f"{holder}#container/{name}",
                        "container",
                        self.nodes[holder]["location"],
                        label=name,
                        image=image,
                    )
            by_attribute(holder)
            if self.type_of(holder) == "aws_ecs_service":
                for e in self.edges(holder, "CONFIG"):
                    if self.type_of(e["target"]) == "aws_ecs_task_definition":
                        loc = _loc(e["evidence"][0] if e["evidence"] else None)
                        if add(holder, e["target"], "task definition", loc):
                            walk(e["target"])

        for nid in main:
            if self.is_compute(nid):
                walk(nid)
        return out

    def containers(self, task_def: str) -> list[tuple[str, str]]:
        """(name, image) of each container of a task definition's container_definitions."""
        value = self.nodes[task_def]["attributes"].get("container_definitions")
        if isinstance(value, dict) and "$json" in value:
            value = value["$json"]
        out = []
        for c in value if isinstance(value, list) else []:
            if isinstance(c, dict) and isinstance(c.get("name"), str):
                image = c.get("image")
                out.append((c["name"], image if isinstance(image, str) else ""))
        return out

    def code_ref(self, nid: str, steps: list[dict[str, Any]]) -> dict[str, Any] | None:
        """A node's own block, else the first evidence of a step that names it."""
        n = self.nodes.get(nid)
        if n is not None and n["location"]:
            return n["location"]
        for s in steps:
            if nid in (s["source"], s["target"]):
                for e in s["evidence"]:
                    if e["location"]:
                        return e["location"]
        return None

    def group(self, depth: dict[str, int]) -> str:
        """The Flow tab group of a chain: the type of the first compute it reaches."""
        first = next((nid for nid in depth if self.is_compute(nid)), None)
        if first is None:
            return "Network"
        return GROUP_OF_TYPE.get(self.type_of(first), "Other")


def _chain_title(f: _Flows, chain: dict[str, Any]) -> str:
    """entry -> ... along the first path to a sink (the main line of the diagram)."""
    path = [chain["entry"]]
    while True:
        nxt = next(
            (
                s["target"]
                for s in chain["steps"]
                if s["source"] == path[-1] and s["kind"] != "read" and s["target"] not in path
            ),
            None,
        )
        if nxt is None:
            break
        path.append(nxt)
    return " → ".join(f.name(p) for p in path)


def build_flows(model: dict[str, Any]) -> dict[str, Any]:
    """Entry points, numbered chains with evidence, side branches, start-here, metrics."""
    f = _Flows(model)
    chains = []
    reached: set[str] = set()
    for entry in f.entries():
        c = f.chain(entry)
        reached |= set(c["depth"])
        chains.append(c)
    # a trigger source only reachable through a loop (something in the repository can
    # write to it, but nothing reached does), then every compute no chain reaches (code
    # that nothing declared starts, whether it sends somewhere or not): each its own
    # chain, so every compute is in exactly one chain as a start or a later step
    late = [s for s in f.trigger_sources() if not f.is_compute(s)]
    late += f.computes()
    for start in late:
        if start not in reached:
            c = f.chain(start)
            reached |= set(c["depth"])
            chains.append(c)

    def order(c: dict[str, Any]) -> tuple[int, str, str]:
        return -len(c["steps"]), f.name(c["entry"]), c["entry"]

    # each compute is expanded in exactly one flow: its own when it starts one, else the
    # first (declared triggers before no-trigger, then longest first) to reach it; every
    # other flow stops there with a "continues in Flow N" link
    owner = {c["entry"]: c["entry"] for c in chains if f.is_compute(c["entry"])}
    deduped = []
    for c in sorted(chains, key=lambda c: (f.is_compute(c["entry"]), *order(c))):
        c = f.chain(c["entry"], owner)
        for nid in c["depth"]:
            if f.is_compute(nid) and nid not in c["links"]:
                owner.setdefault(nid, c["entry"])
        deduped.append(c)
    # the longest pipeline first: that is where to start reading
    chains = sorted(deduped, key=order)
    n_of = {c["entry"]: i for i, c in enumerate(chains, 1)}
    out_chains = []
    for i, c in enumerate(chains, 1):
        entry = f.nodes[c["entry"]]
        no_trigger = f.is_compute(c["entry"])
        nodes = [
            {
                "id": nid,
                "label": f.name(nid),
                "kind_label": kind_of(f.nodes[nid]),
                "depth": d,
                "location": f.nodes[nid]["location"],
                "role": (
                    ("no_trigger" if no_trigger else "entry")
                    if d == 0
                    else ("compute" if f.is_compute(nid) else "target")
                ),
            }
            for nid, d in c["depth"].items()
        ]
        for n in nodes:
            if n["id"] in c["links"]:  # expanded in its own flow: a link to it here
                k = n_of[c["links"][n["id"]]]
                n.update(
                    label=f"continues in Flow {k}",
                    kind_label=f"{n['label']} ({n['kind_label']})",
                    role="link",
                    continues={"flow": f"flow-{k}", "n": k, "compute": n["label"]},
                )
        for s in c["side"]:
            n = f.nodes[s["node"]]
            nodes.append(
                {
                    "id": s["node"],
                    "label": label_of(n),
                    "kind_label": {"dlq": "dead-letter queue"}.get(s["kind"], kind_of(n)),
                    "depth": None,
                    "location": n["location"],
                    "role": "side",
                    "parent": s["parent"],
                    "side_kind": s["kind"],
                }
            )
        for x in c["context"]:
            n = f.nodes.get(x["node"])
            if n is None:  # a container: part of its task definition's block
                label, kind = x["label"], "container" + (f", {x['image']}" if x["image"] else "")
            else:
                label, kind = label_of(n), kind_of(n)
                if x["relation"].lower() != kind.lower():
                    kind = f"{x['relation']}: {kind}"
            nodes.append(
                {
                    "id": x["node"],
                    "label": label,
                    "kind_label": kind,
                    "depth": None,
                    "location": n["location"] if n else x["location"],
                    "role": "context",
                    "parent": x["parent"],
                    "relation": x["relation"],
                }
            )
        for n in nodes:
            n["code_ref"] = n["location"] or f.code_ref(n["id"], c["steps"])
        title = _chain_title(f, c)
        out_chains.append(
            {
                "id": f"flow-{i}",
                "n": i,
                "title": f"{title} (no trigger)" if no_trigger else title,
                "entry": c["entry"],
                "entry_label": f.name(c["entry"]),
                "entry_kind": "compute without a declared trigger"
                if no_trigger
                else kind_of(entry),
                "no_trigger": no_trigger,
                "group": f.group(c["depth"]),
                "steps": c["steps"],
                "side": c["side"],
                "context": c["context"],
                "nodes": nodes,
            }
        )
    steps = [s for c in out_chains for s in c["steps"]]
    covered = {
        nid for c in chains for nid in c["depth"] if f.is_compute(nid) and nid not in c["links"]
    }
    metrics = {
        "flows": len(out_chains),
        "entry_points": len(out_chains),
        "steps": len(steps),
        "unverified": sum(1 for s in steps if s["status"] == "unverified"),
        "iam_only": sum(1 for s in steps if s["status"] == "iam_only"),
        "side_branches": sum(len(c["side"]) for c in out_chains),
        "compute_covered": len(covered),
        "compute_total": len(f.computes()),
        "no_trigger": sum(1 for c in out_chains if c["no_trigger"]),
    }
    return {
        "chains": out_chains,
        "groups": flow_groups(out_chains),
        "metrics": metrics,
        "start_here": start_here(model, f, out_chains),
    }


def flow_groups(chains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chains under the Flow tab's headings (Lambda, ECS, EC2, Network, Other), in
    that order, empty groups left out; each keeps the chains' own order."""
    return [
        {"group": g, "flows": [c["id"] for c in chains if c["group"] == g]}
        for g in GROUP_ORDER
        if any(c["group"] == g for c in chains)
    ]


def _line(text: str, loc: dict[str, Any] | None, level: int = 0, node: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"text": text, "location": loc, "level": level}
    if node:
        out["node"] = node
    return out


def start_here(
    model: dict[str, Any], f: _Flows, chains: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Where to start reading: the root(s), the entry files, then modules and files in
    the order the flows go through them."""
    roots = (model.get("structure") or {}).get("roots", [])
    lines = []
    for r in roots:
        loc = next((s["location"] for s in r["signals"] if s["location"]), None)
        kind = "Terragrunt unit" if r["kind"] == "terragrunt" else f"{r['kind']} root"
        lines.append(_line(f"Root directory: {r['dir']}/ ({kind})", loc))
    if not chains:
        lines.append(
            _line(
                "No entry point found: nothing declares a trigger (event source mapping, "
                "subscription, notification, rule, API, load balancer) for the code here",
                None,
            )
        )
        return lines
    lines.append(_line("Entry files:", None))
    for c in chains:
        first = c["steps"][0] if c["steps"] else None
        loc = first["evidence"][0]["location"] if first and first["evidence"] else None
        if c["no_trigger"]:  # nothing starts it: the compute's own block is the entry
            loc = f.code_ref(c["entry"], c["steps"])
        lines.append(
            _line(
                f"{loc['file'] if loc else '?'} — flow {c['n']}: {c['entry_label']} ({c['entry_kind']})",
                loc,
                1,
                c["entry"],
            )
        )
    modules: list[str] = []
    files: dict[str, dict[str, Any]] = {}
    for c in chains:
        for s in c["steps"]:
            for nid in (s["source"], s["target"]):
                n = f.nodes[nid]
                if n["kind"] != "external" and n["module"] not in modules:
                    modules.append(n["module"])
            for e in s["evidence"]:
                if e["location"]:
                    files.setdefault(e["location"]["file"], e["location"])
            loc = f.nodes[s["target"]]["location"]
            if loc:
                files.setdefault(loc["file"], loc)
    lines.append(_line("Modules in flow order:", None))
    for m in modules:
        n = f.nodes.get(m)
        lines.append(_line(m or "root module", n["location"] if n else None, 1))
    lines.append(_line("Files in flow order:", None))
    for path, loc in files.items():
        lines.append(
            _line(
                path,
                {"file": path, "start_line": loc["start_line"], "end_line": loc["start_line"]},
                1,
            )
        )
    return lines


def metrics_text(m: dict[str, Any]) -> str:
    def p(n: int, w: str) -> str:
        return f"{n} {w if n == 1 else w + 's'}"

    return (
        f"{p(m['flows'], 'flow')} found · {p(m['steps'], 'step')} · "
        f"{p(m['unverified'], 'unverified link')} · {m['iam_only']} IAM-only"
    )


def coverage_text(m: dict[str, Any]) -> str:
    text = f"{m['compute_covered']} of {m['compute_total']} compute resources covered"
    if m["no_trigger"]:
        text += f" ({m['no_trigger']} without a declared trigger)"
    return text


# ------------------------------------------------------------------ markdown


def render_flows_markdown(flows: dict[str, Any], level: int = 1) -> str:
    h = "#" * level
    out = [f"{h} Flows", "", metrics_text(flows["metrics"]) + ".", ""]
    if flows["metrics"]["compute_total"]:
        out += [coverage_text(flows["metrics"]) + ".", ""]
    if flows["groups"]:
        n_of = {c["id"]: c["n"] for c in flows["chains"]}
        out += [
            "By type: "
            + "; ".join(
                f"{g['group']}: " + ", ".join(str(n_of[i]) for i in g["flows"])
                for g in flows["groups"]
            )
            + ".",
            "",
        ]
    out += [f"{h}# Start here", ""]
    for ln in flows["start_here"]:
        loc = f" (`{location_label(ln['location'])}`)" if ln["location"] else ""
        out.append(f"{'  ' * ln['level']}- {ln['text']}{loc}")
    out.append("")
    for c in flows["chains"]:
        out += [f"{h}# Flow {c['n']}: {c['title']}", ""]
        out += [s["text"] for s in c["steps"]] or ["(no steps)"]
        out.append("")
        out += [
            f"- {n['continues']['compute']}: {n['label']}"
            + (f" (`{location_label(n['code_ref'])}`)" if n["code_ref"] else "")
            for n in c["nodes"]
            if n["role"] == "link"
        ]
        if any(n["role"] == "link" for n in c["nodes"]):
            out.append("")
        context = [n for n in c["nodes"] if n["role"] == "context"]
        if context:
            labels = {n["id"]: n["label"] for n in c["nodes"]}
            out += ["Runs in / as:", ""]
            out += [
                f"- {labels.get(n['parent'], n['parent'])} {n['relation']}: {n['label']}"
                + (f" (`{location_label(n['code_ref'])}`)" if n["code_ref"] else "")
                for n in context
            ]
            out.append("")
        if c["side"]:
            out += ["Side branches:", ""]
            out += [
                f"- {s['text']}"
                + (f" (`{location_label(s['location'])}`)" if s["location"] else "")
                for s in c["side"]
            ]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------ diagram

FONT = "Helvetica, Arial, sans-serif"
NODE_W, NODE_H, SIDE_H = 240, 80, 72
COL_GAP, ROW_GAP, PAD = 210, 26, 24
META_H = 22  # a box's summary line (the Network diagram's "subnet-a / sg-a, sg-b")
FRAME_HEAD, FRAME_PAD = 48, 12  # a frame's name and code reference; its margin
FRAME_FILL = ("#f6f8fa", "#ffffff")  # by nesting level: VPC, subnet
# where a step's label may sit along its own edge, best first
LABEL_TS = (0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82)
STYLE = {  # (kind, status) -> colour, dash
    ("trigger", ""): ("#bc4c00", ""),
    ("send", "verified"): ("#1a7f37", ""),
    ("send", "unverified"): ("#9a6700", "8 5"),
    ("send", "iam_only"): ("#57606a", "2 4"),
    ("read", "verified"): ("#0969da", ""),
    ("read", "unverified"): ("#9a6700", "8 5"),
    ("read", "iam_only"): ("#57606a", "2 4"),
    # the Network and IAM tab diagrams (tflens.tabdiagrams)
    ("contains", ""): ("#8c959f", "4 3"),
    ("rule", ""): ("#bc4c00", ""),
    ("rule", "public"): ("#cf222e", ""),
    ("access", "iam_only"): ("#57606a", "2 4"),
    ("deny", ""): ("#cf222e", "6 3"),
}
ROLE_STROKE = {
    "entry": "#bc4c00",
    "no_trigger": "#8250df",
    "compute": "#8250df",
    "link": "#8250df",
    "target": "#1a7f37",
    "side": "#8c959f",
    "context": "#8250df",
    # the Network and IAM tab diagrams
    "network": "#0969da",
    "guard": "#bc4c00",
    "principal": "#8250df",
    "policy": "#9a6700",
}
FLOW_LEGEND = (
    ("triggers / routes to", "trigger", ""),
    ("sends to", "send", "verified"),
    ("reads from", "read", "verified"),
    ("unverified", "send", "unverified"),
    ("IAM only", "send", "iam_only"),
)


def _w(text: str, size: int) -> float:
    return len(text) * 0.6 * size


def _cut(text: str, width: float, size: int) -> str:
    n = int(width / (0.6 * size))
    return text if len(text) <= n else text[: max(n - 1, 1)] + "…"


def _t(
    x: float, y: float, text: str, size: int = 14, fill: str = "#24292f", extra: str = ""
) -> str:
    return f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}"{extra}>{escape(text)}</text>'


def _bezier(p: tuple[float, ...], t: float) -> tuple[float, float]:
    """The point at ``t`` of the cubic ``x1, y1, c1x, c1y, c2x, c2y, x2, y2``."""
    x1, y1, ax, ay, bx, by, x2, y2 = p
    u = 1 - t
    return (
        u**3 * x1 + 3 * u * u * t * ax + 3 * u * t * t * bx + t**3 * x2,
        u**3 * y1 + 3 * u * u * t * ay + 3 * u * t * t * by + t**3 * y2,
    )


def _free(p: tuple[float, float], w: float, h: float, taken: list[tuple[float, ...]]) -> bool:
    """Is a ``w`` x ``h`` label at ``p`` clear of every (left, right, top, bottom)?"""
    x, y = p
    return not any(x < b[1] and b[0] < x + w and y < b[3] and b[2] < y + h for b in taken)


def _node_h(n: dict[str, Any]) -> float:
    return NODE_H + META_H if n.get("meta") else NODE_H


def _col_x(depth: int) -> float:
    return PAD + depth * (NODE_W + COL_GAP)


def framed_layout(
    chain: dict[str, Any], main: list[dict[str, Any]], top: float
) -> tuple[dict[str, tuple[float, float, float, float]], list[tuple[Any, ...]], float]:
    """Box positions for a diagram drawn in frames (the Network diagram's VPCs and
    subnets): each top-level frame is a band across the columns it spans, its nodes
    stacked in their columns and its child frames nested below them; the nodes in no
    frame follow, except those of the leftmost column (what is outside), each lined up
    with what it connects to. Returns the positions, the frames (frame, level, x, y, w, h;
    outer first) and the bottom."""
    ids = {f["id"] for f in chain["frames"]}
    kids: dict[str | None, list[dict[str, Any]]] = {}
    for f in chain["frames"]:
        kids.setdefault(f.get("parent") if f.get("parent") in ids else None, []).append(f)
    members: dict[str | None, list[dict[str, Any]]] = {}
    for n in main:
        members.setdefault(n.get("frame") if n.get("frame") in ids else None, []).append(n)
    pos: dict[str, tuple[float, float, float, float]] = {}
    rects: list[tuple[Any, ...]] = []

    def span(f: dict[str, Any]) -> list[int]:
        ds = [*f["columns"], *(n["depth"] for n in members.get(f["id"], []))]
        for k in kids.get(f["id"], []):
            ds += span(k)
        return [min(ds), max(ds)]

    def nesting(f: dict[str, Any]) -> int:
        return 1 + max((nesting(k) for k in kids.get(f["id"], [])), default=0)

    def stack(nodes: list[dict[str, Any]], col_y: dict[int, float]) -> None:
        for n in nodes:
            d, h = n["depth"], _node_h(n)
            pos[n["id"]] = (_col_x(d), col_y[d], NODE_W, h)
            col_y[d] += h + ROW_GAP

    def place(f: dict[str, Any], y: float, level: int) -> float:
        lo, hi = span(f)
        inset = FRAME_PAD * nesting(f)
        i = len(rects)
        rects.append(())  # the frame before what is in it: drawn under it
        col_y = dict.fromkeys(range(lo, hi + 1), y + FRAME_HEAD)
        stack(members.get(f["id"], []), col_y)
        for k in kids.get(f["id"], []):
            klo, khi = span(k)
            kb = place(k, max(col_y[d] for d in range(klo, khi + 1)), level + 1)
            for d in range(klo, khi + 1):
                col_y[d] = kb + ROW_GAP
        b = max(y + FRAME_HEAD, max(col_y.values()) - ROW_GAP) + FRAME_PAD
        x = _col_x(lo) - inset
        rects[i] = (f, level, x, y, _col_x(hi) + NODE_W + inset - x, b - y)
        return b

    lead = min((n["depth"] for n in main), default=0)
    framed_lead = any(n["depth"] == lead for f in ids for n in members.get(f, [])) or any(
        span(f)[0] <= lead for f in chain["frames"]
    )
    loose = members.get(None, [])
    outside = [] if framed_lead else [n for n in loose if n["depth"] == lead]
    y = float(top)
    for f in kids.get(None, []):
        y = place(f, y, 0) + ROW_GAP
    col_y = {n["depth"]: y for n in loose}
    stack([n for n in loose if n not in outside], col_y)
    bottom = max([y, *col_y.values()])
    centre = {nid: p[1] + p[3] / 2 for nid, p in pos.items()}

    def want(n: dict[str, Any]) -> float:
        ys = [
            centre[o]
            for s in chain["steps"]
            for a, o in ((s["source"], s["target"]), (s["target"], s["source"]))
            if a == n["id"] and o in centre
        ]
        return sum(ys) / len(ys) - _node_h(n) / 2 if ys else float(top)

    y = float(top)
    for n in sorted(outside, key=want):  # stable: ties keep the diagram's order
        h = _node_h(n)
        ny = max(want(n), y)
        pos[n["id"]] = (_col_x(lead), ny, NODE_W, h)
        y = ny + h + ROW_GAP
        bottom = max(bottom, y)
    return pos, rects, bottom


def parallel_lanes(steps: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    """Step number -> (lane, lanes) for steps that share their source and target with
    another (a compute that reads and writes one table, two rules between two groups),
    lanes in step order; a step alone on its pair is left out."""
    pairs: dict[tuple[str, str], list[int]] = {}
    for s in steps:
        pairs.setdefault((s["source"], s["target"]), []).append(s["n"])
    return {n: (k, len(ns)) for ns in pairs.values() if len(ns) > 1 for k, n in enumerate(ns)}


def flow_svg(
    chain: dict[str, Any],
    title: str = "",
    legend: tuple[tuple[str, str, str], ...] = FLOW_LEGEND,
) -> str:
    """One chain, left to right by step depth, at natural size (every font >= 14px).
    Side branches and context (what a compute runs in / as) hang below the node they
    belong to. Every box shows its code reference (``file:line``). Also draws the
    Network and IAM tab diagrams (same shape: nodes with a depth, numbered steps); a
    diagram with ``frames`` (the Network one) has its boxes laid out in them
    (:func:`framed_layout`), and a node's ``meta`` is a summary line in its box."""
    hung = ("side", "context")
    main = [n for n in chain["nodes"] if n["role"] not in hung]
    side: dict[str, list[dict[str, Any]]] = {}
    for n in chain["nodes"]:
        if n["role"] in hung:
            side.setdefault(n["parent"], []).append(n)
    cols: dict[int, list[dict[str, Any]]] = {}
    for n in main:
        cols.setdefault(n["depth"], []).append(n)
    top = PAD + 40
    pos: dict[str, tuple[float, float, float, float]] = {}  # id -> x, y, width, height
    body: list[str] = []
    bottom = float(top)

    def place_side(parent: str, x: float, y: float) -> float:
        """Side boxes under ``parent`` (indented one step per level), then theirs."""
        px = pos[parent][0]
        for s in side.get(parent, []):
            if s["id"] in pos:
                continue
            sx = px + 24
            mid = y + SIDE_H / 2
            body.append(
                f'<path d="M{sx - 12:.0f},{y - ROW_GAP:.0f} V{mid:.0f} H{sx:.0f}" fill="none" '
                f'stroke="#8c959f" stroke-width="1.5" stroke-dasharray="4 3"/>'
            )
            pos[s["id"]] = (sx, y, NODE_W - (sx - x), SIDE_H)
            y = place_side(s["id"], x, y + SIDE_H + ROW_GAP)
        return y

    frames: list[tuple[Any, ...]] = []
    if chain.get("frames") is not None:  # boxes in nested frames (no side boxes there)
        pos, frames, bottom = framed_layout(chain, main, top)
    else:
        for d in sorted(cols):
            x = _col_x(d)
            y = float(top)
            for n in cols[d]:
                h = _node_h(n)
                pos[n["id"]] = (x, y, NODE_W, h)
                y = place_side(n["id"], x, y + h + ROW_GAP)
            bottom = max(bottom, y)
    width = PAD * 2 + (max(cols) + 1 if cols else 1) * (NODE_W + COL_GAP) - COL_GAP
    for f, level, x, y, w, h in frames:
        width = max(width, x + w + PAD)
        ref = f.get("code_ref")
        ref_text = location_label(ref) if ref else "no code reference"
        dash = ' stroke-dasharray="6 4"' if level else ""
        body.append(
            f'<g class="frame" data-frame="{escape(f["id"])}">'
            f"<title>{escape(f['id'])} ({escape(ref_text)})</title>"
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" rx="10" '
            f'fill="{FRAME_FILL[min(level, 1)]}" stroke="{ROLE_STROKE["network"]}" '
            f'stroke-width="1.5"{dash}/>'
            + _t(x + 10, y + 20, _cut(f["label"], w - 20, 14), 14, extra=' font-weight="700"')
            + _t(
                x + 10,
                y + 38,
                _cut(f"{f['kind_label']} · {ref_text}", w - 20, 14),
                14,
                "#57606a",
            )
            + "</g>"
        )
    nodes_svg = []
    for n in chain["nodes"]:
        if n["id"] not in pos:
            continue
        x, y, w, h = pos[n["id"]]
        stroke = ROLE_STROKE[n["role"]]
        dash = ' stroke-dasharray="5 3"' if n["role"] in hung else ""
        name_size = 14 if n["role"] in hung else 16
        ref = n.get("code_ref", n.get("location"))
        ref_text = location_label(ref) if ref else "no code reference"
        meta = n.get("meta") or ""
        meta_svg = (
            _t(x + 12, y + 70, _cut(meta, w - 24, 14), 14, extra=' class="meta"') if meta else ""
        )
        nodes_svg.append(
            f"<g><title>{escape(n['id'])} ({escape(ref_text)})"
            + (f" — {escape(meta)}" if meta else "")
            + "</title>"
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" '
            f'height="{h:.0f}" rx="8" fill="#ffffff" stroke="{stroke}" stroke-width="2"{dash}/>'
            + _t(
                x + 12,
                y + (22 if h == SIDE_H else 26),
                _cut(n["label"], w - 24, name_size),
                name_size,
                extra=' font-weight="700"',
            )
            + _t(
                x + 12,
                y + (42 if h == SIDE_H else 48),
                _cut(n["kind_label"], w - 24, 14),
                14,
                "#57606a",
            )
            + meta_svg
            + _t(
                x + 12,
                y + (62 if h == SIDE_H else 92 if meta else 70),
                _cut(ref_text, w - 24, 14),
                14,
                "#0969da",
                ' class="code-ref"',
            )
            + "</g>"
        )
    drawn = [s for s in chain["steps"] if s["source"] in pos and s["target"] in pos]
    lanes = parallel_lanes([s for s in drawn if not s.get("quiet")])
    quiet_svg, edges_svg, labels_svg = [], [], []
    placed: list[tuple[float, ...]] = []
    boxes = [(bx, bx + bw, by, by + bh) for bx, by, bw, bh in pos.values()]
    routed = 0  # quiet edges sent along the bottom, each in its own lane
    for s in drawn:
        sx, sy, _, sh = pos[s["source"]]
        tx, ty, _, th = pos[s["target"]]
        colour, dash = STYLE[(s["kind"], s["status"])]
        y1, y2 = sy + sh / 2, ty + th / 2
        k, m = lanes.get(s["n"], (0, 1))
        leftward = tx < sx and s["kind"] == "rule"
        if s.get("quiet"):
            # structure (contains / attached): right angles through the gaps between the
            # columns, along the bottom past the boxes when it skips a column
            x1, x2 = sx + NODE_W, tx
            if tx - x1 == COL_GAP:
                xm = x1 + COL_GAP / 2
                d = f"M{x1:.0f},{y1:.0f} H{xm:.0f} V{y2:.0f} H{x2:.0f}"
            else:
                off = 10 + (routed * 7) % (COL_GAP / 2 - 20)
                lane = bottom + 4 + 10 * routed
                d = (
                    f"M{x1:.0f},{y1:.0f} H{x1 + off:.0f} V{lane:.0f} "
                    f"H{x2 - min(off, x2 - 4):.0f} V{y2:.0f} H{x2:.0f}"
                )
                routed += 1
        else:
            # parallel steps (same source and target) each get a lane: ends apart, the
            # middles further apart, each label on its own edge
            gap = min(24.0, (min(sh, th) - 16) / (m - 1)) if m > 1 else 0.0
            o = (k - (m - 1) / 2) * gap
            y1, y2 = y1 + o, y2 + o
            if tx > sx:
                x1, x2, c1, c2 = sx + NODE_W, tx, sx + NODE_W + 80, tx - 80
            elif leftward:  # egress to the outside, in the leftmost column
                x1, x2, c1, c2 = sx, tx + NODE_W, sx - 80, tx + NODE_W + 80
            else:  # back to an earlier node (a loop): around the right side
                x1, x2, c1, c2 = sx + NODE_W, tx + NODE_W, sx + NODE_W + 90, tx + NODE_W + 90
            cy1, cy2 = y1 + 2 * o, y2 + 2 * o
            d = f"M{x1:.0f},{y1:.0f} C{c1:.0f},{cy1:.0f} {c2:.0f},{cy2:.0f} {x2:.0f},{y2:.0f}"
            curve = (x1, y1, c1, cy1, c2, cy2, x2, y2)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        marker = (
            f' marker-start="url(#fa-{s["n"]})"'
            if s["kind"] == "read"
            else f' marker-end="url(#fa-{s["n"]})"'
        )
        lane_attr = f' data-lane="{k}"' if m > 1 else ""
        cls = ' class="contains"' if s.get("quiet") else ""
        (quiet_svg if s.get("quiet") else edges_svg).append(
            f'<marker id="fa-{s["n"]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
            f'markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
            f'<path data-step="{s["n"]}"{lane_attr}{cls} d="{d}" fill="none" stroke="{colour}" '
            f'stroke-width="{1.5 if s.get("quiet") else 2.2}"{dash_attr}{marker}/>'
        )
        if s.get("quiet"):  # the boxes say enough
            continue
        # the label sits on its own edge (its own lane, for a parallel step): at the middle,
        # or slid along the edge clear of the boxes and of the labels already placed; a
        # loop's label may also lean right of the column it loops around
        line1 = f"{s['n']}. {s['verb'].split(' (')[0]}"
        line2 = {"unverified": "(unverified)", "iam_only": "(IAM only)", "public": "(public)"}.get(
            s["status"], ""
        )
        lw = max(_w(line1, 14), _w(line2, 14)) + 10
        lh = 40 if line2 else 22
        loop = tx <= sx and not leftward
        spots = []
        for t in LABEL_TS:
            px, py = _bezier(curve, t)
            for lx in (px - lw / 2, x1 + 4, px - 6) if loop else (px - lw / 2,):
                if lx <= px <= lx + lw:  # on the edge, always
                    spots.append((lx, py - lh / 2))
        lx, ly = next(
            (p for p in spots if _free(p, lw, lh, placed + boxes)),
            next((p for p in spots if _free(p, lw, lh, placed)), spots[0]),
        )
        placed.append((lx, lx + lw, ly, ly + lh))
        labels_svg.append(
            f'<g data-step="{s["n"]}"{lane_attr}><title>{escape(s["text"])}</title>'
            f'<rect x="{lx:.0f}" y="{ly:.0f}" width="{lw:.0f}" height="{lh}" rx="4" fill="#ffffff" '
            f'fill-opacity="0.92" stroke="{colour}" stroke-width="1"/>'
            + _t(lx + 5, ly + 16, line1, 14, colour, ' font-weight="700"')
            + (_t(lx + 5, ly + 34, line2, 14, colour) if line2 else "")
            + "</g>"
        )
    legend_y = bottom + 10 + 10 * routed
    lx = PAD
    for text, kind, status in legend:
        colour, dash = STYLE[(kind, status)]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        body.append(
            f'<line x1="{lx}" y1="{legend_y:.0f}" x2="{lx + 34}" y2="{legend_y:.0f}" stroke="{colour}" stroke-width="2.2"{dash_attr}/>'
        )
        body.append(_t(lx + 40, legend_y + 5, text, 14, colour))
        lx += 40 + _w(text, 14) + 28
    width = max(width, lx + PAD)
    height = legend_y + 24
    head = _t(
        PAD,
        PAD + 18,
        title or f"Flow {chain['n']}: {chain['title']}",
        18,
        extra=' font-weight="700"',
    )
    w, h = round(width), round(height)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'font-family="{FONT}" font-size="14">\n'
        f"<title>{escape(title or chain['title'])}</title>\n"
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>\n'
        # the quiet structure under the boxes, the labelled steps over them
        + "\n".join([head, *body, *quiet_svg, *nodes_svg, *edges_svg, *labels_svg])
        + "\n</svg>\n"
    )


def chain_svg(model: dict[str, Any], name: str, title: str = "") -> str | None:
    """The SVG of ``flow-<n>`` (None when there is no such chain)."""
    flows = build_flows(model)
    chain = next((c for c in flows["chains"] if c["id"] == name), None)
    if chain is None:
        return None
    head = f"Flow {chain['n']}: {chain['title']}"
    return flow_svg(chain, f"{head} — {title}" if title else head)
