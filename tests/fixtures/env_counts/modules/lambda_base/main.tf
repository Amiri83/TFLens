variable "function_name" {
  type = string
}

variable "lambda_count" {
  type = number
}

variable "memory_size" {
  type    = number
  default = 128
}

resource "aws_iam_role" "this" {
  name               = var.function_name
  assume_role_policy = "{}"
}

resource "aws_lambda_function" "example" {
  count         = var.lambda_count
  function_name = var.function_name
  role          = aws_iam_role.this.arn
  handler       = "index.handler"
  runtime       = "python3.12"
  memory_size   = var.memory_size
  filename      = "function.zip"
}

output "function_names" {
  value = aws_lambda_function.example[*].function_name
}
