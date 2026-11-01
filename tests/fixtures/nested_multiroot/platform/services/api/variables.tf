variable "env" {
  type = string
}

variable "region" {
  type = string
}

variable "enable_logs" {
  type = bool
}

variable "log_retention_days" {
  type    = number
  default = 14
}
