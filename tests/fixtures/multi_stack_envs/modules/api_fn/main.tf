# A wrapper module: its call and the function inside modules/lambda are one function.
variable "name" {
  type = string
}

module "fn" {
  source  = "../lambda"
  name    = var.name
  handler = "api.handler"
}

output "arn" {
  value = module.fn.arn
}
