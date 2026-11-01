# A flow per compute: 7 compute resources, only some fed by a declared trigger.
# Placeholder names and the placeholder account 123456789012 only.

# ------------------------------------------------------------------ network

resource "aws_vpc" "vpc-a" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_subnet" "subnet-a" {
  vpc_id            = aws_vpc.vpc-a.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "us-east-1a"
}

resource "aws_subnet" "subnet-b" {
  vpc_id            = aws_vpc.vpc-a.id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "us-east-1b"
}

resource "aws_security_group" "sg-lb" {
  name   = "sg-lb"
  vpc_id = aws_vpc.vpc-a.id

  ingress {
    description = "public https"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "sg-app" {
  name   = "sg-app"
  vpc_id = aws_vpc.vpc-a.id

  ingress {
    description     = "from the load balancer"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.sg-lb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "sg-app-ssh" {
  type              = "ingress"
  security_group_id = aws_security_group.sg-app.id
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = ["10.9.0.0/16"]
}

# ------------------------------------------------------------------ data

resource "aws_sqs_queue" "queue-a" {
  name = "queue-a"
}

resource "aws_sqs_queue" "queue-b" {
  name = "queue-b"
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

resource "aws_s3_bucket" "bucket-a" {
  bucket = "bucket-a"
}

# ------------------------------------------------------------------ Lambda

data "aws_iam_policy_document" "lambda-assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "role-lambda" {
  name               = "role-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda-assume.json
}

resource "aws_iam_role_policy" "role-lambda" {
  name = "role-lambda-data"
  role = aws_iam_role.role-lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.table-a.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.bucket-a.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage"]
        Resource = aws_sqs_queue.queue-a.arn
      },
    ]
  })
}

resource "aws_iam_role" "role-scheduled" {
  name               = "role-scheduled"
  assume_role_policy = data.aws_iam_policy_document.lambda-assume.json
}

resource "aws_iam_role_policy" "role-scheduled" {
  name = "role-scheduled-read"
  role = aws_iam_role.role-scheduled.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem"]
      Resource = aws_dynamodb_table.table-a.arn
    }]
  })
}

resource "aws_iam_role" "role-orphan" {
  name               = "role-orphan"
  assume_role_policy = data.aws_iam_policy_document.lambda-assume.json
}

resource "aws_iam_role_policy" "role-orphan" {
  name = "role-orphan-send"
  role = aws_iam_role.role-orphan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = aws_sqs_queue.queue-b.arn
    }]
  })
}

resource "aws_iam_role" "role-idle" {
  name               = "role-idle"
  assume_role_policy = data.aws_iam_policy_document.lambda-assume.json
}

# fed by queue-a (which the ASG writes to): a later step of the ASG's flow
resource "aws_lambda_function" "lambda-classifier" {
  function_name = "lambda-classifier"
  role          = aws_iam_role.role-lambda.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  filename      = "lambda.zip"

  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.table-a.name
      BUCKET_NAME = aws_s3_bucket.bucket-a.bucket
    }
  }

  vpc_config {
    subnet_ids         = [aws_subnet.subnet-a.id]
    security_group_ids = [aws_security_group.sg-app.id]
  }
}

resource "aws_lambda_event_source_mapping" "queue-a" {
  event_source_arn = aws_sqs_queue.queue-a.arn
  function_name    = aws_lambda_function.lambda-classifier.arn
}

# fed by an EventBridge schedule: an external trigger
resource "aws_lambda_function" "lambda-scheduled" {
  function_name = "lambda-scheduled"
  role          = aws_iam_role.role-scheduled.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  filename      = "lambda.zip"

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.table-a.name
    }
  }
}

