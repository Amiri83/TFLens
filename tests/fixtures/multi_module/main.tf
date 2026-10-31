variable "env" {
  type = string
}

variable "project" {
  type    = string
  default = "example"
}

variable "alert_email" {
  type        = string
  description = "No default and not set in tfvars: stays unknown"
}

locals {
  prefix = "${var.project}-${var.env}"
  tags = {
    Project     = var.project
    Environment = var.env
  }
}

data "aws_caller_identity" "current" {}

module "storage" {
  source      = "./modules/storage"
  bucket_name = "${local.prefix}-data"
  tags        = local.tags
}

module "worker" {
  source     = "./modules/worker"
  name       = "${local.prefix}-worker"
  bucket_arn = module.storage.bucket_arn
  queue_arn  = "arn:aws:sqs:us-east-1:${data.aws_caller_identity.current.account_id}:${local.prefix}-jobs"
}

module "network" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.0.0"
  name    = local.prefix
}

resource "aws_sqs_queue" "jobs" {
  name = "${local.prefix}-jobs"
}

resource "aws_sns_topic" "alerts" {
  name = "${local.prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sqs_queue" "per_tenant" {
  for_each = toset(["a", "b"])
  name     = "${local.prefix}-${each.key}"
}

output "worker_function" {
  value = module.worker.function_name
}
