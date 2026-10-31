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
  type        = string
  description = "environment name"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
  tags = {
    Name = "vpc-a"
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id       = aws_vpc.main.id
  service_name = "com.amazonaws.us-east-1.s3"
  tags = {
    Name = "ep-a"
  }
}

resource "aws_ecr_repository" "app" {
  name = "ecr-a"
}

resource "aws_route53_zone" "main" {
  name = "repo-a.example.com"
}

resource "aws_acm_certificate" "main" {
  domain_name       = "repo-a.example.com"
  validation_method = "DNS"
  tags = {
    Name = "cert-a"
  }
}

resource "aws_kms_key" "main" {
  description = "key-a"
}

resource "aws_kms_alias" "main" {
  name          = "alias/key-a"
  target_key_id = aws_kms_key.main.key_id
}

resource "aws_s3_bucket" "logs" {
  bucket = "bucket-log-a-${var.env}"
}

resource "aws_iam_role" "deploy" {
  name = "repo-a-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_ssm_parameter" "ecr_url" {
  name  = "/repo-a/ecr-url"
  type  = "String"
  value = aws_ecr_repository.app.repository_url
}
