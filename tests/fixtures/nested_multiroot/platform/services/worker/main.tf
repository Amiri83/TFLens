terraform {
  backend "s3" {
    bucket = "example-tfstate-shared"
    key    = "platform/services/worker.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      environment = var.env
      component   = "worker"
    }
  }
}

data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "example-tfstate-${var.env}"
    key    = "platform/network/${var.env}.tfstate"
    region = var.region
  }
}

data "terraform_remote_state" "billing" {
  backend = "s3"
  config = {
    bucket = "example-tfstate-shared"
    key    = "billing/terraform.tfstate"
    region = var.region
  }
}

resource "aws_instance" "worker" {
  count                  = var.instance_count
  ami                    = var.ami_id
  instance_type          = "t3.micro"
  subnet_id              = element(data.terraform_remote_state.network.outputs.private_subnet_ids, count.index)
  vpc_security_group_ids = [data.terraform_remote_state.network.outputs.app_security_group_id]

  tags = {
    Name        = "worker-${count.index}"
    cost_center = data.terraform_remote_state.billing.outputs.cost_center
  }
}
