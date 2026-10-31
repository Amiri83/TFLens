module "queues" {
  source  = "example-org/queues/aws"
  version = "1.2.0"
  name    = "queue-a"
}

module "notify" {
  source = "git::https://example.com/notify.git?ref=v1"
}

module "absent" {
  source  = "example-org/absent/aws"
  version = "0.1.0"
}

resource "aws_iam_role" "fn" {
  name = "role-a"
}

resource "aws_iam_role_policy" "fn" {
  role = aws_iam_role.fn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage"]
      Resource = module.queues.queue_arn
    }]
  })
}

resource "aws_lambda_function" "fn" {
  function_name = "fn-a"
  role          = aws_iam_role.fn.arn
  runtime       = "python3.12"
  handler       = "app.handler"
}

resource "aws_lambda_event_source_mapping" "fn" {
  event_source_arn = module.queues.queue_arn
  function_name    = aws_lambda_function.fn.arn
}

output "queue_name" {
  value = module.queues.queue_name
}

output "notify_topic" {
  value = module.notify.topic_arn
}
