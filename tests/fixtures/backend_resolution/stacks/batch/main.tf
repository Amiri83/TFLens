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
  name = "batch-${var.env}-jobs-${data.aws_region.current.name}"
}

variable "owner" {
  type = string
}

variable "ticket" {
  type = string
}

resource "aws_sns_topic" "events" {
  name = "batch-${var.env}-${var.owner}"
  display_name = var.ticket
}
