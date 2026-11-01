terraform {
  required_version = ">= 1.5"

  # partial configuration: bucket / key / region come from backend.<env>.hcl
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
  source       = "./modules/lambda_wrapper"
  name         = "${var.env}-api"
  lambda_count = var.lambda_count
}

module "worker" {
  source       = "./modules/lambda_wrapper"
  name         = "${var.env}-worker"
  lambda_count = var.lambda_count
}

module "scheduler" {
  source       = "./modules/lambda_wrapper"
  name         = "${var.env}-scheduler"
  lambda_count = var.lambda_count
}

module "notifier" {
  source       = "./modules/lambda_wrapper"
  name         = "${var.env}-notifier"
  lambda_count = var.lambda_count
}

# One ECS service; the canary service is only created when enable_canary is set.
resource "aws_ecs_cluster" "main" {
  name = "${var.env}-cluster"
}

resource "aws_iam_role" "task" {
  name               = "${var.env}-task"
  assume_role_policy = "{}"
}

resource "aws_ecs_task_definition" "app" {
  family        = "${var.env}-app"
  task_role_arn = aws_iam_role.task.arn
  container_definitions = jsonencode([
    {
      name  = "app"
      image = var.service_image
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = "${var.env}-app"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
}

resource "aws_ecs_service" "canary" {
  count           = var.enable_canary ? 1 : 0
  name            = "${var.env}-app-canary"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
}
