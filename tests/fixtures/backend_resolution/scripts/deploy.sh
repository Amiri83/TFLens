#!/bin/sh
# deploys a stack
ENV="$1"
cd stacks/batch && terraform init -backend-config="bucket=example-tfstate-$ENV" \
  -backend-config="key=batch/terraform.tfstate" -backend-config="region=us-east-1"
