variable "name" {
  type = string
}

variable "lambda_count" {
  type = number
}

variable "use_base_module" {
  type    = bool
  default = true
}

module "base" {
  source        = "../lambda_base"
  count         = var.use_base_module ? 1 : 0
  function_name = var.name
  lambda_count  = var.lambda_count
}

# legacy inline function, kept for callers not migrated to the base module: exactly one
# of module.base and aws_lambda_function.legacy is created per call
resource "aws_iam_role" "legacy" {
  count              = var.use_base_module ? 0 : 1
  name               = "${var.name}-legacy"
  assume_role_policy = "{}"
}

resource "aws_lambda_function" "legacy" {
  count         = var.use_base_module ? 0 : 1
  function_name = var.name
  role          = aws_iam_role.legacy[0].arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "legacy.zip"
}
