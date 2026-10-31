output "vpc_id" {
  description = "the shared VPC"
  value       = aws_vpc.main.id
}

output "ecr_repository_url" {
  description = "where images are pushed"
  value       = aws_ecr_repository.app.repository_url
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "certificate_arn" {
  value = aws_acm_certificate.main.arn
}
