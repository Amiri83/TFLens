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
  branches, never chain steps.

Every step carries the ``file:line`` of each piece of evidence it rests on.
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
    # external nodes (by external kind)
    "kafka_topic": "Kafka topic",
    "aws_service": "AWS service",
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
            if ref and writes:
                kind, status, iam = "send", "verified", writes
            elif ref and reads:
                kind, status, iam = "read", "verified", reads
            elif ref:
                kind, status, iam = TARGET_TYPES[self.type_of(target)], "unverified", []
            elif writes:
                kind, status, iam = "send", "iam_only", writes
            elif reads:
                kind, status, iam = "read", "iam_only", reads
            else:
                continue  # an ACCESS edge with neither read nor write actions
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
    def chain(self, entry: str) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        depth = {entry: 0}
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
                if kind != "read":  # what it reads is a sink of this chain
                    queue.append(target)
        main = list(depth)
        return {"entry": entry, "steps": steps, "depth": depth, "side": self.side(main)}

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
    # write to it, but nothing reached does), then code that sends somewhere but that
    # nothing declared starts: each its own chain
    late = [s for s in f.trigger_sources() if not f.is_compute(s)]
    late += [c for c in f.computes() if f.links(c)]
    for start in late:
        if start not in reached:
            c = f.chain(start)
            reached |= set(c["depth"])
            chains.append(c)
    # the longest pipeline first: that is where to start reading
    chains.sort(key=lambda c: (-len(c["steps"]), f.name(c["entry"]), c["entry"]))
    out_chains = []
    for i, c in enumerate(chains, 1):
        entry = f.nodes[c["entry"]]
        nodes = [
            {
                "id": nid,
                "label": f.name(nid),
                "kind_label": kind_of(f.nodes[nid]),
                "depth": d,
                "location": f.nodes[nid]["location"],
                "role": "entry" if d == 0 else ("compute" if f.is_compute(nid) else "target"),
            }
            for nid, d in c["depth"].items()
        ]
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
        no_trigger = f.is_compute(c["entry"])
        out_chains.append(
            {
                "id": f"flow-{i}",
                "n": i,
                "title": _chain_title(f, c),
                "entry": c["entry"],
                "entry_label": f.name(c["entry"]),
                "entry_kind": "compute without a declared trigger"
                if no_trigger
                else kind_of(entry),
                "steps": c["steps"],
                "side": c["side"],
                "nodes": nodes,
            }
        )
    steps = [s for c in out_chains for s in c["steps"]]
    metrics = {
        "flows": len(out_chains),
        "entry_points": len(out_chains),
        "steps": len(steps),
        "unverified": sum(1 for s in steps if s["status"] == "unverified"),
        "iam_only": sum(1 for s in steps if s["status"] == "iam_only"),
        "side_branches": sum(len(c["side"]) for c in out_chains),
    }
    return {
        "chains": out_chains,
        "metrics": metrics,
        "start_here": start_here(model, f, out_chains),
    }


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


# ------------------------------------------------------------------ markdown


def render_flows_markdown(flows: dict[str, Any], level: int = 1) -> str:
    h = "#" * level
    out = [f"{h} Flows", "", metrics_text(flows["metrics"]) + ".", "", f"{h}# Start here", ""]
    for ln in flows["start_here"]:
        loc = f" (`{location_label(ln['location'])}`)" if ln["location"] else ""
        out.append(f"{'  ' * ln['level']}- {ln['text']}{loc}")
    out.append("")
    for c in flows["chains"]:
        out += [f"{h}# Flow {c['n']}: {c['title']}", ""]
        out += [s["text"] for s in c["steps"]] or ["(no steps)"]
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
NODE_W, NODE_H, SIDE_H = 240, 62, 54
COL_GAP, ROW_GAP, PAD = 210, 26, 24
STYLE = {  # (kind, status) -> colour, dash
    ("trigger", ""): ("#bc4c00", ""),
    ("send", "verified"): ("#1a7f37", ""),
    ("send", "unverified"): ("#9a6700", "8 5"),
    ("send", "iam_only"): ("#57606a", "2 4"),
    ("read", "verified"): ("#0969da", ""),
    ("read", "unverified"): ("#9a6700", "8 5"),
    ("read", "iam_only"): ("#57606a", "2 4"),
}
ROLE_STROKE = {"entry": "#bc4c00", "compute": "#8250df", "target": "#1a7f37", "side": "#8c959f"}


def _w(text: str, size: int) -> float:
    return len(text) * 0.6 * size


