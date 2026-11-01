# Flow / diagram follow-ups: a compute reached from a declared trigger and from a
# no-trigger compute (expanded once), a function that reads and writes one table (two
# parallel steps), two rules from one CIDR, a VPC whose lines skip columns.
# Placeholder names, the documentation range 192.0.2.0/24 (RFC 5737) only.

# ------------------------------------------------------------------ network

resource "aws_vpc" "vpc-a" {
  cidr_block = "192.0.2.0/24"
}

resource "aws_subnet" "subnet-a" {
  vpc_id     = aws_vpc.vpc-a.id
  cidr_block = "192.0.2.0/25"
}

resource "aws_security_group" "sg-a" {
  name   = "sg-a"
  vpc_id = aws_vpc.vpc-a.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["192.0.2.0/24"]
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["192.0.2.0/24"]
  }
}

# ------------------------------------------------------------------ data

resource "aws_sqs_queue" "queue-a" {
  name = "queue-a"
}

resource "aws_dynamodb_table" "table-a" {
  name         = "table-a"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}

# ------------------------------------------------------------------ roles

data "aws_iam_policy_document" "lambda-assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "role-a" {
  name               = "role-a"
  assume_role_policy = data.aws_iam_policy_document.lambda-assume.json
}

resource "aws_iam_role_policy" "role-a" {
  name = "role-a-data"
  role = aws_iam_role.role-a.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.table-a.arn
      },
    ]
  })
}

resource "aws_iam_role" "role-b" {
  name               = "role-b"
  assume_role_policy = data.aws_iam_policy_document.lambda-assume.json
}

resource "aws_iam_role_policy" "role-b" {
  name = "role-b-send"
  role = aws_iam_role.role-b.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.queue-a.arn
    }]
  })
}

# ------------------------------------------------------------------ Lambda

# fed by an EventBridge schedule and by queue-a: reads and writes table-a
resource "aws_lambda_function" "lambda-a" {
  function_name = "lambda-a"
  role          = aws_iam_role.role-a.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  filename      = "lambda.zip"

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.table-a.name
    }
  }

  vpc_config {
    subnet_ids         = [aws_subnet.subnet-a.id]
    security_group_ids = [aws_security_group.sg-a.id]
  }
}

resource "aws_cloudwatch_event_rule" "rule-a" {
  name                = "rule-a"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "rule-a" {
  rule = aws_cloudwatch_event_rule.rule-a.name
  arn  = aws_lambda_function.lambda-a.arn
}

resource "aws_lambda_event_source_mapping" "queue-a" {
  event_source_arn = aws_sqs_queue.queue-a.arn
  function_name    = aws_lambda_function.lambda-a.arn
}

# no trigger declared: sends to queue-a, which feeds lambda-a
resource "aws_lambda_function" "lambda-b" {
  function_name = "lambda-b"
  role          = aws_iam_role.role-b.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  filename      = "lambda.zip"

  environment {
    variables = {
      QUEUE_URL = aws_sqs_queue.queue-a.url
    }
  }
}
