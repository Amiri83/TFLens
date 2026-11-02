"""Display labels of model nodes, shared by the Graph, the Diagrams and the flows.

A resource is labelled by the module call that creates it and its type
(``worker · Lambda function``), never by a bare local name such as ``this``; the full
address stays in the detail pane. Pure functions over model nodes, no I/O.
"""

from __future__ import annotations

import posixpath
from typing import Any

# Local names that say nothing on their own (``aws_iam_role.this``).
GENERIC_NAMES = frozenset({"this", "these", "self", "it"})

_SHORT_TYPES = {
    "aws_lambda_function": "Lambda",
    "aws_instance": "EC2",
    "aws_autoscaling_group": "ASG",
    "aws_launch_template": "launch template",
    "aws_ecs_cluster": "ECS cluster",
    "aws_ecs_service": "ECS service",
    "aws_ecs_task_definition": "ECS task",
    "aws_eks_cluster": "EKS",
    "aws_sqs_queue": "SQS",
    "aws_sns_topic": "SNS",
    "aws_s3_bucket": "S3",
    "aws_dynamodb_table": "DynamoDB",
    "aws_db_instance": "RDS",
    "aws_rds_cluster": "RDS cluster",
    "aws_iam_role": "IAM role",
    "aws_iam_policy": "IAM policy",
    "aws_iam_role_policy": "IAM policy",
    "aws_kms_key": "KMS key",
    "aws_lb": "load balancer",
    "aws_vpc": "VPC",
    "aws_subnet": "subnet",
    "aws_security_group": "security group",
    "aws_cloudwatch_log_group": "log group",
    "aws_cloudwatch_event_rule": "EventBridge rule",
    "aws_lambda_event_source_mapping": "trigger",
    "aws_sfn_state_machine": "Step Functions",
    "aws_secretsmanager_secret": "secret",
    "aws_ssm_parameter": "SSM parameter",
}


def is_generic(name: str) -> bool:
    return name.lower() in GENERIC_NAMES


def type_label(rtype: str) -> str:
    """``aws_instance`` -> ``EC2``; unknown types lose their provider prefix."""
    if rtype in _SHORT_TYPES:
        return _SHORT_TYPES[rtype]
    head, _, rest = rtype.partition("_")
    return (rest or head).replace("_", " ")


def module_address(module: str) -> str:
    """A node's module path without its ``<root>::`` prefix ('' for a root module)."""
    module = module or ""
    if "::" in module:
        module = module.split("::", 1)[1]
    return "" if module == "(root)" else module


def call_name(module: str) -> str:
    """``stacks/a::module.net.module.subnets`` -> ``subnets`` ('' for a root module)."""
    address = module_address(module)
    return address.rsplit("module.", 1)[-1].strip(".") if address else ""


def root_name(root: str | None) -> str:
    if not root or root == ".":
        return "root"
    return posixpath.basename(root.rstrip("/")) or root


def node_label(node: dict[str, Any], root: str | None = None) -> str:
    """``<module call> · <type>`` (plus the local name when it says something), a root
    module's resource ``<name> · <type>``; never a bare ``this``."""
    kind = node.get("kind")
    name = str(node.get("name") or "")
    if kind == "external":
        return name.rsplit(":", 1)[-1] if name.startswith("arn:") else name
    if kind == "module":
        return f"{name} · module"
    tlabel = type_label(node.get("type") or "")
    if kind == "data":
        tlabel += " (data)"
    call = call_name(node.get("module") or "")
    meaningful = bool(name) and not is_generic(name)
    if call:
        return f"{call} · {tlabel}" + (f" · {name}" if meaningful and name != call else "")
    return f"{name if meaningful else root_name(root)} · {tlabel}"


def readable_name(name: str, node: dict[str, Any], root: str | None = None) -> str:
    """``name`` unless it is a bare generic local name; then the node's label."""
    return node_label(node, root) if is_generic(name) else name
