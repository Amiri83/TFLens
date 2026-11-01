variable "name" {
  type = string
}

variable "enabled" {
  type = bool
}

module "base" {
  source        = "../lambda_base"
  count         = var.enabled ? 1 : 0
  function_name = var.name
}

# legacy inline function for callers not migrated to the base module: exactly one of
# module.base and aws_lambda_function.legacy is created per call
resource "aws_lambda_function" "legacy" {
  count         = var.enabled ? 0 : 1
  function_name = var.name
  role          = "arn:aws:iam::123456789012:role/example"
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "legacy.zip"
}
