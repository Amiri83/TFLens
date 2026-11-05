# Every value here is provably resolvable from this repository, except var.never_set.
terraform {
  backend "s3" {}
}

variable "env_name" {
  type = string
}

variable "tier" {
  type    = string
  default = "standard"
}

variable "never_set" {
  type = string
}

locals {
  prefix = "example-${var.env_name}"
}

resource "aws_sqs_queue" "per_env" {
  name = var.env_name
}

resource "aws_sqs_queue" "from_local" {
  name = "${local.prefix}-jobs"
}

resource "aws_sns_topic" "from_default" {
  name = var.tier
}

resource "aws_s3_bucket" "unresolvable" {
  bucket = var.never_set
}
