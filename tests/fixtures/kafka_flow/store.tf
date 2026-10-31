# queue-a -> fn-b (writer) -> table-a

resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = aws_lambda_function.writer.arn
  batch_size       = 10
}

resource "aws_lambda_function" "writer" {
  function_name = "fn-b"
  role          = aws_iam_role.writer.arn
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/fn-b.zip"

  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.orders.name
      BUCKET_NAME = aws_s3_bucket.archive.bucket
    }
  }
}

resource "aws_dynamodb_table" "orders" {
  name         = "table-a"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}

resource "aws_s3_bucket" "archive" {
  bucket = "bucket-a"
}

resource "aws_sns_topic" "events" {
  name = "topic-b"
}
