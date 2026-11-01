variable "env" {
  type        = string
  description = "environment name; no default: set per environment"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "lambdas_enabled" {
  type        = bool
  description = "use the base module for the functions; no default: set per environment"
}

variable "alerts_enabled" {
  type        = bool
  description = "create the alerts topic; no default: set per environment"
}
