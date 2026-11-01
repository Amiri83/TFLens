resource "aws_lambda_function" "under_test" {
  function_name = "test-fn"
  handler       = "index.handler"
  runtime       = "python3.12"
  role          = "example-role"
  filename      = "lambda.zip"
}
