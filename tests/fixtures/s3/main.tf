resource "aws_s3_bucket" "data" {
  bucket = "example-data"
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_iam_role" "analytics" {
  name               = "example-analytics"
  assume_role_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"sts:AssumeRole\",\"Principal\":{\"Service\":\"glue.amazonaws.com\"}}]}"
}

resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AnalyticsRead",
      "Effect": "Allow",
      "Principal": { "AWS": "${aws_iam_role.analytics.arn}" },
      "Action": ["s3:GetObject"],
      "Resource": "${aws_s3_bucket.data.arn}/*"
    }
  ]
}
EOF
}

resource "aws_sqs_queue" "ingest" {
  name = "example-ingest"
}

resource "aws_lambda_function" "thumbs" {
  function_name = "example-thumbs"
  role          = "arn:aws:iam::123456789012:role/example-thumbs"
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/thumbs.zip"
}

resource "aws_s3_bucket_notification" "data" {
  bucket = aws_s3_bucket.data.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.thumbs.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "images/"
  }

  queue {
    queue_arn = aws_sqs_queue.ingest.arn
    events    = ["s3:ObjectRemoved:*"]
  }
}
