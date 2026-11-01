env           = "prod"
region        = "us-east-1"
vpc_cidr      = "10.1.0.0/16"
private_cidrs = ["10.1.1.0/24", "10.1.2.0/24", "10.1.3.0/24"]
azs           = ["us-east-1a", "us-east-1b", "us-east-1c"]
extra_tags = {
  criticality = "high"
}
