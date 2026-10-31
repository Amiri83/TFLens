variable "name" {
  type = string
}

variable "env" {
  type = string
}

locals {
  id = "${var.name}-${var.env}"
}

output "id" {
  value = local.id
}

output "tags" {
  value = { Name = local.id, Environment = var.env }
}
