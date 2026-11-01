terraform {
  backend "s3" {}
}

provider "aws" {
  region = "us-east-1"
}

variable "env" {
  type = string
}

resource "aws_lambda_function" "edge" {
  function_name = "${var.env}-edge"
  handler       = "edge.handler"
  runtime       = "python3.12"
  role          = "example-role"
  filename      = "lambda.zip"
}
