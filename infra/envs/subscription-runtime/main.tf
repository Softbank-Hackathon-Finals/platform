terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
  backend "s3" {}
}

provider "aws" {
  region = "ap-northeast-2"
  default_tags { tags = { project = "bravo-platform", component = "platform" } }
}

variable "oidc_provider_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:oidc-provider/token.actions.githubusercontent.com$", var.oidc_provider_arn))
    error_message = "Use the exact GitHub OIDC provider."
  }
}
variable "production_subject" {
  type = string
  validation {
    condition     = var.production_subject == "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/develop"
    error_message = "Use the immutable team repository subject on develop."
  }
}
variable "validation_subject" {
  type = string
  validation {
    condition     = var.validation_subject == "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/feature/T15-subscription-integration"
    error_message = "Only the explicit integration branch can rehearse before merge."
  }
}
variable "validation_expires_at" {
  type = string
  validation {
    condition     = can(formatdate("YYYY-MM-DD", var.validation_expires_at))
    error_message = "A fixed RFC3339 expiry is required."
  }
}
variable "instance_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:ec2:ap-northeast-2:[0-9]{12}:instance/i-[a-f0-9]{17}$", var.instance_arn))
    error_message = "Select one designated Seoul VM."
  }
}
variable "subscription_secret_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:secretsmanager:ap-northeast-2:[0-9]{12}:secret:bravo-platform/agent-subscription/[A-Za-z0-9_-]+$", var.subscription_secret_arn))
    error_message = "Use one exact team subscription Secret ARN, without wildcards."
  }
}

locals {
  broker_name = "bravo-subscription-oidc-broker"
  github_trust = [
    { Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.oidc_provider_arn },
      Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub" = var.production_subject } } },
    { Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.oidc_provider_arn },
      Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub" = var.validation_subject },
        DateLessThan = { "aws:CurrentTime" = var.validation_expires_at }
    } }
  ]
}

resource "aws_iam_role" "controller" {
  name               = "bravo-subscription-controller"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = local.github_trust })
}

resource "aws_iam_role" "broker" {
  name = local.broker_name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [
    for grant in local.github_trust : merge(grant, { Condition = merge(grant.Condition, {
      StringEquals = merge(grant.Condition.StringEquals, { "aws:SourceVpce" = aws_vpc_endpoint.broker_sts.id })
      IpAddress    = { "aws:VpcSourceIp" = "${data.aws_instance.broker.private_ip}/32" }
    }) })
  ] })
  depends_on = [aws_ssm_association.broker_sts]
}

resource "aws_iam_role_policy" "broker" {
  name = "own-subscription-only"
  role = aws_iam_role.broker.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow",
    Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:UpdateSecretVersionStage"],
  Resource = var.subscription_secret_arn }] })
}

resource "aws_ssm_document" "service" {
  name          = "bravo-subscription-service"
  document_type = "Command"
  content = jsonencode({ schemaVersion = "2.2", description = "Start/stop the installed bounded broker; no command input.",
    parameters = {
      operation      = { type = "String", allowedValues = ["start", "stop", "verify"], interpolationType = "ENV_VAR" }
      runId          = { type = "String", default = "", allowedPattern = "^(subscription-[a-f0-9]{32})?$", interpolationType = "ENV_VAR" }
      proposalDigest = { type = "String", default = "", allowedPattern = "^([a-f0-9]{64})?$", interpolationType = "ENV_VAR" }
    },
    mainSteps = [{ action = "aws:runShellScript", name = "service", inputs = { timeoutSeconds = "360", runCommand = [
      "set -eu",
      <<-SH
      case "$SSM_operation" in
        start) systemctl start bravo-subscription.service; echo '{"service_command_completed":true}' ;;
        stop) systemctl stop bravo-subscription.service; echo '{"service_command_completed":true}' ;;
        verify)
          test -n "$SSM_runId"
          test -n "$SSM_proposalDigest"
          export KUBECONFIG=/etc/rancher/rke2/rke2.yaml
          export PYTHONDONTWRITEBYTECODE=1
          /opt/bravo-subscription/worker/.venv/bin/python -m bravo_agent.verification \
            --bundle "/var/lib/bravo-t11-subscription/runs/$SSM_runId/bundle.json" \
            --manifest /opt/bravo-subscription/worker/examples/snapshot-manifest.json \
            --snapshot /opt/bravo-subscription/worker/examples/source \
            --result "/var/lib/bravo-t11-subscription/runs/$SSM_runId/proposal.json" \
            --run-id "$SSM_runId" --proposal-digest "$SSM_proposalDigest" \
            --kubectl /var/lib/rancher/rke2/bin/kubectl \
            --image docker.io/library/python@sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b ;;
        *) exit 2 ;;
      esac
      SH
  ] } }] })
}

resource "aws_ssm_document" "tunnel" {
  name          = "bravo-subscription-tunnel"
  document_type = "Session"
  content = jsonencode({ schemaVersion = "1.0", description = "TLS tunnel to the fixed loopback broker port only.", sessionType = "Port",
    parameters = { localPortNumber = { type = "String", default = "18762", allowedPattern = "18762" } },
    properties = { portNumber = "18761", localPortNumber = "{{ localPortNumber }}", type = "LocalPortForwarding" }
  })
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "controller" {
  name = "fixed-broker-service-and-tunnel"
  role = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ssm:SendCommand", Resource = [var.instance_arn, aws_ssm_document.service.arn] },
    { Effect = "Allow", Action = "ssm:GetCommandInvocation", Resource = "*" },
    { Effect = "Allow", Action = "ssm:StartSession", Resource = [var.instance_arn, aws_ssm_document.tunnel.arn],
    Condition = { BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" } } },
    { Effect = "Allow", Action = "ssm:TerminateSession", Resource = "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:session/BravoSubscription-*" }
  ] })
}

output "controller_role_arn" { value = aws_iam_role.controller.arn }
output "broker_role_arn" { value = aws_iam_role.broker.arn }
