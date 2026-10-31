variable "prefix" {
  type = string
}

output "bucket_name" {
  value = "${var.prefix}-bucket-a"
}