def _cut(text: str, width: float, size: int) -> str:
    n = int(width / (0.6 * size))
    return text if len(text) <= n else text[: max(n - 1, 1)] + "…"


def _t(
    x: float, y: float, text: str, size: int = 14, fill: str = "#24292f", extra: str = ""
) -> str:
    return f'<text x="{x:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}"{extra}>{escape(text)}</text>'


def flow_svg(chain: dict[str, Any], title: str = "") -> str:
    """One chain, left to right by step depth, at natural size (every font >= 14px).
    Side branches hang below the node they belong to."""
    main = [n for n in chain["nodes"] if n["role"] != "side"]
    side: dict[str, list[dict[str, Any]]] = {}
    for n in chain["nodes"]:
        if n["role"] == "side":
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

    for d in sorted(cols):
        x = PAD + d * (NODE_W + COL_GAP)
        y = float(top)
        for n in cols[d]:
            pos[n["id"]] = (x, y, NODE_W, NODE_H)
            y = place_side(n["id"], x, y + NODE_H + ROW_GAP)
        bottom = max(bottom, y)
    width = PAD * 2 + (max(cols) + 1 if cols else 1) * (NODE_W + COL_GAP) - COL_GAP
    nodes_svg = []
    for n in chain["nodes"]:
        if n["id"] not in pos:
            continue
        x, y, w, h = pos[n["id"]]
        stroke = ROLE_STROKE[n["role"]]
        dash = ' stroke-dasharray="5 3"' if n["role"] == "side" else ""
        name_size = 14 if n["role"] == "side" else 16
        nodes_svg.append(
            f'<g><title>{escape(n["id"])}</title><rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" '
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
            + "</g>"
        )
    edges_svg, labels_svg = [], []
    placed: list[tuple[float, float, float, float]] = []
    for s in chain["steps"]:
        if s["source"] not in pos or s["target"] not in pos:
            continue
        sx, sy, _, sh = pos[s["source"]]
        tx, ty, _, th = pos[s["target"]]
        colour, dash = STYLE[(s["kind"], s["status"])]
        y1, y2 = sy + sh / 2, ty + th / 2
        if tx > sx:
            x1, x2 = sx + NODE_W, tx
            d = f"M{x1:.0f},{y1:.0f} C{x1 + 80:.0f},{y1:.0f} {x2 - 80:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
        else:  # back to an earlier node (a loop): around the right side
            x1, x2 = sx + NODE_W, tx + NODE_W
            d = f"M{x1:.0f},{y1:.0f} C{x1 + 90:.0f},{y1:.0f} {x2 + 90:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        marker = (
            f' marker-start="url(#fa-{s["n"]})"'
            if s["kind"] == "read"
            else f' marker-end="url(#fa-{s["n"]})"'
        )
        edges_svg.append(
            f'<marker id="fa-{s["n"]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
            f'markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
            f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2.2"{dash_attr}{marker}/>'
        )
        # the label sits in the gap just before the target: one per incoming step, stacked
        line1 = f"{s['n']}. {s['verb'].split(' (')[0]}"
        line2 = {"unverified": "(unverified)", "iam_only": "(IAM only)"}.get(s["status"], "")
        lw = max(_w(line1, 14), _w(line2, 14)) + 10
        lh = 40 if line2 else 22
        lx = (x2 - 10 - lw) if tx > sx else x1 + 20
        ly = y2 - lh - 4
        while any(lx < b[1] and b[0] < lx + lw and ly < b[3] and b[2] < ly + lh for b in placed):
            ly -= lh + 4
        placed.append((lx, lx + lw, ly, ly + lh))
        labels_svg.append(
            f"<g><title>{escape(s['text'])}</title>"
            f'<rect x="{lx:.0f}" y="{ly:.0f}" width="{lw:.0f}" height="{lh}" rx="4" fill="#ffffff" '
            f'fill-opacity="0.92" stroke="{colour}" stroke-width="1"/>'
            + _t(lx + 5, ly + 16, line1, 14, colour, ' font-weight="700"')
            + (_t(lx + 5, ly + 34, line2, 14, colour) if line2 else "")
            + "</g>"
        )
    legend_y = bottom + 10
    legend = [
        ("triggers / routes to", "trigger", ""),
        ("sends to", "send", "verified"),
        ("reads from", "read", "verified"),
        ("unverified", "send", "unverified"),
        ("IAM only", "send", "iam_only"),
    ]
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
        + "\n".join([head, *body, *edges_svg, *nodes_svg, *labels_svg])
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
