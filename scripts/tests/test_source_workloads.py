"""Credential ownership, fail-closed verification and source contract regressions."""

import importlib.util
import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


render = module("workload_render", "infra/envs/source-workloads/render.py")
seed = module("workload_seed", "infra/envs/source-workloads/seed.py")
verify = module("workload_verify", "infra/envs/source-workloads/verify.py")
preflight = module("source_preflight", "infra/envs/source-aws/preflight.py")
app_probe = module("source_app_probe", "infra/envs/source-workloads/app_probe.py")


class WorkloadTests(unittest.TestCase):
    def probe_restore_case(self, restore_timeouts=0, accept_wrong_admin=False):
        state = {"blocked": True, "restore_attempts": 0}

        def request(method, path, body=None, admin=False, headers=None):
            if path == "/api/info":
                return 200, {}, b'{"env":"aws","write_blocked":true}'
            if path == "/admin/write-block":
                if not admin and not (accept_wrong_admin and method == "PUT"):
                    return 401, {}, b'{}'
                if method == "PUT":
                    if admin and body["blocked"]:
                        state["restore_attempts"] += 1
                        if state["restore_attempts"] <= restore_timeouts:
                            raise TimeoutError("synthetic failure before block applied")
                    state["blocked"] = body["blocked"]
                return 200, {}, json.dumps({"write_blocked": state["blocked"]}).encode()
            if method == "POST":
                if state["blocked"]:
                    return 503, {"retry-after": "5"}, b'{}'
                raise TimeoutError("synthetic business write failure")
            return 200, {}, b'{}'

        output = io.StringIO()
        with patch.object(app_probe, "request", side_effect=request), redirect_stdout(output):
            self.assertEqual(app_probe.run(), 1)
        return state, json.loads(output.getvalue())

    def test_wrong_admin_unexpected_success_cannot_open_writes(self):
        state, result = self.probe_restore_case(accept_wrong_admin=True)
        self.assertTrue(state["blocked"])
        self.assertFalse(result["checks"]["wrong_admin_denied"])

    def test_transient_block_restore_failure_retries_and_reads_final_state(self):
        state, result = self.probe_restore_case(restore_timeouts=1)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["restore_attempts"], 2)
        self.assertTrue(result["checks"]["final_write_blocked"])
        self.assertFalse(result["manual_write_block_required"])

    def test_persistent_restore_failure_is_bounded_and_requires_manual_block(self):
        state, result = self.probe_restore_case(restore_timeouts=10)
        self.assertFalse(state["blocked"])
        self.assertEqual(state["restore_attempts"], 3)
        self.assertFalse(result["checks"]["final_write_blocked"])
        self.assertTrue(result["manual_write_block_required"])
        self.assertFalse(result["passed"])

    def test_app_probe_restores_write_block_after_ambiguous_enable_timeout(self):
        calls = []

        def request(method, path, body=None, admin=False, headers=None):
            calls.append((method, path, body))
            if path == "/api/info":
                return 200, {}, b'{"env":"aws","write_blocked":true}'
            if path == "/admin/write-block":
                if not admin:
                    return 401, {}, b'{}'
                if body == {"blocked": False}:
                    raise TimeoutError("ambiguous request outcome")
                return 200, {}, b'{"write_blocked":true}'
            if method == "POST":
                return 503, {"retry-after": "5"}, b'{}'
            return 200, {}, b'{}'

        output = io.StringIO()
        with patch.object(app_probe, "request", side_effect=request), redirect_stdout(output):
            self.assertEqual(app_probe.run(), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertTrue(result["checks"]["restore_write_block"])
        self.assertTrue(result["checks"]["final_write_blocked"])
        self.assertIn(("PUT", "/admin/write-block", {"blocked": True}), calls)

    def test_app_probe_refuses_to_open_writes_when_initial_state_is_not_blocked(self):
        output = io.StringIO()
        with patch.object(app_probe, "request", return_value=(200, {}, b'{"env":"aws","write_blocked":false}')) as request, redirect_stdout(output):
            self.assertEqual(app_probe.run(), 1)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(json.loads(output.getvalue())["passed"])

    def settings(self):
        return {"region": "ap-northeast-2", "app_role_arn": "arn:aws:iam::123456789012:role/test-app",
                "eso_role_arn": "arn:aws:iam::123456789012:role/test-eso",
                "db_secret_id": "example/db", "control_secret_id": "example/control",
                "s3_bucket": "example-bucket",
                "app_image": "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:" + "a" * 64}

    def test_rendered_objects_satisfy_existing_18_source_contracts(self):
        app, db, pool = render.application(self.settings()), render.database(), render.pooler()
        bundle = {"schema_version": 1, "eks_request": {"logging": {"clusterLogging": [{"enabled": True, "types": ["audit"]}]}},
                  "cnpg_cluster": db[-1], "pooler_service": pool[-1],
                  "pgbouncer_ini": (ROOT / "infra/envs/source-workloads/pgbouncer.ini").read_text(),
                  "app_deployment": app[-2], "secret_store": app[2], "external_secret": app[3]}
        result = preflight.check(bundle)
        self.assertEqual(result["configuration_result"], "PASS", result["checks"])
        self.assertEqual(len(result["checks"]), 18)
        self.assertFalse(result["runtime_verified"])

    def test_rejects_mutable_image_and_accidental_secret_in_metadata(self):
        for field, value in [("app_image", "ghcr.io/softbank-hackathon-finals/demo-chat:latest"),
                             ("password", "not-an-allowed-input")]:
            settings = self.settings()
            settings[field] = value
            with self.assertRaises(ValueError):
                render.application(settings)

    def test_image_reference_cannot_change_repository_or_use_invalid_tag(self):
        for image in ["ghcr.io/softbank-hackathon-finals/demo-chat-unrelated",
                      "ghcr.io/softbank-hackathon-finals/demo-chat/unrelated",
                      "ghcr.io/other/demo-chat", "ghcr.io/softbank-hackathon-finals/demo-chat:",
                      "ghcr.io/softbank-hackathon-finals/demo-chat:-invalid"]:
            with self.subTest(image=image):
                settings = self.settings()
                settings["app_image"] = image + "@sha256:" + "a" * 64
                with self.assertRaises(ValueError):
                    render.application(settings)
        settings = self.settings()
        settings["app_image"] = "ghcr.io/softbank-hackathon-finals/demo-chat:sha-main@sha256:" + "a" * 64
        render.application(settings)

    def test_replication_role_has_database_rule_but_no_physical_replication_rule(self):
        hba = render.database()[-1]["spec"]["postgresql"]["pg_hba"]
        self.assertIn("hostssl chat chat_replicator all scram-sha-256", hba)
        self.assertFalse(any(line.split()[1] == "replication" and "chat_replicator" in line for line in hba))

    def test_private_ecr_is_bound_to_account_region_and_exact_repository(self):
        settings = self.settings()
        registry = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"
        settings["app_image"] = registry + "/bravo/demo-chat@sha256:" + "a" * 64
        with self.assertRaises(ValueError):
            render.application(settings)
        settings["ecr_repository_url"] = registry + "/bravo/demo-chat"
        render.application(settings)
        invalid = [
            registry + "/bravo/demo-chat-unrelated",
            registry + "/bravo/demo-chat/extra",
            registry.replace("ap-northeast-2", "us-west-2") + "/bravo/demo-chat",
            registry.replace("123456789012", "1111" + "2222" + "3333") + "/bravo/demo-chat",
            registry + ".example.com/bravo/demo-chat",
        ]
        for image in invalid:
            with self.subTest(image=image):
                settings["app_image"] = image + "@sha256:" + "a" * 64
                with self.assertRaises(ValueError):
                    render.application(settings)
                settings["ecr_repository_url"] = image
                with self.assertRaises(ValueError):
                    render.application(settings)
                settings["ecr_repository_url"] = registry + "/bravo/demo-chat"

    def test_database_storage_retained_and_app_starts_write_blocked(self):
        self.assertEqual(render.database()[1]["reclaimPolicy"], "Retain")
        app = render.application(self.settings())[-2]["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in app["env"]}
        self.assertEqual(env["PGSSLMODE"], "verify-full")
        self.assertEqual(env["WRITE_BLOCKED"], "true")
        self.assertNotIn("DB_PASSWORD", env)

    def test_scram_rejects_wrong_password_and_unbounded_iterations(self):
        verifier = seed.scram("fixture-value", b"fixed-salt-for-test")
        self.assertTrue(seed.password_matches("fixture-value", verifier))
        self.assertFalse(seed.password_matches("different", verifier))
        self.assertFalse(seed.password_matches("fixture-value", verifier.replace("4096:", "999999999:")))

    def test_existing_foreign_secret_is_never_adopted(self):
        runtime = seed.Runtime("unused")
        with patch.object(runtime, "call", return_value=json.dumps({"metadata": {"labels": {}}})), self.assertRaisesRegex(RuntimeError, "another owner"):
            runtime.secret("source-app-db", lambda: self.fail("must not generate a replacement"))

    def test_failed_provider_does_not_echo_input_or_stderr(self):
        failure = subprocess.CompletedProcess([], 1, stdout="private-value", stderr="private-value")
        with patch.object(seed.subprocess, "run", return_value=failure), self.assertRaises(RuntimeError) as caught:
            seed.Runtime.call(["fake"], "private-value")
        self.assertNotIn("private-value", str(caught.exception))

    def test_publish_reuses_equal_values_and_refuses_overwrite(self):
        runtime = seed.Runtime("unused")
        desired = {"username": "fixture-user", "password": "fixture-value"}

        class Existing:
            def __init__(self, value):
                self.value = value

            def describe_secret(self, **_):
                return {"VersionIdsToStages": {"version": ["AWSCURRENT"]}}

            def get_secret_value(self, **_):
                return {"SecretString": json.dumps(self.value)}

            def put_secret_value(self, **_):
                raise AssertionError("existing data must not be written")

        with patch.object(runtime, "secret", return_value=desired):
            runtime.publish(self.settings(), Existing(desired))
            with self.assertRaisesRegex(RuntimeError, "overwrite refused"):
                runtime.publish(self.settings(), Existing({"password": "different"}))

    def test_ddl_evidence_requires_structured_pgaudit_record(self):
        record = {"logger": "pgaudit", "record": {"database_name": "chat",
                  "audit": {"class": "DDL", "statement": "CREATE SCHEMA t6_audit_canary"}}}
        self.assertTrue(verify.ddl_canary_observed(json.dumps(record)))
        record["logger"] = "postgres"
        self.assertFalse(verify.ddl_canary_observed(json.dumps(record)))
        self.assertFalse(verify.ddl_canary_observed("AUDIT: t6_audit_canary"))


if __name__ == "__main__":
    unittest.main()
