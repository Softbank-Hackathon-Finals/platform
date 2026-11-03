terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  backend "s3" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71.0"
    }
  }
}
variable "account_id" { type = string }
variable "alert_emails" {
  type      = set(string)
  sensitive = true
}
variable "monthly_budget_krw" { type = number }
variable "krw_per_usd" { type = number }
variable "fx_date" { type = string }
variable "fx_source" { type = string }
variable "activate_project_tag" {
  description = "Enable after AWS billing first lists the project key (may take 24h)."
  type        = bool
  default     = false
}
provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      project      = "bravo-platform"
      component    = "platform"
      owner        = "team-bravo"
      "managed-by" = "terraform"
    }
  }
}
module "cost" {
  source             = "../../modules/cost/aws"
  name               = "bravo-platform-monthly-guard"
  account_id         = var.account_id
  monthly_budget_krw = var.monthly_budget_krw
  krw_per_usd        = var.krw_per_usd
  alert_emails       = var.alert_emails
}
# This key is account-global. Keep attribution enabled when removing a budget.
resource "aws_ce_cost_allocation_tag" "component" {
  tag_key = "component"
  status  = "Active"
  lifecycle { prevent_destroy = true }
}
output "cost_controls" {
  value = merge(module.cost.budget, {
    fx_date        = var.fx_date
    fx_source      = var.fx_source
    allocation_tag = aws_ce_cost_allocation_tag.component.tag_key
  })
}

resource "aws_ce_cost_allocation_tag" "project" {
  count   = var.activate_project_tag ? 1 : 0
  tag_key = "project"
  status  = "Active"
  lifecycle { prevent_destroy = true }
}
