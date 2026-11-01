resource "aws_sqs_queue" "this" {
  name                       = var.name
  visibility_timeout_seconds = var.visibility_timeout
  tags                       = var.tags
}

resource "aws_sqs_queue" "dlq" {
  count = var.with_dlq ? 1 : 0
  name  = "${var.name}-dlq"
  tags  = var.tags
}
