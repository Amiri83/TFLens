# A reusable module nothing in this repository calls: not deployed from here.
variable "name" {
  type = string
}

resource "aws_lambda_function" "legacy" {
  function_name = var.name
  handler       = "index.handler"
  runtime       = "python3.12"
  role          = "example-role"
  filename      = "lambda.zip"
}
