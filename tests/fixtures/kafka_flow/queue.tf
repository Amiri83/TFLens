resource "aws_sqs_queue" "work" {
  name = "queue-a"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue" "work_dlq" {
  name = "queue-a-dlq"
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name          = "alarm-a"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  period              = 300
  statistic           = "Maximum"
  dimensions = {
    QueueName = aws_sqs_queue.work_dlq.name
  }
}
