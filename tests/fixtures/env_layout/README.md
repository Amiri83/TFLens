# env_layout (test fixture)

One root with a partial backend (`backend "s3" {}`) and per-environment files in `env/`:

- `env/<env>.tfvars` for dev, uat, prod and intg
- `env/<env>.hcl`: the backend configuration of dev, uat and prod (bucket / key / region /
  dynamodb_table), passed by the deploy workflow as `-backend-config`
- intg has tfvars only: no backend configuration, not in the workflow matrix (not wired)

Per environment: 1 ECS service and 4 Lambda functions, each through a wrapper module that
calls a base module with `count = var.enabled ? 1 : 0` (the wrapper's legacy function has
the opposite count). The archive bucket is only created in prod; intg does not set
`alerts_enabled`, so its alerts topic is uncertain. Placeholder names only.
