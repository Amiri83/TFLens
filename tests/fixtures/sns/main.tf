resource "aws_sns_topic" "alerts" {
  name = "example-alerts"
}

resource "aws_sqs_queue" "alerts" {
  name = "example-alerts-queue"
}

resource "aws_lambda_function" "notifier" {
  function_name = "example-notifier"
  role          = "arn:aws:iam::123456789012:role/example-notifier"
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/notifier.zip"
}

resource "aws_sns_topic_subscription" "queue" {
  topic_arn            = aws_sns_topic.alerts.arn
  protocol             = "sqs"
  endpoint             = aws_sqs_queue.alerts.arn
  raw_message_delivery = true
}

resource "aws_sns_topic_subscription" "fn" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.notifier.arn
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = "arn:aws:sns:us-east-1:123456789012:example-alerts"
  protocol  = "email"
  endpoint  = "alerts@example.com"
}

data "aws_iam_policy_document" "alerts" {
  statement {
    sid       = "AllowEventBridge"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}
