terraform {
  required_version = ">= 1.5"

  # partial configuration: bucket / key / region come from backend-<env>.hcl
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

resource "aws_sqs_queue" "jobs" {
  name = "${var.env}-example-jobs"
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.env}-example-artifacts"
}
