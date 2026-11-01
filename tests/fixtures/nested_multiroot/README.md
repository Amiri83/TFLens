# Example platform

Three stacks deployed separately, each with its own state per environment.

## Network stack (`platform/network`)

Creates the VPC `aws_vpc.main`, the private subnets `aws_subnet.private` and a NAT
gateway `aws_nat_gateway.main` for outbound traffic.

## API stack (`platform/services/api`)

Runs the `aws_lambda_function.api` function as `aws_iam_role.api`. It keeps its sessions in
the `aws_dynamodb_table.sessions` table and reads the network outputs through
`data.terraform_remote_state.network`.

```sh
terraform init -backend-config=envs/backend-dev.hcl
terraform plan -var-file=envs/dev.tfvars
```
