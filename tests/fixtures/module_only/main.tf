terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket = "repo-a-tfstate"
    key    = "module-only/terraform.tfstate"
    region = "us-east-1"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "env" {
  type    = string
  default = "dev"
}

module "label" {
  source = "./modules/label"
  name   = "repo-a"
  env    = var.env
}

module "queue" {
  source  = "terraform-aws-modules/sqs/aws"
  version = "4.2.0"
  name    = "queue-a-${var.env}"
  tags    = module.label.tags
}

module "table" {
  source = "git::https://git.example.com/org/repo-a-modules.git//dynamodb?ref=v2.0.1"
  name   = "table-a-${var.env}"
}
