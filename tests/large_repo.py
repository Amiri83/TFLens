"""A generated Terraform repository large enough to stress the Graph view (generic names
only): one root calling the same local module N times; the module declares eight
resources all named ``this`` (queue, function, role, policy, trigger, log group, bucket,
topic), wired by a trigger (queue -> function) and IAM access (function -> queue,
bucket, topic)."""

from __future__ import annotations

from pathlib import Path

MODULE = """\
variable "name" {
  type = string
}

resource "aws_sqs_queue" "this" {
  name = "${var.name}-jobs"
}

resource "aws_s3_bucket" "this" {
  bucket = "${var.name}-data"
}

resource "aws_sns_topic" "this" {
  name = "${var.name}-events"
}

resource "aws_cloudwatch_log_group" "this" {
  name = "/aws/lambda/${var.name}"
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "this" {
  role = aws_iam_role.this.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage"], Resource = aws_sqs_queue.this.arn },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.this.arn}/*" },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.this.arn },
    ]
  })
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  role          = aws_iam_role.this.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "lambda.zip"
}

resource "aws_lambda_event_source_mapping" "this" {
  event_source_arn = aws_sqs_queue.this.arn
  function_name    = aws_lambda_function.this.arn
}
"""


def write_large_repo(root: Path, modules: int = 40) -> Path:
    """Write the repository under ``root``: ``modules`` calls x 8 resources each."""
    (root / "modules" / "svc").mkdir(parents=True, exist_ok=True)
    (root / "modules" / "svc" / "main.tf").write_text(MODULE)
    calls = [
        'terraform {\n  backend "s3" {\n    bucket = "example-tfstate"\n'
        '    key    = "app/terraform.tfstate"\n    region = "us-east-1"\n  }\n}\n',
        'provider "aws" {\n  region = "us-east-1"\n}\n',
    ]
    for i in range(1, modules + 1):
        calls.append(
            f'module "svc_{i:02d}" {{\n  source = "./modules/svc"\n  name   = "example-svc-{i:02d}"\n}}\n'
        )
    (root / "main.tf").write_text("\n".join(calls))
    return root
