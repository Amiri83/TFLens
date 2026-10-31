terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket         = "repo-a-tfstate"
    key            = "envs/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "repo-a-tflock"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["123456789012"]
}

variable "env" {
  type = string
}

module "network" {
  source = "../../modules/network"
  env    = var.env
  cidr   = "192.0.2.0/24"
}

module "app" {
  source            = "../../modules/app"
  env               = var.env
  security_group_id = module.network.security_group_id
}

module "label" {
  source  = "cloudposse/label/null"
  version = "0.25.0"
  name    = "repo-a"
  stage   = var.env
}

module "alarms" {
  source = "git::https://git.example.com/org/repo-a-modules.git//alarms?ref=v0.3.1"
  env    = var.env
}
