mock_provider "aws" {
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::111122223333:oidc-provider/bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/bravo-gate-signer" }
  }
  mock_data "aws_region" {
    defaults = { region = "ap-northeast-2" }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = { network_interface_ids = ["eni-0123456789abcdef0"] }
  }
  mock_data "aws_network_interface" {
    defaults = { private_ip = "10.81.0.37" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:ap-northeast-2:111122223333:key/00000000-0000-4000-8000-000000000000", key_id = "00000000-0000-4000-8000-000000000000" }
  }
}
variables {
  account_id               = "111122223333"
  issuer_url               = "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform"
  vpc_id                   = "vpc-0123456789abcdef0"
  subnet_ids               = ["subnet-0123456789abcdef0"]
  client_security_group_id = "sg-0123456789abcdef0"
}

run "one_ed25519_sign_key_per_gate_purpose" {
  command = apply
  assert {
    condition     = length(aws_kms_key.gate) == 6 && alltrue([for k in aws_kms_key.gate : k.customer_master_key_spec == "ECC_NIST_EDWARDS25519" && k.key_usage == "SIGN_VERIFY"])
    error_message = "T19 accepts one public key per purpose per plan: six Ed25519 signing keys."
  }
  assert {
    condition     = alltrue([for a in aws_kms_alias.gate : startswith(a.name, "alias/bravo-gate-signer-")])
    error_message = "Aliases are names only; the signer config uses key ARNs."
  }
}

run "only_the_workflow_host_assumes_the_signer" {
  command = apply
  assert {
    condition     = jsondecode(aws_iam_role.signer.assume_role_policy).Statement[0].Condition.StringEquals["bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform:sub"] == "system:serviceaccount:platform-state:t10-workflow-host"
    error_message = "The trust names one exact ServiceAccount subject."
  }
  assert {
    condition     = jsondecode(aws_iam_role.signer.assume_role_policy).Statement[0].Condition.StringEquals["bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform:aud"] == "sts.amazonaws.com" && length(aws_iam_openid_connect_provider.platform.client_id_list) == 1 && contains(aws_iam_openid_connect_provider.platform.client_id_list, "sts.amazonaws.com")
    error_message = "Tokens must be minted for STS, not reused from another audience."
  }
}

run "administrators_never_sign_and_nobody_else_does" {
  command = apply
  assert {
    condition = alltrue([for k in aws_kms_key.gate : anytrue([
      for s in jsondecode(k.policy).Statement : s.Sid == "AdministerNotSign" && s.NotAction == "kms:Sign"
      ]) && anytrue([
      for s in jsondecode(k.policy).Statement : s.Effect == "Deny" && s.Action == "kms:Sign" && s.Condition.ArnNotEquals["aws:PrincipalArn"] == "arn:aws:iam::111122223333:role/bravo-gate-signer"
    ])])
    error_message = "Only the signer role signs; account administration excludes kms:Sign."
  }
  assert {
    condition = alltrue([for k in aws_kms_key.gate : anytrue([
      for s in jsondecode(k.policy).Statement : s.Sid == "SignerSignsRawEd25519" && s.Condition.StringEquals["kms:SigningAlgorithm"] == "ED25519_SHA_512" && s.Condition.StringEquals["kms:MessageType"] == "RAW"
    ])])
    error_message = "The signer signs pure Ed25519 over the raw T19 message only."
  }
}

run "issuer_must_be_exact_https" {
  command = plan
  variables { issuer_url = "http://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform/" }
  expect_failures = [var.issuer_url]
}

run "service_account_has_no_wildcard" {
  command = plan
  variables { service_account = "system:serviceaccount:platform-state:*" }
  expect_failures = [var.service_account]
}

run "sts_and_kms_are_private_endpoints_reachable_only_from_the_node" {
  command = apply
  assert {
    condition     = toset(keys(aws_vpc_endpoint.aws)) == toset(["sts", "kms"]) && alltrue([for e in aws_vpc_endpoint.aws : e.vpc_endpoint_type == "Interface" && e.private_dns_enabled])
    error_message = "The signer reaches STS and KMS through interface endpoints in the platform VPC."
  }
  assert {
    condition     = aws_vpc_endpoint.aws["kms"].service_name == "com.amazonaws.ap-northeast-2.kms" && aws_vpc_security_group_ingress_rule.endpoints.from_port == 443 && aws_vpc_security_group_ingress_rule.endpoints.referenced_security_group_id == "sg-0123456789abcdef0"
    error_message = "Only HTTPS from the platform node reaches the endpoints."
  }
  assert {
    condition     = length(output.endpoint_ips) == 1 && output.endpoint_ips[0] == "10.81.0.37"
    error_message = "The NetworkPolicy input is the endpoint ENIs' private IPs."
  }
}
