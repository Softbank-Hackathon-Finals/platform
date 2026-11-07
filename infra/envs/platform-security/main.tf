# T33 GuardDuty read for the platform findings puller (CronJob bravo-security/bravo-findings-pull) through the
# platform cluster's web identity. Nothing here holds a secret.
# The OIDC provider for the platform issuer belongs to infra/envs/platform-gate-signer (an account has one provider
# per issuer URL), so this stack only reads it: apply that stack first.
locals {
  issuer = trimprefix(var.issuer_url, "https://")
}

data "aws_iam_openid_connect_provider" "platform" {
  url = var.issuer_url
}

resource "aws_iam_role" "pull" {
  name                 = var.name
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.platform.arn },
    Condition = { StringEquals = {
      "${local.issuer}:sub" = var.service_account
      "${local.issuer}:aud" = "sts.amazonaws.com"
    } }
  }] })
}

# Read only: the collector never creates detectors or sample findings, and never archives or updates them.
resource "aws_iam_role_policy" "guardduty_read" {
  name = "guardduty-read"
  role = aws_iam_role.pull.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      # ListDetectors has no resource type.
      Sid      = "ListDetectorsInOneRegion", Effect = "Allow", Action = ["guardduty:ListDetectors"],
      Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = var.region } }
    },
    {
      Sid      = "ReadFindingsOfThisAccountAndRegion", Effect = "Allow",
      Action   = ["guardduty:GetDetector", "guardduty:ListFindings", "guardduty:GetFindings"],
      Resource = "arn:aws:guardduty:${var.region}:${var.account_id}:detector/*"
    }
  ] })
}
