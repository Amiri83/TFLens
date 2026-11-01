terraform {
  backend "s3" {}
}

provider "aws" {
  region = "us-east-1"
}

variable "env" {
  type = string
}

variable "handlers" {
  type    = set(string)
  default = []
}

module "handlers" {
  source   = "../../modules/api_fn"
  for_each = var.handlers
  name     = "${var.env}-${each.key}"
}
