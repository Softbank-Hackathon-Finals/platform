"""Credential ownership, fail-closed verification and source contract regressions."""

import importlib.util
import json
import subprocess
import unittest
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


class WorkloadTests(unittest.TestCase):
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
