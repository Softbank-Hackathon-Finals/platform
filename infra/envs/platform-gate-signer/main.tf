# T19 platform signer (ADR-0038): one non-exportable Ed25519 KMS key per gate purpose, usable only by the
# workflow host's ServiceAccount through the platform cluster's web identity. Nothing here holds a secret.
locals {
  purposes = toset(["context", "run-source", "run-target", "readiness-binding", "readiness-snapshot", "gate-result"])
  issuer   = trimprefix(var.issuer_url, "https://")
  signer   = "arn:aws:iam::${var.account_id}:role/${var.name}"
  ed25519  = "ECC_NIST_EDWARDS25519" # KMS spec name, not a secret
}

resource "aws_iam_openid_connect_provider" "platform" {
  url            = var.issuer_url
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "signer" {
  name                 = var.name
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = aws_iam_openid_connect_provider.platform.arn },
    Condition = { StringEquals = {
      "${local.issuer}:sub" = var.service_account
      "${local.issuer}:aud" = "sts.amazonaws.com"
    } }
  }] })
}

resource "aws_kms_key" "gate" {
  for_each                 = local.purposes
  description              = "T19 gate envelope signer: ${each.key}"
  customer_master_key_spec = local.ed25519
  key_usage                = "SIGN_VERIFY"
  deletion_window_in_days  = 7
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    # Account IAM administers the key but never signs with it.
    { Sid = "AdministerNotSign", Effect = "Allow", Principal = { AWS = "arn:aws:iam::${var.account_id}:root" },
    NotAction = "kms:Sign", Resource = "*" },
    { Sid = "SignerSignsRawEd25519", Effect = "Allow", Principal = { AWS = local.signer }, Action = "kms:Sign",
    Resource = "*", Condition = { StringEquals = { "kms:SigningAlgorithm" = "ED25519_SHA_512", "kms:MessageType" = "RAW" } } },
    { Sid = "SignerReadsPublicKey", Effect = "Allow", Principal = { AWS = local.signer },
    Action = ["kms:GetPublicKey", "kms:DescribeKey"], Resource = "*" },
    { Sid = "NobodyElseSigns", Effect = "Deny", Principal = { AWS = "*" }, Action = "kms:Sign", Resource = "*",
    Condition = { ArnNotEquals = { "aws:PrincipalArn" = local.signer } } },
  ] })
  depends_on = [aws_iam_role.signer] # the key policy names the role
}

resource "aws_kms_alias" "gate" {
  for_each      = local.purposes
  name          = "alias/${var.name}-${each.key}"
  target_key_id = aws_kms_key.gate[each.key].key_id
}

# Private STS and KMS for the platform VPC: the workflow host's NetworkPolicy opens only these ENIs
# (infra/envs/workflow-host/network.py --aws-endpoints), not the public AWS ranges.
data "aws_region" "current" {}

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-endpoints"
  description = "STS and KMS interface endpoints for the T19 platform signer"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints" {
  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from the platform node (pods egress through it)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = var.client_security_group_id
}

resource "aws_vpc_endpoint" "aws" {
  for_each            = toset(["sts", "kms"])
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${data.aws_region.current.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = var.subnet_ids
  security_group_ids  = [aws_security_group.endpoints.id]
}

data "aws_network_interface" "endpoint" {
  # Static keys (service, subnet index): the ENI ids are known only after apply.
  for_each = { for p in setproduct(["sts", "kms"], range(length(var.subnet_ids))) : "${p[0]}-${p[1]}" => p }
  id       = sort(tolist(aws_vpc_endpoint.aws[each.value[0]].network_interface_ids))[each.value[1]]
}
