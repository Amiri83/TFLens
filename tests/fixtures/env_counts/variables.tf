variable "env" {
  type        = string
  description = "environment name; no default: set per environment"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "lambda_count" {
  type        = number
  description = "instances of each base-module function; no default: set per environment"
}

variable "service_image" {
  type        = string
  description = "container image of the ECS service; no default: set per environment"
}

variable "enable_canary" {
  type    = bool
  default = false
}
