variable "env" {
  type = string
}

variable "region" {
  type = string
}

variable "name_prefix" {
  type    = string
  default = "example"
}

variable "vpc_cidr" {
  type = string
}

variable "private_cidrs" {
  type = list(string)
}

variable "azs" {
  type = list(string)
}

variable "extra_tags" {
  type    = map(string)
  default = {}
}
