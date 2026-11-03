mock_provider "aws" {}
override_data {
  target = data.aws_subnet.private["subnet-0123456789abcdef0"]
  values = { vpc_id = "vpc-0123456789abcdef0", availability_zone = "ap-northeast-2a", map_public_ip_on_launch = false }
}
override_data {
  target = data.aws_subnet.private["subnet-0123456789abcdef1"]
  values = { vpc_id = "vpc-0123456789abcdef0", availability_zone = "ap-northeast-2c", map_public_ip_on_launch = false }
}
variables {
  account_id             = "111122223333"
  vpc_id                 = "vpc-0123456789abcdef0"
  private_subnet_ids     = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
  private_route_table_id = "rtb-0123456789abcdef0"
  node_release_version   = "1.35.9-20261031"
  vpc_cni_version        = "v1.23.2-eksbuild.8"
  operator_principal_arn = "arn:aws:iam::111122223333:role/operator"
}
run "private_encrypted_audited_cluster" {
  command = plan
  assert {
    condition     = !aws_eks_cluster.source.vpc_config[0].endpoint_public_access && aws_eks_cluster.source.vpc_config[0].endpoint_private_access
    error_message = "EKS must have a private-only endpoint."
  }
  assert {
    condition     = !aws_eks_cluster.source.access_config[0].bootstrap_cluster_creator_admin_permissions && aws_eks_cluster.source.access_config[0].authentication_mode == "API"
    error_message = "Admin must be an explicit individual access entry."
  }
  assert {
    condition     = contains(aws_eks_cluster.source.enabled_cluster_log_types, "audit") && aws_kms_key.cluster.enable_key_rotation && aws_launch_template.node.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "Audit, encrypted control plane, and pod IMDS boundary are required."
  }
  assert {
    condition     = !contains(keys(aws_iam_role_policy_attachment.node), "AmazonEKS_CNI_Policy")
    error_message = "CNI permissions must remain on its own workload role."
  }
}
run "session_arn_rejected" {
  command = plan
  variables { operator_principal_arn = "arn:aws:sts::111122223333:assumed-role/operator/session" }
  expect_failures = [var.operator_principal_arn]
}
run "foreign_operator_rejected" {
  command = plan
  variables { operator_principal_arn = "arn:aws:iam::999900001111:role/operator" }
  expect_failures = [aws_eks_cluster.source]
}
