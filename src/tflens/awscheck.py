"""Phase 3 of discovery (optional, off by default): do the confirmed resources exist?

Only when the run asks for it *and* an AWS account is configured: an :class:`AwsTarget`
built from that account's own credentials (a named profile, the standard credential chain
of an "Environment" account, or the account's stored keys, held in memory only). The
credentials are never written to a file, never put on a command line, never logged.

Every call is read-only (``sts:GetCallerIdentity``, ``s3:HeadBucket``,
``sqs:GetQueueUrl``, ``dynamodb:DescribeTable``, ``lambda:GetFunctionConfiguration``,
``sns:GetTopicAttributes``, ``ecr:DescribeRepositories``), one per resource whose name is
known statically. Without boto3, without an account, or when the credentials do not work,
the phase is skipped with a clear message: never a crash, never a made-up result.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MAX_CHECKS = 200
# resource type -> (service, name attribute)
CHECKS: dict[str, tuple[str, str]] = {
    "aws_s3_bucket": ("s3", "bucket"),
    "aws_sqs_queue": ("sqs", "name"),
    "aws_dynamodb_table": ("dynamodb", "name"),
    "aws_lambda_function": ("lambda", "function_name"),
    "aws_sns_topic": ("sns", "name"),
    "aws_ecr_repository": ("ecr", "name"),
}
# error codes that mean "no such resource" (anything else: the check failed)
MISSING_CODES = frozenset(
    {
        "404",
        "NoSuchBucket",
        "NotFound",
        "AWS.SimpleQueueService.NonExistentQueue",
        "QueueDoesNotExist",
        "ResourceNotFoundException",
        "RepositoryNotFoundException",
    }
)
NO_BOTO = "boto3 is not installed (pip install 'tflens[aws]'): AWS check skipped"
OFF = "not requested for this run (off by default)"


@dataclass
class AwsTarget:
    """The account a run checks against. ``session`` builds a boto3 session from the
    account's own credentials; ``problem`` says why it cannot be used ('' when it can)."""

    label: str
    region: str
    session: Callable[[], Any] | None = None
    problem: str = ""


def boto3_available() -> bool:
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    return True


def target_from_account(account: Any) -> AwsTarget:
    """An :class:`AwsTarget` for a stored account (:class:`accounts.Account` fetched with
    its secret). Its secrets stay in the closure, in memory."""
    label = account.display_name or f"account {account.id}"
    problem = account.credential_problem()
    if not problem and not boto3_available():
        problem = NO_BOTO
    if problem:
        return AwsTarget(label, account.region, None, problem)
    mode, region = account.auth_mode, account.region
    profile = account.profile
    key_id, secret, token = (
        account.access_key_id,
        account.secret_access_key,
        account.session_token,
    )

    def session() -> Any:
        import boto3

        if mode == "profile":
            return boto3.Session(profile_name=profile or None, region_name=region)
        if mode in ("keys", "temporary"):
            return boto3.Session(
                aws_access_key_id=key_id,
                aws_secret_access_key=secret,
                aws_session_token=token or None,
                region_name=region,
            )
        return boto3.Session(region_name=region)  # the standard credential chain

    return AwsTarget(label, region, session)


def target_from_profile(profile: str, region: str = "") -> AwsTarget:
    """``tflens serve --dir ... --aws-profile NAME``: a named profile of the AWS config."""
    if not boto3_available():
        return AwsTarget(f"profile {profile}", region, None, NO_BOTO)

    def session() -> Any:
        import boto3

        return boto3.Session(profile_name=profile, region_name=region or None)

    return AwsTarget(f"profile {profile}", region, session)


def _code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str((resp.get("Error") or {}).get("Code") or "")
    return ""


def _name(node: dict[str, Any], attr: str) -> str | None:
    value = (node.get("attributes") or {}).get(attr)
    if isinstance(value, str) and value and "${" not in value:
        return value
    return None


def candidates(model: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """The confirmed resources a check can name, and how many were left out (their name
    is not known statically)."""
    out, unnamed = [], 0
    for n in model.get("nodes", []):
        if n.get("kind") != "resource" or n.get("type") not in CHECKS or not n.get("location"):
            continue
        service, attr = CHECKS[n["type"]]
        name = _name(n, attr)
        if name is None:
            unnamed += 1
            continue
        out.append({"id": n["id"], "type": n["type"], "service": service, "name": name})
    return out, unnamed


def _call(client: Any, service: str, name: str, ident: dict[str, str]) -> None:
    if service == "s3":
        client.head_bucket(Bucket=name)
    elif service == "sqs":
        client.get_queue_url(QueueName=name)
    elif service == "dynamodb":
        client.describe_table(TableName=name)
    elif service == "lambda":
        client.get_function_configuration(FunctionName=name)
    elif service == "sns":
        arn = f"arn:{ident['partition']}:sns:{ident['region']}:{ident['account']}:{name}"
        client.get_topic_attributes(TopicArn=arn)
    elif service == "ecr":
        client.describe_repositories(repositoryNames=[name])


def _result(status: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "message": message, "checks": [], **extra}


def run_checks(
    model: dict[str, Any], target: AwsTarget | None, requested: bool, progress: Any
) -> dict[str, Any]:
    """The ``pipeline.aws`` section: ``off`` (not asked for), ``skipped`` (asked for, not
    possible: why), ``done`` (each check: exists / missing / error with the error code)."""
    if not requested:
        return _result("off", OFF)
    if target is None:
        return _result(
            "skipped",
            "no AWS account is configured for this run (add one under Accounts, or start "
            "with --aws-profile): AWS check skipped",
        )
    if target.problem or target.session is None:
        return _result("skipped", f"{target.label}: {target.problem or 'not usable'}")
    items, unnamed = candidates(model)
    progress.step(f"connecting to AWS ({target.label}, read-only)")
    try:
        session = target.session()
        ident_raw = session.client("sts").get_caller_identity()
    except Exception as exc:  # no credentials, expired, network: say so, never fake
        code = _code(exc) or type(exc).__name__
        log.warning("AWS check: cannot connect with %s: %s", target.label, code)
        return _result("skipped", f"{target.label}: cannot connect ({code}): AWS check skipped")
    arn = str(ident_raw.get("Arn", ""))
    ident = {
        "account": str(ident_raw.get("Account", "")),
        "partition": arn.split(":")[1] if arn.count(":") >= 2 else "aws",
        "region": target.region or getattr(session, "region_name", "") or "us-east-1",
    }
    checks = []
    clients: dict[str, Any] = {}
    for item in items[:MAX_CHECKS]:
        progress.step(f"AWS: {item['type']} {item['name']}")
        try:
            client = clients.get(item["service"])
            if client is None:
                client = clients[item["service"]] = session.client(item["service"])
            _call(client, item["service"], item["name"], ident)
            status, detail = "exists", ""
        except Exception as exc:
            code = _code(exc) or type(exc).__name__
            status, detail = ("missing" if code in MISSING_CODES else "error"), code
        checks.append({**item, "status": status, "detail": detail})
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("exists", "missing", "error")}
    message = (
        f"{target.label}: {counts['exists']} exist, {counts['missing']} missing, "
        f"{counts['error']} not checkable"
    )
    if unnamed:
        message += f"; {unnamed} resource(s) not checked (name not known statically)"
    if len(items) > MAX_CHECKS:
        message += f"; only the first {MAX_CHECKS} checked"
    return {
        "status": "done",
        "message": message,
        "checks": checks,
        "counts": counts,
        "unnamed": unnamed,
    }
