terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

variable "env" {
  type = string
}

variable "security_group_id" {
  type = string
}

resource "aws_sqs_queue" "jobs" {
  name = "repo-a-${var.env}-jobs"
}

resource "aws_iam_role" "fn" {
  name = "repo-a-${var.env}-fn-a"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "fn" {
  role = aws_iam_role.fn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
      Resource = aws_sqs_queue.jobs.arn
    }]
  })
}

resource "aws_lambda_function" "fn" {
  function_name = "fn-a-${var.env}"
  role          = aws_iam_role.fn.arn
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/fn-a.zip"

  vpc_config {
    subnet_ids         = ["subnet-00000000000000001"]
    security_group_ids = [var.security_group_id]
  }
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.fn.arn
}
