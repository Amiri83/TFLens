# kafka-a (self-managed) topic-a -> fn-a (classifier)

resource "aws_secretsmanager_secret" "kafka" {
  name = "secret-a"
}

resource "aws_lambda_event_source_mapping" "kafka" {
  function_name     = aws_lambda_function.classifier.arn
  topics            = ["topic-a"]
  starting_position = "TRIM_HORIZON"

  self_managed_event_source {
    endpoints = {
      KAFKA_BOOTSTRAP_SERVERS = "192.0.2.10:9092,192.0.2.11:9092"
    }
  }

  source_access_configuration {
    type = "BASIC_AUTH"
    uri  = aws_secretsmanager_secret.kafka.arn
  }
}

resource "aws_lambda_function" "classifier" {
  function_name = "fn-a"
  role          = aws_iam_role.classifier.arn
  runtime       = "python3.12"
  handler       = "app.handler"
  filename      = "build/fn-a.zip"

  environment {
    variables = {
      QUEUE_URL  = aws_sqs_queue.work.url
      SECRET_ARN = aws_secretsmanager_secret.kafka.arn
    }
  }
}

resource "aws_cloudwatch_log_group" "classifier" {
  name              = "/aws/lambda/fn-a"
  retention_in_days = 14
}
