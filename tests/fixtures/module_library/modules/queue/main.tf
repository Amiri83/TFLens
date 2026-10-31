variable "name" {
  type        = string
  description = "queue name"
}

variable "visibility_timeout" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}

module "naming" {
  source = "../naming"
  name   = var.name
}

resource "aws_sqs_queue" "dlq" {
  name = "${module.naming.full_name}-dlq"
}

resource "aws_sqs_queue" "this" {
  name                       = module.naming.full_name
  visibility_timeout_seconds = var.visibility_timeout
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
  tags = var.tags
}

output "queue_url" {
  description = "URL to send messages to"
  value       = aws_sqs_queue.this.url
}

output "queue_arn" {
  value = aws_sqs_queue.this.arn
}
