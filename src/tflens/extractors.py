"""Service-specific extractors: ACCESS, TRIGGER, NETWORK and RUNS_ON edges.

Each extractor receives the analyzer and one resource/data item. Generic CONFIG
edges (attribute references, environment variables) are produced by the analyzer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .policy import as_list
from .values import JsonEncoded, Location, display, render, unwrap

if TYPE_CHECKING:
    from .analyze import Analyzer, Item

Extractor = Callable[["Analyzer", "Item"], None]
EXTRACTORS: dict[tuple[bool, str], Extractor] = {}

SG = {"aws_security_group"}
ROLE = {"aws_iam_role"}
USER = {"aws_iam_user"}
GROUP = {"aws_iam_group"}
LAMBDA = {"aws_lambda_function"}


def extractor(*types: str, data: bool = False) -> Callable[[Extractor], Extractor]:
    def register(fn: Extractor) -> Extractor:
        for t in types:
            EXTRACTORS[(data, t)] = fn
        return fn

    return register


def _props(a: Analyzer, it: Item, *names: str) -> None:
    a.props(it.id, **{n: a.get(it, n) for n in names})


def _attach_sgs(a: Analyzer, it: Item, value: Any, loc: Location) -> None:
    for sg in a.resolve(it, value, "security_groups", loc, SG, "security_group"):
        a.edge(
            "NETWORK",
            it.id,
            sg,
            "member of security group",
            [a.ev(loc)],
            {"attachment": True},
        )


# ------------------------------------------------------------------------ EC2


@extractor("aws_instance")
def ec2_instance(a: Analyzer, it: Item) -> None:
    _props(a, it, "instance_type", "ami", "subnet_id", "associate_public_ip_address", "key_name")
    if "user_data" in it.block.attrs or "user_data_base64" in it.block.attrs:
        a.props(it.id, user_data=True)
    if "ami" in it.block.attrs:
        loc = a.attr_loc(it, "ami")
        for t in a.resolve(it, a.get(it, "ami"), "ami", loc, None, "ami", data_ok=True):
            a.edge("RUNS_ON", it.id, t, "ami", [a.ev(loc)])
    if "iam_instance_profile" in it.block.attrs:
        loc = a.attr_loc(it, "iam_instance_profile")
        for p in a.resolve(
            it,
            a.get(it, "iam_instance_profile"),
            "iam_instance_profile",
            loc,
            {"aws_iam_instance_profile"},
            "instance_profile",
        ):
            a.runs_as.setdefault(it.id, []).append((p, [a.ev(loc, "instance profile")], []))
    for name in ("vpc_security_group_ids", "security_groups"):
        if name in it.block.attrs:
            _attach_sgs(a, it, a.get(it, name), a.attr_loc(it, name))


# --------------------------------------------------------------------- Lambda


@extractor("aws_lambda_function")
def lambda_function(a: Analyzer, it: Item) -> None:
    _props(a, it, "function_name", "runtime", "handler", "memory_size", "timeout", "package_type")
    for _, env in a.nested(it, "environment"):
        variables = unwrap(env.get("variables"))
        if isinstance(variables, dict):
            a.props(it.id, environment_variables=sorted(variables))
    if "runtime" in it.block.attrs:
        loc = a.attr_loc(it, "runtime")
        rt = render(a.get(it, "runtime"))
        if rt is None:
            a.unknown(it.id, "runtime", "runtime is not known statically", loc)
        else:
            a.edge("RUNS_ON", it.id, a.external("runtime", rt), "runtime", [a.ev(loc)])
    if "image_uri" in it.block.attrs:
        loc = a.attr_loc(it, "image_uri")
        for t in a.resolve(it, a.get(it, "image_uri"), "image_uri", loc, None, "container_image"):
            a.edge("RUNS_ON", it.id, t, "container image", [a.ev(loc)])
    if "role" in it.block.attrs:
        loc = a.attr_loc(it, "role")
        for r in a.resolve(it, a.get(it, "role"), "role", loc, ROLE, "iam_role"):
            a.runs_as.setdefault(it.id, []).append((r, [a.ev(loc, "execution role")], []))
    for blk, vpc in a.nested(it, "vpc_config"):
        if "security_group_ids" in blk.attrs:
            _attach_sgs(a, it, vpc["security_group_ids"], blk.attrs["security_group_ids"].loc)


@extractor("aws_lambda_event_source_mapping")
def lambda_event_source_mapping(a: Analyzer, it: Item) -> None:
    sources = a.resolve(
        it,
        a.get(it, "event_source_arn"),
        "event_source_arn",
        a.attr_loc(it, "event_source_arn"),
        None,
        "event_source",
    )
    functions = a.resolve(
        it,
        a.get(it, "function_name"),
        "function_name",
        a.attr_loc(it, "function_name"),
        LAMBDA,
        "lambda_function",
    )
    attrs = {
        k: display(v)
        for k in (
            "batch_size",
            "starting_position",
            "enabled",
            "maximum_batching_window_in_seconds",
        )
        if (v := a.get(it, k)) is not None
    }
    for s in sources:
        for f in functions:
            a.edge("TRIGGER", s, f, "event source mapping", [a.ev(it.loc)], attrs, [it.id])


@extractor("aws_lambda_permission")
def lambda_permission(a: Analyzer, it: Item) -> None:
    functions = a.resolve(
        it,
        a.get(it, "function_name"),
        "function_name",
        a.attr_loc(it, "function_name"),
        LAMBDA,
        "lambda_function",
    )
    action = render(a.get(it, "action")) or "lambda:InvokeFunction"
    principal = render(a.get(it, "principal"))
    sources: list[str] = []
    if "source_arn" in it.block.attrs:
        sources = a.resolve(
            it, a.get(it, "source_arn"), "source_arn", a.attr_loc(it, "source_arn"), None, "arn"
        )
    if not sources:
        if principal is None:
            a.unknown(
                it.id, "principal", "principal is not known statically", a.attr_loc(it, "principal")
            )
            return
        kind = "aws_account" if principal.isdigit() else "aws_service"
        sources = [a.external("principal" if principal == "*" else kind, principal)]
    attrs = {
        "effect": "Allow",
        "actions": [action],
        "principal": principal,
        "resource_policy": True,
    }
    for f in functions:
        for s in sources:
            a.edge("ACCESS", s, f, f"Allow: {action}", [a.ev(it.loc)], attrs, [it.id])


# ------------------------------------------------------------------------ SQS


@extractor("aws_sqs_queue")
def sqs_queue(a: Analyzer, it: Item) -> None:
    _props(
        a,
        it,
        "name",
        "fifo_queue",
        "visibility_timeout_seconds",
        "message_retention_seconds",
        "kms_master_key_id",
        "sqs_managed_sse_enabled",
    )
    redrive = unwrap(a.get(it, "redrive_policy"))
    if isinstance(redrive, JsonEncoded) and isinstance(redrive.value, dict):
        loc = a.attr_loc(it, "redrive_policy")
        dlq = a.resolve(
            it, redrive.value.get("deadLetterTargetArn"), "redrive_policy", loc, {"aws_sqs_queue"}
        )
        a.props(it.id, dead_letter_queue=dlq)
    if "policy" in it.block.attrs:
        a.grant_resource(it.id, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [])


@extractor("aws_sqs_queue_policy")
def sqs_queue_policy(a: Analyzer, it: Item) -> None:
    queues = a.resolve(
        it,
        a.get(it, "queue_url"),
        "queue_url",
        a.attr_loc(it, "queue_url"),
        {"aws_sqs_queue"},
        "sqs_queue",
    )
    for q in queues:
        a.grant_resource(q, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [it.id])


# ------------------------------------------------------------------------ SNS


@extractor("aws_sns_topic")
def sns_topic(a: Analyzer, it: Item) -> None:
    _props(a, it, "name", "fifo_topic", "kms_master_key_id")
    if "policy" in it.block.attrs:
        a.grant_resource(it.id, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [])


@extractor("aws_sns_topic_policy")
def sns_topic_policy(a: Analyzer, it: Item) -> None:
    topics = a.resolve(
        it, a.get(it, "arn"), "arn", a.attr_loc(it, "arn"), {"aws_sns_topic"}, "sns_topic"
    )
    for t in topics:
        a.grant_resource(t, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [it.id])


_ARN_PROTOCOLS = {"lambda", "sqs", "firehose", "application"}


@extractor("aws_sns_topic_subscription")
def sns_subscription(a: Analyzer, it: Item) -> None:
    topics = a.resolve(
        it,
        a.get(it, "topic_arn"),
        "topic_arn",
        a.attr_loc(it, "topic_arn"),
        {"aws_sns_topic"},
        "sns_topic",
    )
    protocol = render(a.get(it, "protocol"))
    loc = a.attr_loc(it, "endpoint")
    if protocol is None:
        a.unknown(it.id, "protocol", "protocol is not known statically", a.attr_loc(it, "protocol"))
        return
    if protocol in _ARN_PROTOCOLS:
        endpoints = a.resolve(it, a.get(it, "endpoint"), "endpoint", loc, None, protocol)
    else:
        endpoints = a.resolve(
            it, a.get(it, "endpoint"), "endpoint", loc, None, f"{protocol}_endpoint"
        )
    attrs: dict[str, Any] = {"protocol": protocol}
    for k in ("raw_message_delivery", "filter_policy"):
        if (v := a.get(it, k)) is not None:
            attrs[k] = display(v)
    for t in topics:
        for e in endpoints:
            a.edge("TRIGGER", t, e, f"subscription ({protocol})", [a.ev(it.loc)], attrs, [it.id])


# ------------------------------------------------------------------- DynamoDB


@extractor("aws_dynamodb_table")
def dynamodb_table(a: Analyzer, it: Item) -> None:
    _props(
        a,
        it,
        "name",
        "billing_mode",
        "hash_key",
        "range_key",
        "stream_enabled",
        "stream_view_type",
        "read_capacity",
        "write_capacity",
    )
    gsis = [render(g.get("name")) or "?" for _, g in a.nested(it, "global_secondary_index")]
    if gsis:
        a.props(it.id, global_secondary_indexes=gsis)
    for _, sse in a.nested(it, "server_side_encryption"):
        a.props(it.id, server_side_encryption=sse.get("enabled"))
    for _, pitr in a.nested(it, "point_in_time_recovery"):
        a.props(it.id, point_in_time_recovery=pitr.get("enabled"))


# ------------------------------------------------------------------------- S3


@extractor("aws_s3_bucket")
def s3_bucket(a: Analyzer, it: Item) -> None:
    _props(a, it, "bucket", "bucket_prefix", "force_destroy", "acl")
    if "policy" in it.block.attrs:
        a.grant_resource(it.id, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [])


def _buckets(a: Analyzer, it: Item) -> list[str]:
    return a.resolve(
        it, a.get(it, "bucket"), "bucket", a.attr_loc(it, "bucket"), {"aws_s3_bucket"}, "s3_bucket"
    )


@extractor("aws_s3_bucket_policy")
def s3_bucket_policy(a: Analyzer, it: Item) -> None:
    for b in _buckets(a, it):
        a.grant_resource(b, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [it.id])


_NOTIFICATION_TARGETS = (
    ("lambda_function", "lambda_function_arn"),
    ("queue", "queue_arn"),
    ("topic", "topic_arn"),
)


@extractor("aws_s3_bucket_notification")
def s3_bucket_notification(a: Analyzer, it: Item) -> None:
    buckets = _buckets(a, it)
    for block_name, arn_attr in _NOTIFICATION_TARGETS:
        for blk, val in a.nested(it, block_name):
            loc = blk.attrs[arn_attr].loc if arn_attr in blk.attrs else blk.loc
            targets = a.resolve(it, val.get(arn_attr), arn_attr, loc, None, block_name)
            events = [render(e) or display(e) for e in as_list(val.get("events"))]
            attrs: dict[str, Any] = {"events": events}
            for k in ("filter_prefix", "filter_suffix"):
                if k in val:
                    attrs[k] = display(val[k])
            for b in buckets:
                for t in targets:
                    label = f"s3 notification: {', '.join(events)}"
                    a.edge("TRIGGER", b, t, label, [a.ev(blk.loc)], attrs, [it.id])
    if unwrap(a.get(it, "eventbridge")) is True:
        target = a.external("aws_service", "events.amazonaws.com")
        for b in buckets:
            a.edge(
                "TRIGGER",
                b,
                target,
                "s3 notification: EventBridge",
                [a.ev(a.attr_loc(it, "eventbridge"))],
                {},
                [it.id],
            )


@extractor("aws_s3_bucket_public_access_block")
def s3_public_access_block(a: Analyzer, it: Item) -> None:
    flags = (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    )
    summary = {f: a.get(it, f) for f in flags if f in it.block.attrs}
    for b in _buckets(a, it):
        a.props(b, public_access_block=summary)


@extractor("aws_s3_bucket_versioning")
def s3_versioning(a: Analyzer, it: Item) -> None:
    for _, cfg in a.nested(it, "versioning_configuration"):
        for b in _buckets(a, it):
            a.props(b, versioning=cfg.get("status"))


@extractor("aws_s3_bucket_server_side_encryption_configuration")
def s3_encryption(a: Analyzer, it: Item) -> None:
    algos = []
    for _, rule in a.nested(it, "rule"):
        for default in as_list(rule.get("apply_server_side_encryption_by_default")):
            if isinstance(default, dict):
                algos.append(display(default.get("sse_algorithm")))
    for b in _buckets(a, it):
        a.props(b, server_side_encryption=algos)


# ------------------------------------------------------------------------ IAM


@extractor("aws_iam_role")
def iam_role(a: Analyzer, it: Item) -> None:
    _props(a, it, "name", "path", "max_session_duration", "permissions_boundary")
    if "assume_role_policy" in it.block.attrs:
        a.grant_resource(
            it.id, it, a.get(it, "assume_role_policy"), a.attr_loc(it, "assume_role_policy"), []
        )
    for blk, inline in a.nested(it, "inline_policy"):
        if "policy" in blk.attrs:
            a.grant_identity([it.id], it, inline["policy"], blk.attrs["policy"].loc, [])
    if "managed_policy_arns" in it.block.attrs:
        loc = a.attr_loc(it, "managed_policy_arns")
        for arn in as_list(a.get(it, "managed_policy_arns")):
            a.attach_policy([it.id], it, arn, loc, [])


@extractor("aws_iam_user", "aws_iam_group", "aws_iam_policy")
def iam_named(a: Analyzer, it: Item) -> None:
    _props(a, it, "name", "path")


_INLINE = {
    "aws_iam_role_policy": ("role", ROLE, "iam_role"),
    "aws_iam_user_policy": ("user", USER, "iam_user"),
    "aws_iam_group_policy": ("group", GROUP, "iam_group"),
}


@extractor(*_INLINE)
def iam_inline_policy(a: Analyzer, it: Item) -> None:
    attr, types, ext = _INLINE[it.type]
    principals = a.resolve(it, a.get(it, attr), attr, a.attr_loc(it, attr), types, ext)
    a.grant_identity(principals, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [it.id])


_ATTACH = {
    "aws_iam_role_policy_attachment": ("role", ROLE, "iam_role"),
    "aws_iam_user_policy_attachment": ("user", USER, "iam_user"),
    "aws_iam_group_policy_attachment": ("group", GROUP, "iam_group"),
}


@extractor(*_ATTACH)
def iam_policy_attachment(a: Analyzer, it: Item) -> None:
    attr, types, ext = _ATTACH[it.type]
    principals = a.resolve(it, a.get(it, attr), attr, a.attr_loc(it, attr), types, ext)
    a.attach_policy(principals, it, a.get(it, "policy_arn"), a.attr_loc(it, "policy_arn"), [it.id])


@extractor("aws_iam_policy_attachment")
def iam_policy_attachment_multi(a: Analyzer, it: Item) -> None:
    principals: list[str] = []
    for attr, types, ext in (
        ("roles", ROLE, "iam_role"),
        ("users", USER, "iam_user"),
        ("groups", GROUP, "iam_group"),
    ):
        if attr in it.block.attrs:
            principals += a.resolve(it, a.get(it, attr), attr, a.attr_loc(it, attr), types, ext)
    a.attach_policy(principals, it, a.get(it, "policy_arn"), a.attr_loc(it, "policy_arn"), [it.id])


@extractor("aws_iam_instance_profile")
def iam_instance_profile(a: Analyzer, it: Item) -> None:
    _props(a, it, "name", "path")
    if "role" in it.block.attrs:
        loc = a.attr_loc(it, "role")
        for r in a.resolve(it, a.get(it, "role"), "role", loc, ROLE, "iam_role"):
            a.runs_as.setdefault(it.id, []).append((r, [a.ev(loc, "instance profile role")], []))


# ------------------------------------------------------------ security groups


def _port_label(direction: str, protocol: str | None, from_port: Any, to_port: Any) -> str:
    if protocol in ("-1", "all"):
        return f"{direction} all traffic"
    fp, tp = render(from_port), render(to_port)
    ports = "?" if fp is None else fp if fp == tp or tp is None else f"{fp}-{tp}"
    return f"{direction} {protocol or '?'} {ports}"


def _sg_rule(
    a: Analyzer,
    holder: Item,
    sg: str,
    direction: str,
    rule: dict[str, Any],
    loc: Location,
    via: list[str],
) -> None:
    protocol = render(rule.get("protocol"))
    label = _port_label(direction, protocol, rule.get("from_port"), rule.get("to_port"))
    peers: list[str] = []
    for key in ("cidr_blocks", "ipv6_cidr_blocks"):
        if rule.get(key) is not None:
            peers += a.resolve(holder, rule[key], key, loc, None, "cidr")
    for key in ("security_groups", "source_security_group_id"):
        if rule.get(key) is not None:
            peers += a.resolve(holder, rule[key], key, loc, SG, "security_group")
    if rule.get("prefix_list_ids") is not None:
        peers += a.resolve(
            holder, rule["prefix_list_ids"], "prefix_list_ids", loc, None, "prefix_list"
        )
    if unwrap(rule.get("self")) is True:
        peers.append(sg)
    attrs: dict[str, Any] = {
        "direction": direction,
        "protocol": protocol,
        "from_port": display(rule.get("from_port")),
        "to_port": display(rule.get("to_port")),
    }
    if rule.get("description") is not None:
        attrs["description"] = display(rule["description"])
    for peer in dict.fromkeys(peers):
        public = peer.endswith(":0.0.0.0/0") or peer.endswith(":::/0")
        edge_attrs = {**attrs, "public": public}
        src, dst = (peer, sg) if direction == "ingress" else (sg, peer)
        a.edge("NETWORK", src, dst, label, [a.ev(loc)], edge_attrs, via)


@extractor("aws_security_group")
def security_group(a: Analyzer, it: Item) -> None:
    _props(a, it, "name", "description", "vpc_id")
    for direction in ("ingress", "egress"):
        for blk, rule in a.nested(it, direction):
            _sg_rule(a, it, it.id, direction, rule, blk.loc, [])


@extractor("aws_security_group_rule")
def security_group_rule(a: Analyzer, it: Item) -> None:
    direction = render(a.get(it, "type"))
    if direction not in ("ingress", "egress"):
        a.unknown(it.id, "type", "rule type is not known statically", a.attr_loc(it, "type"))
        return
    rule = {name: a.get(it, name) for name in it.block.attrs}
    sgs = a.resolve(
        it,
        a.get(it, "security_group_id"),
        "security_group_id",
        a.attr_loc(it, "security_group_id"),
        SG,
        "security_group",
    )
    for sg in sgs:
        _sg_rule(a, it, sg, direction, rule, it.loc, [it.id])


@extractor("aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule")
def vpc_security_group_rule(a: Analyzer, it: Item) -> None:
    direction = "ingress" if it.type.endswith("ingress_rule") else "egress"
    rule: dict[str, Any] = {
        "protocol": a.get(it, "ip_protocol"),
        "from_port": a.get(it, "from_port"),
        "to_port": a.get(it, "to_port"),
        "description": a.get(it, "description"),
        "cidr_blocks": a.get(it, "cidr_ipv4"),
        "ipv6_cidr_blocks": a.get(it, "cidr_ipv6"),
        "source_security_group_id": a.get(it, "referenced_security_group_id"),
        "prefix_list_ids": a.get(it, "prefix_list_id"),
    }
    sgs = a.resolve(
        it,
        a.get(it, "security_group_id"),
        "security_group_id",
        a.attr_loc(it, "security_group_id"),
        SG,
        "security_group",
    )
    for sg in sgs:
        _sg_rule(a, it, sg, direction, rule, it.loc, [it.id])


# ------------------------------------------------------------------------ VPC


@extractor("aws_vpc_endpoint")
def vpc_endpoint(a: Analyzer, it: Item) -> None:
    _props(a, it, "service_name", "vpc_id", "private_dns_enabled")
    ep_type = render(a.get(it, "vpc_endpoint_type")) or "Gateway"
    a.props(it.id, vpc_endpoint_type=ep_type)
    loc = a.attr_loc(it, "service_name")
    for svc in a.resolve(it, a.get(it, "service_name"), "service_name", loc, None, "aws_service"):
        a.edge(
            "NETWORK",
            it.id,
            svc,
            f"private {ep_type} endpoint",
            [a.ev(loc)],
            {"vpc_endpoint_type": ep_type},
        )
    if "security_group_ids" in it.block.attrs:
        _attach_sgs(a, it, a.get(it, "security_group_ids"), a.attr_loc(it, "security_group_ids"))
    if "policy" in it.block.attrs:
        a.grant_resource(it.id, it, a.get(it, "policy"), a.attr_loc(it, "policy"), [])
