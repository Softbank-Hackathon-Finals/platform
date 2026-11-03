"""Verify AWS budget configuration; recipient activation is reported separately."""
import argparse
import datetime as dt
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from cost_report import aws, pages


def verify(email):
    config = json.loads(subprocess.check_output(['terraform', 'output', '-json', 'cost_controls'], text=True))
    if aws('sts', 'get-caller-identity')['Account'] != config['account_id']:
        raise RuntimeError('Wrong AWS account')
    params = {'AccountId': config['account_id'], 'BudgetName': config['name']}
    budget = aws('budgets', 'describe-budget', **params)['Budget']
    checks = []
    def check(label, condition):
        if not condition:
            raise RuntimeError('Readback failed: '+label)
        checks.append(label)
    check('monthly USD funding', budget['TimeUnit'] == 'MONTHLY' and budget['BudgetType'] == 'COST'
          and budget['BudgetLimit']['Unit'] == 'USD'
          and Decimal(budget['BudgetLimit']['Amount']) == Decimal(config['limit_usd']))
    check('account-wide guard', not budget.get('CostFilters') and not budget.get('FilterExpression'))
    check('unblended net costs', not budget['CostTypes']['UseBlended'] and not budget['CostTypes']['UseAmortized']
          and all(budget['CostTypes'][key] for key in ['IncludeCredit', 'IncludeRefund', 'IncludeTax', 'IncludeSupport',
              'IncludeDiscount', 'IncludeOtherSubscription', 'IncludeRecurring', 'IncludeSubscription', 'IncludeUpfront']))
    notifications = aws('budgets', 'describe-notifications-for-budget', **params)['Notifications']
    check('three thresholds', len(notifications) == 3 and {(n['NotificationType'], n['Threshold']) for n in notifications}
          == {('ACTUAL', 80), ('ACTUAL', 100), ('FORECASTED', 100)})
    for n in notifications:
        check(f"{n['NotificationType']} {n['Threshold']} comparison", n['ComparisonOperator'] == 'GREATER_THAN'
              and n.get('ThresholdType', 'PERCENTAGE') == 'PERCENTAGE')
        subscribers = aws('budgets', 'describe-subscribers-for-notification', Notification=n, **params)['Subscribers']
        check(f"{n['NotificationType']} {n['Threshold']} owner recipient", len(subscribers) == 1
              and subscribers[0]['SubscriptionType'] == 'EMAIL' and subscribers[0]['Address'] == email)
    tags = {t['TagKey']: t['Status'] for batch in pages(aws, 'list-cost-allocation-tags', 'CostAllocationTags',
        TagKeys=['component', 'project']) for t in batch}
    check('component allocation active', tags.get('component') == 'Active')
    contacts = aws('notificationscontacts', 'list-email-contacts').get('emailContacts', [])
    status = next((c['status'] for c in contacts if c['address'] == email), 'not_found')
    return {'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'checks_passed': checks,
            'budget_name': config['name'], 'limit_usd': config['limit_usd'],
            'monthly_budget_krw': config['monthly_budget_krw'], 'fx_date': config['fx_date'],
            'scope': config['scope'], 'cost_allocation_tags': tags, 'recipient_status': status,
            'notification_delivery_tested': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--email', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.email)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(f"PASS: {len(result['checks_passed'])} AWS configuration checks; recipient={result['recipient_status']}; delivery untested")
