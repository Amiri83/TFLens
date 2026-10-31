terraform {
  backend "s3" {
    bucket = "repo-a-tfstate"
    key    = "kafka-flow/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = "us-east-1"
}
