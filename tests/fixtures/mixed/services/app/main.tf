terraform {
  backend "s3" {
    bucket = "bucket-a"
    key    = "app/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = "us-east-1"
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config = {
    bucket = "bucket-a"
    key    = "platform/terraform.tfstate"
    region = "us-east-1"
  }
}

data "aws_ssm_parameter" "ecr_url" {
  name = "/repo-a/ecr-url"
}

resource "aws_sqs_queue" "work" {
  name              = "queue-a"
  kms_master_key_id = data.terraform_remote_state.platform.outputs.kms_key_arn
}

resource "aws_lambda_function" "worker" {
  function_name = "fn-a"
  role          = "arn:aws:iam::123456789012:role/repo-a-fn-a"
  package_type  = "Image"
  image_uri     = "${data.aws_ssm_parameter.ecr_url.value}:latest"
}

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.worker.arn
}
