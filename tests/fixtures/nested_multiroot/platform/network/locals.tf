locals {
  base_tags = {
    environment = var.env
    owner       = "platform-team"
    managed_by  = "terraform"
  }
  common_tags = merge(local.base_tags, var.extra_tags)
  name        = join("-", [var.name_prefix, var.env])
}
