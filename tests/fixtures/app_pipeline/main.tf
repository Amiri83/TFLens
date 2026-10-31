terraform {
  backend "s3" {
    bucket = "bucket-a"
    key    = "app-pipeline/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_sns_topic" "events" {
  name = "topic-a"
}

resource "aws_sqs_queue" "work" {
  name = "queue-a"
}

resource "aws_sns_topic_subscription" "work" {
  topic_arn = aws_sns_topic.events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.work.arn
}

resource "aws_lambda_function" "worker" {
  function_name = "fn-a"
  role          = aws_iam_role.worker.arn
  handler       = "app.handler"
  runtime       = "python3.12"
  filename      = "fn-a.zip"

  environment {
    variables = {
      BUCKET_NAME = aws_s3_bucket.out.bucket
    }
  }
}

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
}

resource "aws_s3_bucket" "out" {
  bucket = "bucket-a"
}

resource "aws_iam_role" "worker" {
  name = "repo-a-fn-a"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.work.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.out.arn}/*"
      }
    ]
  })
}
