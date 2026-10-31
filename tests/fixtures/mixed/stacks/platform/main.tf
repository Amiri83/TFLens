terraform {
  backend "s3" {
    bucket = "bucket-a"
    key    = "platform/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "env" {
  type = string
}

resource "aws_ecr_repository" "app" {
  name = "ecr-a"
}

resource "aws_kms_key" "main" {
  description = "key-a"
}

resource "aws_acm_certificate" "main" {
  domain_name       = "repo-a.example.com"
  validation_method = "DNS"
}

resource "aws_s3_bucket" "logs" {
  bucket = "bucket-log-a-${var.env}"
}

resource "aws_ssm_parameter" "ecr_url" {
  name  = "/repo-a/ecr-url"
  type  = "String"
  value = aws_ecr_repository.app.repository_url
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "log_bucket" {
  value = aws_s3_bucket.logs.bucket
}
