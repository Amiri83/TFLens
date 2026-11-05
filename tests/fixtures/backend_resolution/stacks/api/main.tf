terraform {
  backend "s3" {}
}

provider "aws" {
  region = "us-east-1"
}

variable "env" {
  type = string
}

data "aws_region" "current" {}

resource "aws_sqs_queue" "jobs" {
  name = "api-${var.env}-jobs-${data.aws_region.current.name}"
}
