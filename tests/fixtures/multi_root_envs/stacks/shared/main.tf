terraform {
  backend "gcs" {
    bucket = "repo-a-state"
    prefix = "stacks/shared"
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type = string
}

variable "env" {
  type = string
}

resource "aws_sns_topic" "alerts" {
  name = "repo-a-${var.env}-alerts"
}

resource "aws_sqs_queue" "alerts" {
  name = "repo-a-${var.env}-alerts"
}

resource "aws_sns_topic_subscription" "alerts" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.alerts.arn
}
