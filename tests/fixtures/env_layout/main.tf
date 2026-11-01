terraform {
  required_version = ">= 1.5"

  # partial configuration: bucket / key / region / dynamodb_table come from env/<env>.hcl
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.region
}

# Four functions, each through the wrapper module, which calls the shared base module.
module "api" {
  source  = "./modules/lambda_wrapper"
  name    = "${var.env}-api"
  enabled = var.lambdas_enabled
}

module "worker" {
  source  = "./modules/lambda_wrapper"
  name    = "${var.env}-worker"
  enabled = var.lambdas_enabled
}

module "scheduler" {
  source  = "./modules/lambda_wrapper"
  name    = "${var.env}-scheduler"
  enabled = var.lambdas_enabled
}

module "notifier" {
  source  = "./modules/lambda_wrapper"
  name    = "${var.env}-notifier"
  enabled = var.lambdas_enabled
}

resource "aws_ecs_cluster" "main" {
  name = "${var.env}-cluster"
}

resource "aws_ecs_task_definition" "app" {
  family = "${var.env}-app"
  container_definitions = jsonencode([
    {
      name  = "app"
      image = "example.invalid/app:${var.env}"
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = "${var.env}-app"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
}

resource "aws_ecr_repository" "app" {
  name = "${var.env}-app"
}

resource "aws_lb" "public" {
  name               = "${var.env}-public"
  load_balancer_type = "application"
}

resource "aws_sqs_queue" "jobs" {
  name = "${var.env}-jobs"
}

resource "aws_dynamodb_table" "items" {
  name         = "${var.env}-items"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}

resource "aws_cloudwatch_event_rule" "nightly" {
  name                = "${var.env}-nightly"
  schedule_expression = "cron(0 2 * * ? *)"
}

# alerts: set per environment (no default)
resource "aws_sns_topic" "alerts" {
  count = var.alerts_enabled ? 1 : 0
  name  = "${var.env}-alerts"
}

# only in prod
resource "aws_s3_bucket" "archive" {
  count  = var.env == "prod" ? 1 : 0
  bucket = "example-${var.env}-archive"
}
