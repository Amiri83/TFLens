provider "aws" {
  region = "us-east-1"
}

module "queue" {
  source = "../.."
  name   = "example-queue"
}

resource "aws_s3_bucket" "example" {
  bucket = "example-bucket"
}
