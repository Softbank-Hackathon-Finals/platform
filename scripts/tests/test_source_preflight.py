import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "infra/envs/source-aws"
spec = importlib.util.spec_from_file_location("source_preflight", SOURCE / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class SourceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.configuration = json.loads((SOURCE / "configuration.example.json").read_text())

    def blocked(self, identifier):
        r = preflight.check(self.configuration)
        self.assertEqual(r["configuration_result"], "BLOCK")
        self.assertIn(identifier, [c["id"] for c in r["checks"] if c["result"] == "BLOCK"])
        self.assertFalse(r["runtime_verified"])
        self.assertFalse(r["deployment_ready"])

    def env(self):
        return self.configuration["app_deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]

    def test_example_only_proves_selected_configuration(self):
        result = preflight.check(self.configuration)
        self.assertEqual(result["configuration_result"], "PASS")
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["deployment_ready"])
        self.assertTrue(result["remaining_runtime_evidence"])

    def test_cloudtrail_or_api_logs_do_not_replace_kubernetes_audit(self):
        self.configuration["eks_request"]["logging"]["clusterLogging"][0]["types"] = ["api", "authenticator"]
        self.configuration["cloudtrail"] = {"IsLogging": True}
        self.blocked("eks.audit")

    def test_audit_disabled_or_ambiguous_is_blocked(self):
        logs = self.configuration["eks_request"]["logging"]["clusterLogging"]
        logs.append({"types": ["audit"], "enabled": False})
        self.blocked("eks.audit")

    def test_pg_audit_exclusion_and_connections_off_are_blocked(self):
        params = self.configuration["cnpg_cluster"]["spec"]["postgresql"]["parameters"]
        params["pgaudit.log"] = "all, -ddl"
        self.blocked("db.ddl")
        params["log_connections"] = "off"
        self.blocked("db.connections")

    def test_parameter_value_logging_is_blocked(self):
        self.configuration["cnpg_cluster"]["spec"]["postgresql"]["parameters"]["pgaudit.log_parameter"] = "on"
        self.blocked("db.parameter_privacy")

    def test_cnpg_direct_app_connection_is_blocked(self):
        next(e for e in self.env() if e["name"] == "DB_HOST")["value"] = "source-db-rw.source.svc.cluster.local"
        self.blocked("app.pooler")

    def test_db_probe_does_not_make_cutover_unready(self):
        app = self.configuration["app_deployment"]["spec"]["template"]["spec"]["containers"][0]
        app["readinessProbe"]["httpGet"]["path"] = "/api/health/db"
        self.blocked("app.probes")

    def test_static_credentials_and_sdk_dependency_removal_are_blocked(self):
        self.env().append({"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {"name": "static-key", "key": "key"}}})
        self.blocked("app.sdk_dependency")
        self.env().pop()
        next(e for e in self.env() if e["name"] == "DB_SECRET_ID")["value"] = ""
        self.blocked("app.sdk_dependency")

    def test_duplicate_env_does_not_hide_runtime_override(self):
        self.env().append(copy.deepcopy(next(e for e in self.env() if e["name"] == "DB_HOST")))
        self.blocked("app.env")

    def test_pooler_duplicate_hosts_inline_password_and_weak_tls_are_blocked(self):
        original = self.configuration["pgbouncer_ini"]
        self.configuration["pgbouncer_ini"] = original.replace("port=5432", "host=other port=5432")
        self.blocked("pooler.parse")
        self.configuration["pgbouncer_ini"] = original.replace("dbname=chat", "dbname=chat password=never-print-this")
        self.blocked("pooler.backend")
        self.assertNotIn("never-print-this", json.dumps(preflight.check(self.configuration)))
        self.configuration["pgbouncer_ini"] = original.replace("verify-full", "require")
        self.blocked("pooler.tls")

    def test_application_cannot_be_pooler_admin(self):
        self.configuration["pgbouncer_ini"] = self.configuration["pgbouncer_ini"].replace("cutover_control", "chat_app")
        self.blocked("pooler.admin")

    def test_eso_static_key_and_wrong_namespace_are_blocked(self):
        aws = self.configuration["secret_store"]["spec"]["provider"]["aws"]
        aws["auth"]["secretRef"] = {"accessKeyIDSecretRef": {"name": "static"}}
        self.blocked("eso.identity")
        del aws["auth"]["secretRef"]
        self.configuration["external_secret"]["metadata"]["namespace"] = "other"
        self.blocked("eso.admin_reference")

    def test_inline_admin_token_and_wrong_secret_binding_are_blocked(self):
        token = next(e for e in self.env() if e["name"] == "ADMIN_TOKEN")
        token["value"] = "never-print-this"
        self.blocked("eso.admin_reference")
        del token["value"]
        token["valueFrom"]["secretKeyRef"]["name"] = "wrong-secret"
        self.blocked("eso.admin_reference")

    def test_missing_configuration_is_not_success(self):
        self.configuration = {"schema_version": 1}
        self.blocked("eks.audit")

    def test_malformed_json_cli_returns_block_without_input_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"password": "never-print-this", bad}')
            p = subprocess.run([sys.executable, str(SOURCE / "preflight.py"), str(path)], capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["configuration_result"], "BLOCK")
        self.assertNotIn("never-print-this", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
