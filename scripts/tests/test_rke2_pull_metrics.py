import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "infra/envs/private-registry/rke2_private_pull_probe.py"
SPEC = importlib.util.spec_from_file_location("rke2_private_pull_probe", SOURCE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
SERIES = 'kubelet_credential_provider_plugin_duration_count{plugin_name="ecr-credential-provider"}'


class ProviderMetricTests(unittest.TestCase):
    def read_count(self, text):
        with patch.object(probe, "call", return_value=text):
            return probe.metric_calls("test-node")

    def test_actual_kubelet_histogram_count_is_observed(self):
        text = '\n'.join([
            '# TYPE kubelet_credential_provider_plugin_duration histogram',
            'kubelet_credential_provider_plugin_duration_bucket{plugin_name="ecr-credential-provider",le="+Inf"} 3',
            'kubelet_credential_provider_plugin_duration_sum{plugin_name="ecr-credential-provider"} 0.17',
            SERIES + ' 3',
            'kubelet_credential_provider_plugin_duration_count{plugin_name="other-provider"} 8',
        ])
        self.assertEqual(self.read_count(text), 3)

    def test_zero_and_exponent_counts_are_real_observations(self):
        self.assertEqual(self.read_count(SERIES + ' 0'), 0)
        self.assertEqual(self.read_count(SERIES + ' 1e3'), 1000)

    def test_missing_or_wrong_series_is_unknown_not_zero(self):
        for text in ('', SERIES.replace('_duration_count', '_duration_seconds_count') + ' 3',
                     SERIES.replace('ecr-credential-provider', 'other-provider') + ' 4'):
            with self.subTest(text=text):
                self.assertIsNone(self.read_count(text))

    def test_malformed_or_duplicate_count_is_unknown(self):
        for value in ('NaN', '+Inf', '-1', '0.5', '3garbage'):
            with self.subTest(value=value):
                self.assertIsNone(self.read_count(SERIES + ' ' + value))
        self.assertIsNone(self.read_count(SERIES + ' 1\n' + SERIES + ' 2'))


if __name__ == '__main__':
    unittest.main()
