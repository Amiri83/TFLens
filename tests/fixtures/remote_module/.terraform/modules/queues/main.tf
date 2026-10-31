variable "name" {
  type = string
}

variable "retention" {
  type    = number
  default = 345600
}

variable "kms_key" {
  type = string
}

module "dlq" {
  source = "./modules/dlq"
  name   = "${var.name}-dlq"
}

resource "aws_sqs_queue" "this" {
  name                      = var.name
  message_retention_seconds = var.retention
  kms_master_key_id         = var.kms_key
  redrive_policy = jsonencode({
    deadLetterTargetArn = module.dlq.arn
    maxReceiveCount     = 5
  })
}

output "queue_arn" {
  value = aws_sqs_queue.this.arn
}

output "queue_name" {
  value = aws_sqs_queue.this.name
}