resource "aws_cloudwatch_event_rule" "rule-a" {
  name                = "rule-a"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "rule-a" {
  rule = aws_cloudwatch_event_rule.rule-a.name
  arn  = aws_lambda_function.lambda-scheduled.arn
}

# no trigger declared, sends to queue-b
resource "aws_lambda_function" "lambda-orphan" {
  function_name = "lambda-orphan"
  role          = aws_iam_role.role-orphan.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  filename      = "lambda.zip"

  environment {
    variables = {
      QUEUE_URL = aws_sqs_queue.queue-b.url
    }
  }
}

# no trigger declared, nothing it sends to: still a flow of its own
resource "aws_lambda_function" "lambda-idle" {
  function_name = "lambda-idle"
  role          = aws_iam_role.role-idle.arn
  runtime       = "python3.12"
  handler       = "index.handler"
  filename      = "lambda.zip"
}

# ------------------------------------------------------------------ ECS

resource "aws_ecs_cluster" "cluster-a" {
  name = "cluster-a"
}

resource "aws_iam_role" "role-task" {
  name = "role-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "role-task" {
  name = "role-task-read"
  role = aws_iam_role.role-task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:Query"]
      Resource = aws_dynamodb_table.table-a.arn
    }]
  })
}

resource "aws_ecs_task_definition" "task-def-a" {
  family                   = "task-def-a"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = 256
  memory                   = 512
  task_role_arn            = aws_iam_role.role-task.arn
  container_definitions = jsonencode([
    {
      name  = "app"
      image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/app-a:1.0.0"
      environment = [
        { name = "TABLE_NAME", value = aws_dynamodb_table.table-a.name },
      ]
    },
    {
      name  = "sidecar"
      image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/sidecar-a:1.0.0"
    },
  ])
}

# no load balancer, no trigger: a flow of its own
resource "aws_ecs_service" "ecs-service-a" {
  name            = "ecs-service-a"
  cluster         = aws_ecs_cluster.cluster-a.id
  task_definition = aws_ecs_task_definition.task-def-a.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [aws_subnet.subnet-b.id]
    security_groups = [aws_security_group.sg-app.id]
  }
}

# ------------------------------------------------------------------ EC2 / ASG

resource "aws_iam_role" "role-ec2" {
  name = "role-ec2"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "role-ec2" {
  name = "role-ec2-access"
  role = aws_iam_role.role-ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.bucket-a.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.queue-a.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "profile-ec2" {
  name = "profile-ec2"
  role = aws_iam_role.role-ec2.name
}

# no trigger: a flow of its own (reads bucket-a through its instance profile)
resource "aws_instance" "instance-a" {
  ami                    = "ami-0123456789abcdef0"
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.subnet-a.id
  iam_instance_profile   = aws_iam_instance_profile.profile-ec2.name
  vpc_security_group_ids = [aws_security_group.sg-app.id]
}

resource "aws_launch_template" "lt-a" {
  name          = "lt-a"
  image_id      = "ami-0123456789abcdef0"
  instance_type = "t3.micro"

  iam_instance_profile {
    name = aws_iam_instance_profile.profile-ec2.name
  }
}

resource "aws_lb" "lb-a" {
  name               = "lb-a"
  load_balancer_type = "application"
  subnets            = [aws_subnet.subnet-a.id, aws_subnet.subnet-b.id]
  security_groups    = [aws_security_group.sg-lb.id]
}

resource "aws_lb_target_group" "tg-a" {
  name     = "tg-a"
  port     = 8080
  protocol = "HTTP"
  vpc_id   = aws_vpc.vpc-a.id
}

resource "aws_lb_listener" "lb-a" {
  load_balancer_arn = aws_lb.lb-a.arn
  port              = 443
  protocol          = "HTTPS"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.tg-a.arn
  }
}

# behind lb-a / tg-a, writes to queue-a (which feeds lambda-classifier)
resource "aws_autoscaling_group" "asg-a" {
  name                = "asg-a"
  min_size            = 1
  max_size            = 2
  vpc_zone_identifier = [aws_subnet.subnet-a.id]
  target_group_arns   = [aws_lb_target_group.tg-a.arn]

  launch_template {
    id = aws_launch_template.lt-a.id
  }
}

# a load balancer with no target: routing only (the Network group)
resource "aws_lb" "lb-c" {
  name = "lb-c"
}

resource "aws_lb_target_group" "tg-c" {
  name     = "tg-c"
  port     = 80
  protocol = "HTTP"
}

resource "aws_lb_listener" "lb-c" {
  load_balancer_arn = aws_lb.lb-c.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.tg-c.arn
  }
}
