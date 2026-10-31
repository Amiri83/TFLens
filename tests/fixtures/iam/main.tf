resource "aws_s3_bucket" "reports" {
  bucket = "example-reports"
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::123456789012:root"]
    }
  }
}

resource "aws_iam_role" "app" {
  name                = "example-app"
  assume_role_policy  = data.aws_iam_policy_document.trust.json
  managed_policy_arns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]

  inline_policy {
    name = "logs"
    policy = jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "*"
      }]
    })
  }
}

data "aws_iam_policy_document" "reports" {
  statement {
    sid       = "Write"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.reports.arn}/*"]
  }

  statement {
    sid       = "NoDelete"
    effect    = "Deny"
    actions   = ["s3:DeleteObject"]
    resources = ["${aws_s3_bucket.reports.arn}/*"]
  }
}

resource "aws_iam_policy" "reports" {
  name   = "example-reports-write"
  policy = data.aws_iam_policy_document.reports.json
}

resource "aws_iam_policy_attachment" "reports" {
  name       = "reports"
  roles      = [aws_iam_role.app.name]
  policy_arn = aws_iam_policy.reports.arn
}

resource "aws_iam_user" "ci" {
  name = "example-ci"
}

resource "aws_iam_user_policy" "ci" {
  name = "ci-read"
  user = aws_iam_user.ci.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "arn:aws:s3:::example-reports/*"
    }]
  })
}

resource "aws_iam_group" "ops" {
  name = "example-ops"
}

resource "aws_iam_group_policy_attachment" "ops" {
  group      = aws_iam_group.ops.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_instance_profile" "app" {
  name = "example-app"
  role = aws_iam_role.app.name
}
