include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "git::https://git.example.com/org/repo-a-modules.git//vpc?ref=v1.2.0"
}

inputs = {
  env  = "dev"
  cidr = "192.0.2.0/24"
}
