terraform {
  required_version = ">= 1.5.0"

  # key and dynamodb_table come from backend.tfbackend (terraform init -backend-config)
  backend "s3" {
    bucket = "repo-a-tfstate"
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
