resource "aws_dynamodb_table" "orders" {
  name             = "example-orders"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "customer_id"
  range_key        = "order_id"
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  attribute {
    name = "customer_id"
    type = "S"
  }

  attribute {
    name = "order_id"
    type = "S"
  }

  global_secondary_index {
    name            = "by-order"
    hash_key        = "order_id"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_iam_role" "reader" {
  name = "example-orders-reader"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_policy" "orders_read" {
  name = "example-orders-read"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
      Resource = [
        "arn:aws:dynamodb:*:*:table/example-orders",
        "arn:aws:dynamodb:*:*:table/example-orders/index/*",
        "arn:aws:dynamodb:*:*:table/example-orders/stream/*",
      ]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "reader" {
  role       = aws_iam_role.reader.name
  policy_arn = aws_iam_policy.orders_read.arn
}

resource "aws_lambda_function" "projector" {
  function_name = "example-projector"
  role          = aws_iam_role.reader.arn
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/projector.zip"
}

resource "aws_lambda_event_source_mapping" "orders_stream" {
  event_source_arn  = aws_dynamodb_table.orders.stream_arn
  function_name     = "example-projector"
  starting_position = "LATEST"
}
