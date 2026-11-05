# Handoff

Deploy staging with `terraform init -backend-config=stacks/app/envs/backend-staging.hcl`,
and wire prod with `terraform plan -var-file=stacks/app/envs/prod.tfvars`.
