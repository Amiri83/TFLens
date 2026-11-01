# TFLens

Static, read-only analyzer for Terraform repositories. It parses `.tf`, `.tf.json`,
`.tfvars`, `.tfvars.json` and `terragrunt.hcl` files (python-hcl2 and its own JSON
reader, both with exact line numbers), evaluates what can be determined from the code (variables,
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
tflens                           # no arguments: the web UI, same as `tflens serve`
```

| Option | Meaning |
| --- | --- |
| `--dir PATH` | repository to analyze (required) |
| `--out FILE` | write the model JSON |
| `--report FILE` | write the markdown report |
| `--overview FILE` | write the markdown overview (see below) |
| `--handoff FILE` | write `handoff.md`: the overview plus the diagrams as Mermaid (for Claude Code) |
| `--diagrams DIR` | write the diagrams as `.mmd` and `.svg` (and `.png` when an offline renderer exists) |
| `--no-git` | do not read the git history |
| `--var-file FILE` | extra tfvars for root modules (relative to `--dir`), repeatable |
| `--var NAME=VALUE` | set a root variable, repeatable |

With none of `--out`, `--report` or `--overview`, the model JSON goes to stdout.
If the repository has nothing to analyze, `tflens` exits with status 1 and prints
the reason and a listing of what the repository contains.

## Zero-config onboarding

Nothing has to be configured before a first analysis:

- **Discovery is never silent.** Every directory that is not walked is listed with
  the reason: unreadable (the `os.walk` error), tool / VCS / hidden directories
  (`.git`, `.terraform`, ...) and symlinks. If no `*.tf`, `*.tf.json` or
  `terragrunt.hcl` file is found the run **fails** with that reason and a listing
  of the top two directory levels with file counts by extension.
- **Roots**: directories that are not called as a module by another directory
  (their `backend` / `cloud` / `provider` / `terraform` blocks are listed as the
  signals), and Terragrunt units. A unit's `terraform { source }` is resolved
  (`get_repo_root()`, `get_terragrunt_dir()`, `find_in_parent_folders()`,
  `path_relative_to_include()`, literal locals); a local source is analyzed with
  the unit's `inputs` (merged with `include`-d parents) as variable values, a
  remote one is reported with its kind and version.
- **Backend** per root: type and its key settings (s3 `bucket` / `key` / `region` /
  `dynamodb_table`, gcs, azurerm, remote / cloud, ...), from the `backend` block, a
  `*.tfbackend` partial configuration, or Terragrunt `remote_state`; a setting that
  cannot be known is `UNKNOWN` with the reason. Plus the workspace setup.
- **Providers**: `required_providers` constraints per module, versions locked in
  `.terraform.lock.hcl`, `required_version`, and `provider` configurations.
- **Modules**: every call, classified local / registry / git / s3 / gcs / http,
  with its version (`version` or `?ref=`), call site and inputs.
- **Environments**: from tfvars files (`envs/dev.tfvars`, `vars/prod.tfvars`,
  with `common.tfvars` applied to each), `envs/*` or env-named root directories,
  and Terragrunt layouts (`live/<env>/...`, `env.hcl`). In the managed app they
  are created automatically (on add / Sync / **Detect environments**), bound to
  their root, and can be edited; an edited environment is never overwritten.
- **Root view** (the analyzer's first tab, the `## Roots by shape` section of
  `handoff.md`): each root is classified by shape, with the reason (resource counts and
  backend) spelled out, and shown in the view that shape calls for:
  *app / pipeline* (compute fed by triggers or queues) → **Flow**;
  *platform / bootstrap* (ECR, DNS, certificates, KMS, IAM, log buckets, SSM
  parameters, little to no compute) → **Provides**: what it creates, the outputs it
  exposes, who reads them (`terraform_remote_state` sources whose backend settings name
  its state, `aws_ssm_parameter` data sources) and its environments;
  *network* (VPC, subnets, security groups, endpoints, transit gateways) → **Network**:
  VPC → subnets → what is placed in them, security groups, endpoints, and what can
  reach what, with a diagram (VPC → subnets → security groups → the instances, ECS
  services, load balancers, Lambda functions in them; the security group rules are the
  numbered, labelled edges, public ones in red); *module library* (no backend, no
  provider, nothing calls it) → **Module catalog**: inputs, outputs, resources, an
  example `module` block and who calls it. Every root also has an **IAM** view: each
  role / user / group → its attached and inline policies → what they grant (*can
  write* / *can read* / *can access* / *denies*), as a list and a diagram. Every box of
  these diagrams shows its `file:line`. A mixed repository lists its roots grouped by
  shape, each with a one-line description; a root switcher and a view switcher pick
  any root and any view.
  `/api/shapes`, `/api/root-view?root=<dir>&view=<flow|provides|network|iam|catalog>`.
- **Flow** (the analyzer's second tab, the `## Flows` section of `handoff.md`): how
  data moves, entry point to sink. Entry points are the declared triggers nothing in
  the repository writes to (Kafka / MSK / SQS / Kinesis / DynamoDB event source
  mappings, SNS subscriptions, S3 notifications, EventBridge rules, API Gateway
  integrations, load balancer listeners → target groups → ECS services, EC2 instances,
  Auto Scaling groups), plus **every compute** (Lambda, ECS service, EC2 instance, Auto
  Scaling group, ...) that no chain from an entry point reaches: it starts its own
  flow, marked *(no trigger)*, so every compute is in exactly one flow as a start or a
  later step, never twice as a start. The flows are grouped under **Lambda / ECS /
  EC2 / Network** (by the first compute a chain reaches; *Network* = routing with no
  compute behind it), and the header shows *covered / total compute*. Under each
  compute hangs what it runs in and as: ECS cluster → task definition → containers →
  task / execution role; instance profile → role; ASG → launch template → instance
  profile → role. A compute
  **sends to** / **reads from** a queue, topic, table, bucket, stream or secret when it
  has both a reference to it (environment variable, ECS container `environment` /
  `secrets`) and an IAM write / read action on it; a reference alone is *unverified*,
  an IAM grant alone *can write* / *can read*. Each chain is numbered, every step with
  the `file:line` of each piece of evidence, and drawn left to right (fonts ≥ 14px, the
  panel scrolls). Dead-letter queues, alarms and log groups are side branches. A
  *Start here* box lists the root, the entry files and the modules / files in flow
  order; the metrics (flows, steps, unverified links) are in the header. The resource
  inventory follows, collapsed under *All resources*.
- **Overview** (the analyzer's third tab, `overview.md` of each run): what the
  repository deploys and, per root, backend, providers, modules, resources by
  service, the top ACCESS / TRIGGER / NETWORK relationships and the unknowns.
  Every line links to its `file:line`.

## Deep crawl

Besides the Terraform code, every analysis crawls (read-only, offline, no LLM; the only
I/O is reading files and one `git log`):

- **Docs**: READMEs down to two directory levels and the files at the top of `docs/`:
  title, headings and the first prose paragraph (badges, HTML, tables and code skipped),
  each with its `file:line`.
- **CI**: GitHub Actions (`.github/workflows/*.yml`; `.github`, `.gitlab`, `.circleci`
  and `.buildkite` are walked although hidden), GitLab CI, Jenkinsfile, Atlantis
  (`projects` with dir / workspace), CircleCI, Bitbucket and Azure Pipelines: the
  `terraform` / `terragrunt` / `tofu` / `tflint` / ... commands with their line.
- **Lock files**: the provider versions of every `.terraform.lock.hcl`.
- **Git history**: when the analyzed directory is a git checkout (or, for a managed
  repository with a sub-directory, inside one), the last 200 commits touching it
  (sha, author, date, message, changed files relative to the analyzed directory), the
  most-changed files and the authors. Nothing is fetched; `GIT_OPTIONAL_LOCKS=0`. A git
  repository synced by TFLens is a shallow clone, so it has one commit.
- Unreadable or oversized files are listed with the reason; a failed `git log` is
  reported with git's message. Nothing is skipped silently.

The **classification** adds, per root, a role (`deployable`, or `library` for a directory
nobody calls that has no backend, provider configuration or tfvars), and a catalog of
every module directory (`root` / `child` / `terragrunt-source` / `remote`) with its call
sites.

The **Overview** also has: a *Purpose (guess)* (README title and first paragraph, then
e.g. "deploys 2 Lambda functions, 1 SQS queue, 1 DynamoDB table and 1 S3 bucket", the CI
that applies it), a drill-down per resource (what it is, triggered by, triggers, can
access, accessed by, network, and the defining code), the docs, the CI and lock files,
and the history (recent commits, most-changed files). Every line links to `file:line`.

## Diagrams

Three diagrams, each as Mermaid (`.mmd`) and a standalone SVG computed by TFLens itself
(deterministic, no browser or renderer needed); the **Diagrams** tab shows them with
download links:

- **architecture**: the services grouped by root and module, with only the TRIGGER /
  ACCESS / NETWORK flows, laid out triggers → compute → data, with AWS-style icons
  (TFLens's own simplified drawings in the AWS category colours; drop `<icon>.svg`
  files, e.g. the official AWS Architecture Icons under their licence, into
  `static/vendor/aws-icons/` to replace them, see `icons.py` for the names).
- **module-tree**: each root and the modules it calls, with kind, source and version.
- **env-matrix**: root × environment, with each root's backend.

A PNG is written only when an offline SVG renderer is installed (`rsvg-convert`,
`cairosvg` or `inkscape`); otherwise the run says so and the SVG is the export.

## Run history and Changes

Each managed run stores a snapshot in the database (`run_files`, `run_modules`,
`run_resources`, `run_edges`, `run_envs`, `run_backends`, keyed by the run, which records
the repository and the commit). The **Changes** page of a run (`/runs/<id>/changes`,
linked from the Runs table and the analyzer) diffs it against the previous run of the
same repository and environment, or any other run: added / removed / changed resources
(with the changed properties), edges, module calls (source and version changes listed
first), environments, backends and files. Runs recorded before snapshots existed are
back-filled from their `model.json`.

Every run also writes `handoff.md` (flows + overview + the three diagrams as Mermaid + the
changes since the previous run) and `diagrams/` next to its `model.json`; both can be
downloaded from the Runs table. `handoff.md` is meant to be given to Claude Code as
context (`@handoff.md`).

The report ends with an **Evidence** section: every relationship gets an entry
(`E1`, `E2`, ... linked from the resource sections) with file, line range and
the source lines, evidence lines marked `>` and two lines of context around them.

## Web UI

`tflens serve` runs a local web app (Flask on waitress). It has two modes.

### Standalone: one repository, no data directory

```sh
tflens serve --dir path/to/terraform            # http://127.0.0.1:8765/
tflens serve --dir path/to/terraform --port 0 --no-browser
```

| Option | Meaning |
| --- | --- |
| `--dir PATH` | repository (selects this mode) |
| `--host HOST` / `--port N` | bind address (default `127.0.0.1:8765`; `--port 0` picks a free port) |
| `--model FILE` | model.json to serve (default `<dir>/tflens-out/model.json`) |
| `--regenerate` | re-analyze at startup even if the model exists |
| `--no-browser` | do not open a browser |
| `--allow-remote` | required to bind a non-loopback `--host` |
| `--var-file`, `--var` | as for the analyzer, used when (re)generating the model |

This mode never creates or reads `~/.tflens` and needs no database (`tflens --dir`
doesn't either). The server re-reads model.json when it changes and regenerates
it when it is missing or unusable; the **Regenerate** button re-analyzes on demand.

### Managed: repositories, environments and run history

```sh
tflens serve                     # data directory $TFLENS_HOME or ~/.tflens
tflens                           # the same (also `python -m tflens`)
tflens serve --home /srv/tflens --port 9000 --no-browser
```

The data directory (mode 700) holds `tflens.db` (SQLite), `secret.key` (Fernet
key, or set `TFLENS_SECRET_KEY`), `flask.secret`, `logs/`, `repos/` (git clones)
and `out/` (runs). The database and the key are created **lazily**, on the
first save; just starting the server or browsing writes neither.

- **Repos**: a local directory (analyzed in place, never written to) or a git URL
  with optional branch and sub-directory. **Sync** of a git repository is the only
  network operation TFLens performs, and only when you click it: a shallow
  `git clone --depth 1` into `repos/<id>` (no prompts, https/ssh only). Git auth is
  the SSH agent, an SSH key file, or an access token. The token is stored
  Fernet-encrypted and goes to git only through its environment
  (`http.extraHeader`). It is never put on the command line or in a file, never
  logged, and never shown. URLs with embedded credentials are refused.
- **Environments** per repository: tfvars files plus extra `-var KEY=VALUE` lines
  (values stored encrypted, only names shown). **Analyze** runs the static
  analyzer read-only and records a run.
- **Runs**: history per repository. A run's `model.json`, `report.md` and
  `overview.md` stay on disk under `out/<repo>/<run>/`; the database stores only
  the path, summary counts and status. **Open** shows the run in the analyzer at
  `/runs/<id>/` (read-only). A failed run is listed with its reason (and, when
  nothing was analyzable, what the repository contains).
- **Accounts**: AWS account records in the same form as IPLens (encrypted or
  memory-only secrets, masked display). The analysis never uses them; TFLens makes
  no AWS calls.
- **Settings**: log directory; server host / port / allow-remote (applied at the
  next start, and the `--host` / `--port` / `--allow-remote` flags take precedence);
  auto-regenerate (analyze every environment after a successful Sync); how many
  runs to keep per repository × environment.
- **Logs**: the last N entries of the rotating log file. Credentials and tokens
  are redacted before they are written.

### Analyzer and security (both modes)

- **Graph**: nodes colored by service, edges by kind (CONFIG dashed, RUNS_ON
  dotted); filters by edge kind, service and module; search by name or id.
- **Overview**: see above; a `file:line` opens the source snippet.
- **Repository**: the whole repository tree (analyzed files marked, skipped
  directories with their reason, roots tagged); selecting a file lists what it
  defines.
- **Detail panel** (click a node or edge): summary, the defining block, evidence
  snippets with highlighted line numbers, and unknowns with their reasons.

The server binds to loopback unless allowed otherwise. It serves only requests
whose Host header names it (DNS-rebinding guard), checks a CSRF token on every
form POST, and sends a `default-src 'self'` CSP on every response. The UI makes
no external requests; the graph library is vendored (copy `cytoscape.min.js` into
`src/tflens/static/vendor/`).

JSON API (all GET; under `/runs/<id>/` for a run): `api/summary`, `api/graph`,
`api/nodes`, `api/edges`, `api/node?id=`, `api/edge?id=`, `api/tree`,
`api/file?path=`, `api/overview`, `api/source?file=&start=&end=`, `api/model`,
`api/diagram?name=architecture|module-tree|env-matrix&format=svg|mmd`,
`api/diagram?name=flow-<n>|network|iam&format=svg[&root=<dir>]`, `api/handoff`
(add `download=1` for an attachment); `POST api/regenerate` (standalone only, needs the
header `X-TFLens: 1`).

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
  the Lambda functions, EC2 instances (via instance profile) and ECS services (via
  task definition and task role) that run as it.
- **TRIGGER**: event source mappings (self-managed Kafka topics included), SNS
  subscriptions, S3 notifications, EventBridge rule targets, API Gateway
  integrations, load balancer listener → target group → ECS service / attachment.
- **NETWORK**: security group ingress/egress rules, SG membership, VPC endpoints.
- **CONFIG**: attribute references between resources, including Lambda
  environment variables and ECS container environment / secrets (each pointing at
  the line that sets the variable).
- **RUNS_ON**: instance → AMI, Lambda → runtime or container image.

## model.json

`schema_version`, `summary`, `files`, `parse_errors`, `roots`, `remote_modules`,
`modules` (module tree with inputs, outputs and remote source/version), `variables` (value and where it came from),
`nodes`, `edges` (with `evidence` and `via`), `unknowns` (subject, attribute,
reason, location), `discovery` (status / failure reason, analyzable and all files,
skipped directories, inventory), `structure` (roots with role, backend, providers,
modules, resources; environments; module directories) and `crawl` (docs, CI, lock
files, git history, notes, and the extra files the UI may show). Symbolic values are encoded explicitly: `{"$ref": ...}`,
`{"$unknown": reason}`, `{"$template": ...}`, `{"$json": ...}`.

## Development

```sh
pytest
ruff check .
ruff format --check .
```

Fixture repositories live in `tests/fixtures/<service>/` and use placeholder
names and account IDs only.
