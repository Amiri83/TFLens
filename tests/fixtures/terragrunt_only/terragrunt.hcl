# Root configuration: included by every unit under live/.
remote_state {
  backend = "s3"
  config = {
    bucket         = "repo-a-tfstate"
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "repo-a-tflock"
  }
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<EOF
provider "aws" {
  region = "us-east-1"
}
EOF
}

inputs = {
  project = "repo-a"
}
