terraform {
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = module.labels.tags
  }
}

module "labels" {
  source      = "../../../modules/labels"
  environment = var.env
  component   = "api"
}

locals {
  tags = merge(module.labels.tags, { tier = "backend" })
}

data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "example-tfstate-${var.env}"
    key    = "platform/network/${var.env}.tfstate"
    region = var.region
  }
}

module "jobs" {
  source   = "../../../modules/queue"
  name     = "${var.env}-jobs"
  with_dlq = true
  tags     = local.tags
}

resource "aws_iam_role" "api" {
  name = "${var.env}-api"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_lambda_function" "api" {
  function_name = "${var.env}-api"
  role          = aws_iam_role.api.arn
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/api.zip"

  vpc_config {
    subnet_ids         = [data.terraform_remote_state.network.outputs.private_subnet_ids[0]]
    security_group_ids = [data.terraform_remote_state.network.outputs.app_security_group_id]
  }

  environment {
    variables = {
      QUEUE_URL = module.jobs.queue_url
      VPC_ID    = data.terraform_remote_state.network.outputs.vpc_id
    }
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "api" {
  count             = var.enable_logs ? 1 : 0
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}
