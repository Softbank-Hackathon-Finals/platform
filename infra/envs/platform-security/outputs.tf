output "role_arn" {
  description = "Put into the bravo-findings-aws ConfigMap (role_arn) outside Git."
  value       = aws_iam_role.pull.arn
}
