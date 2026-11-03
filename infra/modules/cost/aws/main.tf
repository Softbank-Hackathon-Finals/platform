variable "name" { type = string }
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Expected a 12-digit AWS account ID."
  }
}
variable "monthly_budget_krw" {
  type = number
  validation {
    condition     = var.monthly_budget_krw > 0
    error_message = "Monthly funding must be positive."
  }
}
variable "krw_per_usd" {
  type = number
  validation {
    condition     = var.krw_per_usd > 0
    error_message = "KRW per USD must be positive."
  }
}
variable "alert_emails" {
  type      = set(string)
  sensitive = true
  validation {
    condition = length(var.alert_emails) >= 1 && length(var.alert_emails) <= 10 && alltrue([
      for email in var.alert_emails : can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", email))
    ])
    error_message = "Provide 1-10 valid recipient email addresses."
  }
}
locals {
  usd_limit = format("%.2f", floor(var.monthly_budget_krw / var.krw_per_usd * 100) / 100)
  alerts = {
    actual_80    = { kind = "ACTUAL", threshold = 80 }
    actual_100   = { kind = "ACTUAL", threshold = 100 }
    forecast_100 = { kind = "FORECASTED", threshold = 100 }
  }
}
# Account-wide guard: untagged or not-yet-attributed spend still counts.
resource "aws_budgets_budget" "monthly" {
  account_id   = var.account_id
  name         = var.name
  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = local.usd_limit
  limit_unit   = "USD"
  cost_types {
    include_credit             = true
    include_discount           = true
    include_other_subscription = true
    include_recurring          = true
    include_refund             = true
    include_subscription       = true
    include_support            = true
    include_tax                = true
    include_upfront            = true
    use_amortized              = false
    use_blended                = false
  }
  dynamic "notification" {
    for_each = local.alerts
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.kind
      subscriber_email_addresses = var.alert_emails
    }
  }
  tags = { component = "platform" }
  lifecycle {
    precondition {
      condition     = tonumber(local.usd_limit) >= 0.01
      error_message = "Converted budget must be at least USD 0.01."
    }
  }
}
output "budget" {
  value = {
    name               = aws_budgets_budget.monthly.name
    account_id         = var.account_id
    monthly_budget_krw = var.monthly_budget_krw
    limit_usd          = local.usd_limit
    krw_per_usd        = var.krw_per_usd
    scope              = "account"
    metric             = "UnblendedCost"
    notifications      = local.alerts
  }
}
