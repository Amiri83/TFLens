terraform {
  backend "s3" {
    bucket = "example-tfstate-shared"
    key    = "shared/terraform.tfstate"
    region = "us-east-1"
  }
}

output "org_prefix" {
  value = "example"
}
