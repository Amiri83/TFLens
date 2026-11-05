variable "name" {
  type = string
}

resource "aws_sqs_queue" "this" {
  name = var.name
}

output "queue_name" {
  value = aws_sqs_queue.this.name
}

output "queue_arn" {
  value = aws_sqs_queue.this.arn
}
