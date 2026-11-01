env-counts: one root applied to three environments (dev, uat, prod).

The backend is partial (`backend "s3" {}`); each environment's bucket / key come from
`backend.<env>.hcl`, passed by CI as `-backend-config`, and its variables from
`environments/<env>.tfvars`, passed as `-var-file`.

The four Lambda functions each go through `modules/lambda_wrapper`, which calls the shared
`modules/lambda_base` (`count = var.use_base_module ? 1 : 0`); the base module defines the
function with `count = var.lambda_count`. The wrapper also keeps a legacy inline function
(`count = var.use_base_module ? 0 : 1`): only one of the two blocks is created per call.
The canary ECS service is created only when `enable_canary` is set.

| env  | ECS services | Lambda functions | unknowns |
|------|--------------|------------------|----------|
| dev  | 1            | 4                | 0        |
| uat  | 1            | 4                | 0        |
| prod | 1            | 4                | 0        |
| total| 3            | 12               | 0        |
