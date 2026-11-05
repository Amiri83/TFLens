terraform {
  backend "s3" {}
}

variable "env" {
  type = string
}

variable "base_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

locals {
  vpc_name = "example-${var.env}-vpc"
}

resource "aws_vpc" "main" {
  cidr_block = var.base_cidr
  tags       = { Name = local.vpc_name }
}

resource "aws_subnet" "private" {
  count      = 2
  vpc_id     = aws_vpc.main.id
  cidr_block = cidrsubnet(var.base_cidr, 8, count.index)
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "vpc_name" {
  value = local.vpc_name
}

output "subnet_cidr" {
  value = cidrsubnet(var.base_cidr, 8, 1)
}
