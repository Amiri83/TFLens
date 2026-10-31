# alb-a -> svc-a (ECS) -> reads table-a

resource "aws_lb" "public" {
  name               = "alb-a"
  load_balancer_type = "application"
  subnets            = ["subnet-0a", "subnet-0b"]
}

resource "aws_lb_target_group" "svc" {
  name        = "tg-a"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = "vpc-0a"
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.public.arn
  port              = 443
  protocol          = "HTTPS"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.svc.arn
  }
}

resource "aws_ecs_cluster" "main" {
  name = "cluster-a"
}

resource "aws_iam_role" "svc_task" {
  name = "svc-a-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "svc_task" {
  role = aws_iam_role.svc_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:Query"]
      Resource = aws_dynamodb_table.orders.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "svc" {
  name = "/ecs/svc-a"
}

resource "aws_ecs_task_definition" "svc" {
  family        = "svc-a"
  task_role_arn = aws_iam_role.svc_task.arn
  container_definitions = jsonencode([{
    name  = "app"
    image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/svc-a:1.0.0"
    environment = [
      { name = "TABLE_NAME", value = aws_dynamodb_table.orders.name },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group = aws_cloudwatch_log_group.svc.name
      }
    }
  }])
}

resource "aws_ecs_service" "svc" {
  name            = "svc-a"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.svc.arn
  desired_count   = 2

  load_balancer {
    target_group_arn = aws_lb_target_group.svc.arn
    container_name   = "app"
    container_port   = 8080
  }
}
