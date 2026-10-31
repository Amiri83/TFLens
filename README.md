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

The report ends with an **Evidence** section: every relationship gets an entry
(`E1`, `E2`, ... linked from the resource sections) with file, line range and
the source lines, evidence lines marked `>` and two lines of context around them.

## Web UI

```sh
tflens serve --dir path/to/terraform            # http://127.0.0.1:8765/
tflens serve --dir path/to/terraform --port 0 --no-browser
```

| Option | Meaning |
| --- | --- |
| `--dir PATH` | repository (required) |
| `--host HOST` / `--port N` | bind address (default `127.0.0.1:8765`; `--port 0` picks a free port) |
| `--model FILE` | model.json to serve (default `<dir>/tflens-out/model.json`) |
| `--regenerate` | re-analyze at startup even if the model exists |
| `--no-browser` | do not open a browser |
| `--allow-remote` | required to bind a non-loopback `--host` |
| `--var-file`, `--var` | as for the analyzer, used when (re)generating the model |

The server (standard library `http.server`, no extra dependencies) re-reads
model.json when it changes and regenerates it when it is missing or unusable;
the **Regenerate** button re-analyzes on demand. It makes no external requests:
pages are served with a `default-src 'self'` CSP and the graph library is
vendored (copy `cytoscape.min.js` into `src/tflens/static/vendor/`).

- **Graph**: nodes colored by service, edges by kind (CONFIG dashed, RUNS_ON
  dotted); filters by edge kind, service and module; search by name or id.
- **Repository**: file/module tree; selecting a file lists what it defines.
- **Detail panel** (click a node or edge): summary, the defining block, evidence
  snippets with highlighted line numbers, and unknowns with their reasons.

JSON API (all GET): `/api/summary`, `/api/graph`, `/api/nodes`, `/api/edges`,
`/api/node?id=`, `/api/edge?id=`, `/api/tree`, `/api/file?path=`, `/api/model`;
`POST /api/regenerate` (needs header `X-TFLens: 1`).

## Remote modules

If `<root>/.terraform/modules/modules.json` exists (written by a previous
`terraform init`), TFLens reads the modules it lists from their materialized
directories under `.terraform/modules/` and analyzes them like local modules
(inputs, outputs, nested modules). Nothing is downloaded. Calls with no
manifest, no manifest entry, or a missing directory stay unknown with the
reason. `model.json` lists the manifest entries under `remote_modules`.

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

`schema_version`, `summary`, `files`, `parse_errors`, `roots`, `remote_modules`,
`modules` (module tree with inputs, outputs and remote source/version), `variables` (value and where it came from),
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
