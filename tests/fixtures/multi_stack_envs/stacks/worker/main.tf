terraform {
  backend "s3" {}
}

provider "aws" {
  region = "us-east-1"
}

variable "env" {
  type = string
}

variable "worker_enabled" {
  type    = bool
  default = false
}

module "worker" {
  source = "../../modules/lambda"
  count  = var.worker_enabled ? 1 : 0
  name   = "${var.env}-worker"
}
