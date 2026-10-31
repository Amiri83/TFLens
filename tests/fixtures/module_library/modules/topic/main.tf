variable "name" {
  type        = string
  description = "topic name"
}

resource "aws_sns_topic" "this" {
  name = var.name
}

output "topic_arn" {
  value = aws_sns_topic.this.arn
}
