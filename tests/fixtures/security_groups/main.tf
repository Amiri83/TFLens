resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"
}

resource "aws_security_group" "lb" {
  name   = "example-lb"
  vpc_id = aws_vpc.main.id
}

resource "aws_security_group" "app" {
  name   = "example-app"
  vpc_id = aws_vpc.main.id

  ingress {
    description     = "from load balancer"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.lb.id]
  }

  ingress {
    description = "cluster gossip"
    from_port   = 7946
    to_port     = 7946
    protocol    = "tcp"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "app_ssh" {
  type              = "ingress"
  security_group_id = aws_security_group.app.id
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = ["10.20.0.0/16"]
}

resource "aws_security_group_rule" "app_vpc" {
  type              = "ingress"
  security_group_id = aws_security_group.app.id
  from_port         = 9090
  to_port           = 9100
  protocol          = "tcp"
  cidr_blocks       = [aws_vpc.main.cidr_block]
}

resource "aws_vpc_security_group_ingress_rule" "lb_https" {
  security_group_id = aws_security_group.lb.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "app" {
  ami                    = "ami-0123456789abcdef0"
  instance_type          = "t3.small"
  vpc_security_group_ids = [aws_security_group.app.id]
}
