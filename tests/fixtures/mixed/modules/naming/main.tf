variable "name" {
  type        = string
  description = "short name"
}

variable "env" {
  type    = string
  default = "dev"
}

output "full_name" {
  value = "repo-a-${var.env}-${var.name}"
}
