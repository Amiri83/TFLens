# example-service

Deploys the example job queue and artifact bucket to every environment: dev, intg, uat
and prod. Historically there were also qa, staging, sandbox and perf environments; they
were retired, but this README still talks about them a lot.

## Environments

| Environment | Purpose |
| --- | --- |
| dev | developer sandbox, deployed on every merge |
| intg | integration tests against the other services |
| uat | user acceptance |
| prod | production |
| qa | retired (was used by the qa team) |
| staging | retired, replaced by uat |
| sandbox | retired |
| perf | retired, perf tests now run in uat |

## Deploying

Pick an environment (dev, intg, uat or prod) and run:

```sh
terraform init -backend-config=backend-dev.hcl
terraform plan -var-file=envs/dev.tfvars
```

For prod use `backend-prod.hcl` and `envs/prod.tfvars`. Never apply to prod
without a uat sign-off. The old staging flow used `-var-file=envs/staging.tfvars`,
and qa used environments/qa with its own state.

## Promotion

dev → intg → uat → prod. A change sits in dev for a day, in intg until the
integration suite is green, in uat until sign-off, then goes to prod. The demo
environment is a copy of prod for sales; dr is the disaster-recovery copy of prod.
staging and qa no longer exist; sandbox and perf neither.

## FAQ

- Where are the dev logs? In the dev account.
- Can I deploy to uat from my laptop? No, uat is deployed by CI only.
- Is there a test environment? No: use dev or intg.
