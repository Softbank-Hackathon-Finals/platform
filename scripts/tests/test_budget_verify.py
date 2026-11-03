import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('budget_verify', Path(__file__).parents[2] / 'infra/envs/cost-aws/verify.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class BudgetReadback(unittest.TestCase):
    def setUp(self):
        self.recipient = 'owner@example.com'
        self.threshold_type = None  # AWS omits the default percentage type.
        self.tag_status = 'Active'

    def api(self, service, operation, **params):
        if service == 'sts':
            return {'Account': '123'}
        if operation == 'describe-budget':
            return {'Budget': {'TimeUnit': 'MONTHLY', 'BudgetType': 'COST',
                'BudgetLimit': {'Unit': 'USD', 'Amount': '372.32'},
                'CostTypes': {**{key: True for key in ['IncludeCredit', 'IncludeRefund', 'IncludeTax',
                    'IncludeSupport', 'IncludeDiscount', 'IncludeOtherSubscription', 'IncludeRecurring',
                    'IncludeSubscription', 'IncludeUpfront']}, 'UseBlended': False, 'UseAmortized': False}}}
        if operation == 'describe-notifications-for-budget':
            return {'Notifications': [{'NotificationType': kind, 'Threshold': threshold,
                'ComparisonOperator': 'GREATER_THAN',
                **({'ThresholdType': self.threshold_type} if self.threshold_type else {})}
                for kind, threshold in [('ACTUAL', 80), ('ACTUAL', 100), ('FORECASTED', 100)]]}
        if operation == 'describe-subscribers-for-notification':
            return {'Subscribers': [{'SubscriptionType': 'EMAIL', 'Address': self.recipient}]}
        if operation == 'list-cost-allocation-tags':
            return {'CostAllocationTags': [{'TagKey': 'component', 'Status': self.tag_status}]}
        if operation == 'list-email-contacts':
            return {'emailContacts': [{'address': 'owner@example.com', 'status': 'inactive'}]}
        raise AssertionError(operation)

    def run_verify(self):
        config = {'account_id': '123', 'name': 'budget', 'limit_usd': '372.32', 'monthly_budget_krw': 500000,
                  'fx_date': '2026-11-02', 'scope': 'account'}
        with patch.object(verify.subprocess, 'check_output', return_value=json.dumps(config)), \
             patch.object(verify, 'aws', side_effect=self.api):
            return verify.verify('owner@example.com')

    def test_configuration_does_not_claim_delivery(self):
        result = self.run_verify()
        self.assertEqual(len(result['checks_passed']), 11)
        self.assertEqual(result['recipient_status'], 'inactive')
        self.assertFalse(result['notification_delivery_tested'])

    def test_wrong_subscriber_or_threshold_fails(self):
        self.recipient = 'someone@example.com'
        with self.assertRaisesRegex(RuntimeError, 'owner recipient'):
            self.run_verify()
        self.recipient = 'owner@example.com'
        self.threshold_type = 'ABSOLUTE_VALUE'
        with self.assertRaisesRegex(RuntimeError, 'comparison'):
            self.run_verify()

    def test_inactive_allocation_fails(self):
        self.tag_status = 'Inactive'
        with self.assertRaisesRegex(RuntimeError, 'allocation active'):
            self.run_verify()
