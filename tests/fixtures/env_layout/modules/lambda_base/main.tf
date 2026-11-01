variable "function_name" {
  type = string
}

resource "aws_iam_role" "this" {
  name               = var.function_name
  assume_role_policy = "{}"
}

resource "aws_cloudwatch_log_group" "this" {
  name = "/aws/lambda/${var.function_name}"
}

resource "aws_lambda_function" "example" {
  function_name = var.function_name
  role          = aws_iam_role.this.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  filename      = "function.zip"
}
