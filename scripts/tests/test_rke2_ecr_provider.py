import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "infra/envs/private-registry/rke2_credential_provider.py"
SPEC = importlib.util.spec_from_file_location("rke2_ecr_provider", SOURCE)
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


class ProviderTests(unittest.TestCase):
    def test_exact_repository_only_without_stored_credentials(self):
        repository = "000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat"
        config = provider.provider_configuration(repository)
        item = config["providers"][0]
        self.assertEqual(item["matchImages"], [repository])
        self.assertEqual(item["env"], [{"name": "AWS_REGION", "value": "ap-northeast-2"}])
        for bad in (repository + "-other", repository.replace("ap-northeast-2", "us-east-1"), "*.amazonaws.com"):
            with self.subTest(repository=bad), self.assertRaises(provider.Failure):
                provider.provider_configuration(bad)

    def test_existing_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config"
            path.write_bytes(b"existing operator config")
            with self.assertRaises(provider.Failure):
                provider.ensure_owned(path, b"new config", 0o600, [])
            self.assertEqual(path.read_bytes(), b"existing operator config")

    def test_symlink_is_never_followed_for_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "operator"
            target.write_bytes(b"same")
            path = pathlib.Path(directory) / "config"
            path.symlink_to(target)
            with self.assertRaises(provider.Failure):
                provider.ensure_owned(path, b"same", 0o600, [])
            self.assertEqual(target.read_bytes(), b"same")

    def test_new_configuration_is_private_and_tracked_for_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config"
            created = []
            self.assertTrue(provider.ensure_owned(path, b"configuration", 0o600, created))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(created, [path])

    def test_failed_cli_output_is_not_exposed(self):
        result = Mock(returncode=1, stdout="private response", stderr="private endpoint")
        with patch.object(provider.subprocess, "run", return_value=result), \
             self.assertRaisesRegex(provider.Failure, "^operation_failed$"):
            provider.run(["unused"], "operation")

    def test_readiness_rejects_unready_or_empty_system_inventory(self):
        node = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
        pod = {"metadata": {"namespace": "kube-system"}, "status": {
            "phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}
        with patch.object(provider, "run", side_effect=["ok", json.dumps({"items": [node]})]), \
             patch.object(provider, "pods", return_value=[pod] * 17):
            self.assertEqual(provider.ready_counts(), (1, 1, 17, 17))
        broken = {"metadata": {"namespace": "kube-system"}, "status": {
            "phase": "Running", "conditions": [{"type": "Ready", "status": "False"}]}}
        for inventory in ([], [pod] * 16, [pod] * 16 + [broken]):
            with patch.object(provider, "run", side_effect=["ok", json.dumps({"items": [node]})]), \
                 patch.object(provider, "pods", return_value=inventory), self.assertRaises(provider.Failure):
                provider.ready_counts()


if __name__ == "__main__":
    unittest.main()
