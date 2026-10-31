variable "name" {
  type = string
}

output "full_name" {
  value = "repo-a-${var.name}"
}
