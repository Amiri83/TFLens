"""AWS-style service icons for the exported SVG diagrams (offline, self-contained).

Each icon is a rounded square in the AWS architecture-icon category colour with a short
white glyph. They are TFLens's own simplified drawings, embedded as ``<symbol>`` elements
so an exported SVG needs nothing else. To use other artwork (e.g. the official AWS
Architecture Icons, under their own licence), drop ``<icon>.svg`` files into
``static/vendor/aws-icons/`` (names: the keys of :data:`ICONS`): a vendored file replaces
the built-in drawing of that icon.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

VENDOR_DIR = Path(__file__).parent / "static" / "vendor" / "aws-icons"

# AWS architecture icon category colours.
COMPUTE = "#ED7100"
INTEGRATION = "#E7157B"
DATABASE = "#C925D1"
STORAGE = "#7AA116"
NETWORK = "#8C4FFF"
SECURITY = "#DD344C"
TERRAFORM = "#7B42BC"
NEUTRAL = "#545B64"

# icon name -> (colour, glyph)
ICONS: dict[str, tuple[str, str]] = {
    "lambda": (COMPUTE, "λ"),
    "ec2": (COMPUTE, "EC2"),
    "autoscaling": (COMPUTE, "ASG"),
    "ecs": (COMPUTE, "ECS"),
    "eks": (COMPUTE, "EKS"),
    "ecr": (COMPUTE, "ECR"),
    "sqs": (INTEGRATION, "SQS"),
    "sns": (INTEGRATION, "SNS"),
    "eventbridge": (INTEGRATION, "EB"),
    "sfn": (INTEGRATION, "SFN"),
    "apigateway": (NETWORK, "API"),
    "kinesis": (NETWORK, "KIN"),
    "dynamodb": (DATABASE, "DDB"),
    "rds": (DATABASE, "RDS"),
    "elasticache": (DATABASE, "EC"),
    "s3": (STORAGE, "S3"),
    "efs": (STORAGE, "EFS"),
    "vpc": (NETWORK, "VPC"),
    "elb": (NETWORK, "LB"),
    "cloudfront": (NETWORK, "CF"),
    "iam": (SECURITY, "IAM"),
    "kms": (SECURITY, "KMS"),
    "secretsmanager": (SECURITY, "SM"),
    "terraform": (TERRAFORM, "TF"),
    "module-local": (NEUTRAL, "MOD"),
    "module-registry": (TERRAFORM, "REG"),
    "module-git": ("#F05032", "GIT"),
    "module-remote": (NEUTRAL, "EXT"),
    "generic": (NEUTRAL, "AWS"),
}

# resource type -> icon name
TYPE_ICONS: dict[str, str] = {
    "aws_lambda_function": "lambda",
    "aws_instance": "ec2",
    "aws_autoscaling_group": "autoscaling",
    "aws_ecs_cluster": "ecs",
    "aws_ecs_service": "ecs",
    "aws_eks_cluster": "eks",
    "aws_ecr_repository": "ecr",
    "aws_sqs_queue": "sqs",
    "aws_sns_topic": "sns",
    "aws_cloudwatch_event_rule": "eventbridge",
    "aws_sfn_state_machine": "sfn",
    "aws_api_gateway_rest_api": "apigateway",
    "aws_apigatewayv2_api": "apigateway",
    "aws_kinesis_stream": "kinesis",
    "aws_dynamodb_table": "dynamodb",
    "aws_db_instance": "rds",
    "aws_rds_cluster": "rds",
    "aws_elasticache_cluster": "elasticache",
    "aws_s3_bucket": "s3",
    "aws_efs_file_system": "efs",
    "aws_vpc": "vpc",
    "aws_subnet": "vpc",
    "aws_vpc_endpoint": "vpc",
    "aws_security_group": "vpc",
    "aws_lb": "elb",
    "aws_cloudfront_distribution": "cloudfront",
    "aws_iam_role": "iam",
    "aws_iam_user": "iam",
    "aws_kms_key": "kms",
    "aws_secretsmanager_secret": "secretsmanager",
}

_XML_DECL = re.compile(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>|<!--.*?-->", re.S)
_SVG_OPEN = re.compile(r"<svg\b([^>]*)>", re.S)
_VIEWBOX = re.compile(r'viewBox\s*=\s*"([^"]+)"')


def icon_for_type(rtype: str) -> str:
    return TYPE_ICONS.get(rtype, "generic")


def colour(name: str) -> str:
    return ICONS.get(name, ICONS["generic"])[0]


@cache
def _vendored(name: str) -> str | None:
    """A vendored icon as a ``<symbol>`` body, or None."""
    path = VENDOR_DIR / f"{name}.svg"
    try:
        text = _XML_DECL.sub("", path.read_text(encoding="utf-8")).strip()
    except (OSError, UnicodeDecodeError):
        return None
    m = _SVG_OPEN.search(text)
    if m is None or "<script" in text.lower():
        return None
    vb = _VIEWBOX.search(m.group(1))
    body = text[m.end() :].rsplit("</svg>", 1)[0]
    return (
        f'<symbol id="icon-{name}" viewBox="{vb.group(1) if vb else "0 0 64 64"}">{body}</symbol>'
    )


def symbol(name: str) -> str:
    """The ``<symbol id="icon-<name>">`` definition of an icon (64x64 view box)."""
    vendored = _vendored(name)
    if vendored is not None:
        return vendored
    fill, glyph = ICONS.get(name, ICONS["generic"])
    size = 34 if len(glyph) == 1 else (22 if len(glyph) == 2 else 17)
    return (
        f'<symbol id="icon-{name}" viewBox="0 0 64 64">'
        f'<rect x="2" y="2" width="60" height="60" rx="10" fill="{fill}"/>'
        f'<text x="32" y="{32 + size * 0.36:.0f}" text-anchor="middle" '
        f'font-family="Helvetica, Arial, sans-serif" font-weight="700" font-size="{size}" '
        f'fill="#ffffff">{glyph}</text></symbol>'
    )
