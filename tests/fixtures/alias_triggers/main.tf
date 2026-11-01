# Lambda functions triggered through aliases, an SNS topic passed in as an ARN
# variable, and Kafka (self-managed and MSK) event source mappings.

variable "env" {
  type        = string
  description = "environment name; no default: set per environment"
}

variable "alerts_topic_arn" {
  type        = string
  description = "ARN of an SNS topic created outside this repository"
}

variable "kafka_bootstrap_servers" {
  type    = string
  default = "ip-10-0-0-1:9092"
}

locals {
  prefix = "${var.env}-example-platform"
}

resource "aws_iam_role" "lambda" {
  name               = "${local.prefix}-lambda"
  assume_role_policy = "{}"
}

# --- an SQS queue triggering a function through its alias
resource "aws_sqs_queue" "orders" {
  name = "${local.prefix}-orders-queue"
}

resource "aws_lambda_function" "orders" {
  function_name = "${local.prefix}-orders-processor-with-a-long-name"
  role          = aws_iam_role.lambda.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_lambda_alias" "orders_live" {
  name             = "live"
  function_name    = aws_lambda_function.orders.function_name
  function_version = "$LATEST"
}

resource "aws_lambda_event_source_mapping" "orders" {
  event_source_arn = aws_sqs_queue.orders.arn
  function_name    = aws_lambda_alias.orders_live.arn
}

# --- an SNS topic created elsewhere, passed in as an ARN variable
resource "aws_lambda_function" "alerts" {
  function_name = "${local.prefix}-alerts"
  role          = aws_iam_role.lambda.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_sns_topic_subscription" "alerts" {
  topic_arn = var.alerts_topic_arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.alerts.arn
}

# --- self-managed Kafka, on an alias
resource "aws_lambda_function" "ingest" {
  function_name = "${local.prefix}-ingest"
  role          = aws_iam_role.lambda.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_lambda_alias" "ingest_live" {
  name             = "live"
  function_name    = aws_lambda_function.ingest.arn
  function_version = "$LATEST"
}

resource "aws_lambda_event_source_mapping" "ingest" {
  function_name     = aws_lambda_alias.ingest_live.arn
  topics            = ["example-events"]
  starting_position = "TRIM_HORIZON"

  self_managed_event_source {
    endpoints = {
      KAFKA_BOOTSTRAP_SERVERS = var.kafka_bootstrap_servers
    }
  }
}

# --- MSK, on an alias
resource "aws_msk_cluster" "events" {
  cluster_name           = "${local.prefix}-events"
  kafka_version          = "3.6.0"
  number_of_broker_nodes = 3

  broker_node_group_info {
    instance_type   = "kafka.m5.large"
    client_subnets  = []
    security_groups = []
  }
}

resource "aws_lambda_function" "audit" {
  function_name = "${local.prefix}-audit"
  role          = aws_iam_role.lambda.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}

resource "aws_lambda_alias" "audit_live" {
  name             = "live"
  function_name    = aws_lambda_function.audit.function_name
  function_version = "$LATEST"
}

resource "aws_lambda_event_source_mapping" "audit" {
  event_source_arn  = aws_msk_cluster.events.arn
  function_name     = aws_lambda_alias.audit_live.arn
  topics            = ["example-audit"]
  starting_position = "LATEST"
}
