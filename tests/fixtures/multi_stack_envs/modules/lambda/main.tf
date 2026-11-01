variable "name" {
  type = string
}

variable "handler" {
  type    = string
  default = "index.handler"
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-role"
  assume_role_policy = "{}"
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  handler       = var.handler
  runtime       = "python3.12"
  role          = aws_iam_role.this.arn
  filename      = "lambda.zip"
}

output "arn" {
  value = aws_lambda_function.this.arn
}
