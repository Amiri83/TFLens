# TFLens

Static, read-only analyzer for Terraform repositories. It parses `.tf`/`.tfvars`
files (python-hcl2), evaluates what can be determined from the code (variables,
tfvars, locals, local modules and their outputs, `aws_iam_policy_document`), and
builds a graph of AWS resources and their relationships, each relationship
backed by evidence (file, line range, snippet).

No `terraform` binary, no `terraform init`, no AWS API calls, no LLM. Anything
that cannot be resolved statically is reported as **unknown, with a reason**,
never guessed. Output is deterministic: the same input gives byte-identical
output.

## Usage

```sh
pip install -e ".[dev]"
tflens --dir path/to/terraform --out model.json --report report.md
```

| Option | Meaning |
| --- | --- |
| `--dir PATH` | repository to analyze (required) |
| `--out FILE` | write the model JSON |
| `--report FILE` | write the markdown report |
| `--var-file FILE` | extra tfvars for root modules (relative to `--dir`), repeatable |
| `--var NAME=VALUE` | set a root variable, repeatable |

With neither `--out` nor `--report`, the model JSON goes to stdout.

## What it extracts

Resources: `aws_instance`, `aws_lambda_function` (+ event source mappings,
permissions), `aws_sqs_queue` (+ queue policy), `aws_sns_topic` (+ subscriptions,
topic policy), `aws_dynamodb_table`, `aws_s3_bucket` (+ bucket policy,
notifications, versioning, public access block, encryption), IAM roles, users,
groups, policies, inline policies, attachments and instance profiles,
`aws_security_group` (+ `aws_security_group_rule`, `aws_vpc_security_group_*_rule`),
`aws_vpc_endpoint`. Every other resource still appears as a node with CONFIG
edges.

Edge kinds:

- **ACCESS**: IAM action → resource, from identity policies (role/user/group,
  inline or attached) and resource policies (trust, bucket, queue, topic,
  endpoint, lambda permission). Access granted to a role is also attributed to
  the Lambda functions and EC2 instances (via instance profile) that run as it.
- **TRIGGER**: event source mappings, SNS subscriptions, S3 notifications.
- **NETWORK**: security group ingress/egress rules, SG membership, VPC endpoints.
- **CONFIG**: attribute references between resources, including Lambda
  environment variables.
- **RUNS_ON**: instance → AMI, Lambda → runtime or container image.

## model.json

`schema_version`, `summary`, `files`, `parse_errors`, `roots`, `modules` (module
tree with inputs and outputs), `variables` (value and where it came from),
`nodes`, `edges` (with `evidence` and `via`), `unknowns` (subject, attribute,
reason, location). Symbolic values are encoded explicitly: `{"$ref": ...}`,
`{"$unknown": reason}`, `{"$template": ...}`, `{"$json": ...}`.

## Development

```sh
pytest
ruff check .
ruff format --check .
```

Fixture repositories live in `tests/fixtures/<service>/` and use placeholder
names and account IDs only.
