variable "environment" {
  type = string
}

variable "component" {
  type = string
}

variable "extra" {
  type    = map(string)
  default = {}
}

locals {
  tags = merge(
    {
      environment = var.environment
      component   = var.component
      managed_by  = "terraform"
    },
    var.extra,
  )
}

output "tags" {
  value = local.tags
}
