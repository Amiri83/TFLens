variable "name" {
  type = string
}

variable "visibility_timeout" {
  type    = number
  default = 30
}

variable "with_dlq" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
