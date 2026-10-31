variable "env" {
  type = string
}

variable "cidr" {
  type = string
}

resource "aws_vpc" "this" {
  cidr_block = var.cidr
  tags       = { Name = "repo-a-${var.env}" }
}

resource "aws_security_group" "app" {
  name   = "repo-a-${var.env}-app"
  vpc_id = aws_vpc.this.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["192.0.2.0/24"]
  }
}

output "security_group_id" {
  value = aws_security_group.app.id
}
