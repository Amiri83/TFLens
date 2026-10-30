from __future__ import annotations

from tflens.analyze import pattern_of
from tflens.policy import statement_spans
from tflens.values import Interp, Ref, Unknown


def test_statement_spans_hcl_and_json():
    hcl = [
        "policy = jsonencode({",
        "  Statement = [",
        '    { Effect = "Allow", Action = "s3:*" },',
        "    {",
        '      Effect = "Deny"',
        '      Condition = { Bool = { "aws:SecureTransport" = "false" } }',
        "    }",
        "  ]",
        "})",
    ]
    assert statement_spans(hcl, 10) == [(12, 12), (13, 16)]
    json_text = ['{"Statement": {"Effect": "Allow",', ' "Action": "sqs:*"}}']
    assert statement_spans(json_text, 1) == [(1, 2)]
    assert statement_spans(["no statements here"], 1) == []


def test_pattern_of_replaces_unknown_parts():
    acct = Ref("data.aws_caller_identity.c", "account_id")
    assert (
        pattern_of(Interp(("arn:aws:sqs:us-east-1:", acct, ":jobs")))
        == "arn:aws:sqs:us-east-1:*:jobs"
    )
    assert pattern_of(Ref("aws_s3_bucket.b", "bucket", "data", True)) == "data"
    assert pattern_of(Unknown("nope")) is None
    assert pattern_of(Interp((acct,))) is None
