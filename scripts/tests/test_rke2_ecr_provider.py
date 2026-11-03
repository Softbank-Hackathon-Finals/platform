import hashlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

SOURCE = pathlib.Path(__file__).resolve().parents[2] / "infra/envs/private-registry/rke2_credential_provider.py"
SPEC = importlib.util.spec_from_file_location("rke2_ecr_provider", SOURCE)
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)
SCAN_SPEC = importlib.util.spec_from_file_location("provider_scan", SOURCE.with_name("scan_credential_provider.py"))
scan = importlib.util.module_from_spec(SCAN_SPEC)
SCAN_SPEC.loader.exec_module(scan)
BUILD_SPEC = importlib.util.spec_from_file_location("provider_build", SOURCE.with_name("build_credential_provider.py"))
build = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(build)


class ProviderTests(unittest.TestCase):
    def test_empty_scan_is_never_vulnerability_success(self):
        for report in ({}, {"Results": []}, {"Results": [{"Type": "gobinary"}]}):
            with self.assertRaises(ValueError):
                scan.summary(report, 0, "hash")
        report = {"Results": [{"Type": "gobinary", "Packages": [{}] * 54}]}
        self.assertTrue(scan.summary(report, 0, "hash")["gate_pass"])
        report["Results"][0]["Vulnerabilities"] = [{"Severity": "HIGH"}]
        self.assertFalse(scan.summary(report, 1, "hash")["gate_pass"])
        with self.assertRaises(ValueError):
            scan.summary(report, 0, "hash")

    def test_unreviewed_build_input_is_rejected(self):
        inputs = json.loads(SOURCE.with_name("provider-build-inputs.json").read_text())
        build.check_inputs(inputs)
        for key in ("source_commit", "source_archive_url", "binary_sha256", "go_linux_amd64_sha256"):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                build.check_inputs({**inputs, key: "unreviewed"})

    def test_replacement_restores_old_binary_on_readiness_failure_and_is_idempotent(self):
        old, new = b"known original", b"reviewed replacement"
        repository = "000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat"
        private_input = {"repository_url": repository, "digest": "sha256:" + "a" * 64}
        auth_response = json.dumps({"kind": "CredentialProviderResponse", "auth": {repository: {
            "username": "AWS", "password": "mock-short-lived-value"}},
            "cacheDuration": "6h0m0s", "cacheKeyType": "Registry"})
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = pathlib.Path(directory)
            paths = {"BINARY": root / "bin/provider", "CANDIDATE": root / "candidate",
                     "BINARY_BACKUP": root / "before", "MAIN_CONFIG": root / "main",
                     "CONFIG": root / "config", "DROPIN": root / "dropin"}
            paths["BINARY"].parent.mkdir()
            paths["BINARY"].write_bytes(old)
            paths["CANDIDATE"].write_bytes(new)
            paths["MAIN_CONFIG"].write_bytes(b"original main")
            for name in ("BINARY", "CANDIDATE"):
                paths[name].chmod(0o755)
            for name, path in paths.items():
                stack.enter_context(patch.object(provider, name, path))
            stack.enter_context(patch.object(provider, "BINARY_SHA", hashlib.sha256(new).hexdigest()))
            stack.enter_context(patch.object(provider, "LEGACY_SHA", hashlib.sha256(old).hexdigest()))
            stack.enter_context(patch.object(provider.os, "geteuid", return_value=0))
            stack.enter_context(patch.object(provider.os, "uname", return_value=Mock(machine="x86_64")))
            stack.enter_context(patch.object(provider.os, "environ", {}))
            stack.enter_context(patch.object(provider, "pods", return_value=[]))
            stack.enter_context(patch.object(provider, "ready_counts", return_value=(1, 1, 17, 17)))
            stack.enter_context(patch.object(provider, "kubelet_provider_flags", return_value=True))
            stack.enter_context(patch.object(provider.subprocess, "run", return_value=Mock(returncode=0, stdout=auth_response)))
            # Local tests do not require root; scope ownership mock to these files.
            real_stat = pathlib.Path.stat
            def root_stat(path, *args, **kwargs):
                if path == pathlib.Path("/root/.aws/credentials"):
                    raise FileNotFoundError(2, "missing")  # Test host has no shared AWS credentials.
                actual = real_stat(path, *args, **kwargs)
                return Mock(st_uid=0, st_mode=actual.st_mode)
            stack.enter_context(patch.object(pathlib.Path, "stat", root_stat))
            # The production primary-config backup location is independently patched.
            backup = root / "main.before"
            stack.enter_context(patch.object(provider, "PRIMARY_BACKUP", backup))
            with patch.object(provider.sys, "stdin", io.StringIO(json.dumps(private_input))), \
                 patch.object(provider, "wait_ready", side_effect=[provider.Failure("not_ready"), (1, 1, 17, 17)]), \
                 patch.object(provider, "run") as commands, self.assertRaises(provider.Failure):
                provider.main()
            self.assertEqual(paths["BINARY"].read_bytes(), old)
            self.assertFalse(paths["CONFIG"].exists())
            self.assertEqual(commands.call_count, 2)  # failed restart + rollback restart
            for expected_change in (True, False):
                with patch.object(provider.sys, "stdin", io.StringIO(json.dumps(private_input))), \
                     patch.object(provider, "wait_ready", return_value=(1, 1, 17, 17)), \
                     patch.object(provider, "run") as commands, redirect_stdout(io.StringIO()) as output:
                    provider.main()
                self.assertEqual(json.loads(output.getvalue())["rke2_service_restarted"], expected_change)
                self.assertEqual(commands.call_count, int(expected_change))
                self.assertEqual(paths["BINARY"].read_bytes(), new)
                self.assertEqual(paths["MAIN_CONFIG"].read_bytes(), b"original main")
                if expected_change:
                    paths["CANDIDATE"].unlink()  # Repeat must not need candidate/download/build.

    def test_unknown_or_symlink_binary_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "binary"
            path.write_bytes(b"foreign binary")
            path.chmod(0o755)
            with self.assertRaises(provider.Failure):
                provider.owned_binary(path, {provider.BINARY_SHA, provider.LEGACY_SHA})
            link = path.with_name("link")
            link.symlink_to(path)
            with self.assertRaises(provider.Failure):
                provider.owned_binary(link, {hashlib.sha256(path.read_bytes()).hexdigest()})

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
