include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "tfr:///terraform-aws-modules/lambda/aws?version=7.2.0"
}

dependency "vpc" {
  config_path = "../vpc"
}

inputs = {
  function_name = "fn-a-prod"
  vpc_id        = dependency.vpc.outputs.vpc_id
}
