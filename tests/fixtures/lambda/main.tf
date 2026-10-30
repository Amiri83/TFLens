resource "aws_dynamodb_table" "items" {
  name         = "example-items"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}

resource "aws_sqs_queue" "jobs" {
  name = "example-jobs"
}

resource "aws_s3_bucket" "uploads" {
  bucket = "example-uploads"
}

resource "aws_security_group" "fn" {
  name = "example-fn"
}

resource "aws_iam_role" "fn" {
  name = "example-fn"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "fn" {
  role = aws_iam_role.fn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.items.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.jobs.arn
      }
    ]
  })
}

resource "aws_lambda_function" "worker" {
  function_name = "example-worker"
  role          = aws_iam_role.fn.arn
  runtime       = "nodejs20.x"
  handler       = "index.handler"
  filename      = "build/worker.zip"
  memory_size   = 256

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.items.name
      LOG_LEVEL  = "info"
    }
  }

  vpc_config {
    subnet_ids         = ["subnet-00000000000000001"]
    security_group_ids = [aws_security_group.fn.id]
  }
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.worker.arn
  batch_size       = 10
}

resource "aws_lambda_permission" "s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.worker.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.uploads.arn
}
