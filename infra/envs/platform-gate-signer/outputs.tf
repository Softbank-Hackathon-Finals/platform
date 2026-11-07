output "role_arn" {
  description = "AWS_ROLE_ARN of the workflow host (chart value gateSigner.roleArn)."
  value       = aws_iam_role.signer.arn
}

output "keys" {
  description = "The `keys` map of /workflow/gate-signer.json: one key ARN per purpose, never an alias."
  value       = { for p, k in aws_kms_key.gate : p => k.arn }
}

output "endpoint_ips" {
  description = "Private IPs of the STS/KMS endpoint ENIs: the --aws-endpoints input of workflow-host/network.py."
  value       = sort(distinct([for n in data.aws_network_interface.endpoint : n.private_ip]))
}
