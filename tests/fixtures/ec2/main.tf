variable "instance_type" {
  type    = string
  default = "t3.micro"
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "example-artifacts"
}

resource "aws_iam_role" "web" {
  name = "example-web"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "web_artifacts" {
  name = "read-artifacts"
  role = aws_iam_role.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = ["arn:aws:s3:::example-artifacts", "arn:aws:s3:::example-artifacts/*"]
    }]
  })
}

resource "aws_iam_instance_profile" "web" {
  name = "example-web"
  role = aws_iam_role.web.name
}

resource "aws_security_group" "web" {
  name        = "example-web"
  description = "web tier"

  ingress {
    description = "https"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "web" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.web.name
  vpc_security_group_ids = [aws_security_group.web.id]

  tags = {
    Name = "example-web"
  }
}

resource "aws_instance" "legacy" {
  ami           = "ami-0123456789abcdef0"
  instance_type = "t3.nano"
}
