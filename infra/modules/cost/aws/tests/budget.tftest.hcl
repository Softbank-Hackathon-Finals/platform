mock_provider "aws" {}
variables {
  account_id         = "123456789012"
  name               = "test-monthly-guard"
  monthly_budget_krw = 500000
  krw_per_usd        = 1342.902996
  alert_emails       = ["owner@example.com"]
}
run "funding_and_alerts" {
  command = plan
  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "372.32" && aws_budgets_budget.monthly.limit_unit == "USD" && aws_budgets_budget.monthly.time_unit == "MONTHLY"
    error_message = "Funding must round DOWN to cents using the pinned exchange rate."
  }
  assert {
    condition     = toset([for n in aws_budgets_budget.monthly.notification : "${n.notification_type}:${n.threshold}"]) == toset(["ACTUAL:80", "ACTUAL:100", "FORECASTED:100"]) && alltrue([for n in aws_budgets_budget.monthly.notification : n.comparison_operator == "GREATER_THAN" && n.threshold_type == "PERCENTAGE" && n.subscriber_email_addresses == toset(["owner@example.com"])])
    error_message = "Owner must receive exactly actual 80/100 and forecast 100 percent notifications."
  }
  assert {
    condition     = length(aws_budgets_budget.monthly.cost_filter) == 0 && length(aws_budgets_budget.monthly.filter_expression) == 0 && aws_budgets_budget.monthly.cost_types[0].use_blended == false
    error_message = "The guard covers untagged account costs, on an unblended basis."
  }
}
run "reject_negative_budget" {
  command = plan
  variables { monthly_budget_krw = -1 }
  expect_failures = [var.monthly_budget_krw]
}
run "reject_zero_exchange_rate" {
  command = plan
  variables { krw_per_usd = 0 }
  expect_failures = [var.krw_per_usd]
}
run "reject_no_recipients" {
  command = plan
  variables { alert_emails = [] }
  expect_failures = [var.alert_emails]
}
run "reject_bad_email" {
  command = plan
  variables { alert_emails = ["invalid"] }
  expect_failures = [var.alert_emails]
}
run "reject_bad_account" {
  command = plan
  variables { account_id = "1234" }
  expect_failures = [var.account_id]
}
