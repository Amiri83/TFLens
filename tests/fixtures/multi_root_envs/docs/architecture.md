# repo-a architecture

Each environment root (envs/dev, envs/prod) deploys the app module: queue-a feeds fn-a.

## Network

The app runs in a VPC with one security group open to 192.0.2.0/24 on 443.

## Alerts

stacks/shared publishes alerts to an SNS topic subscribed by an SQS queue.
