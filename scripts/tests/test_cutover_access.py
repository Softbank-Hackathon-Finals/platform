"""Keep the Executor's new capability away from database/operator pods."""

import importlib.util
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cutover_access", ROOT / "infra/envs/cutover-access/render.py")
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)
spec = importlib.util.spec_from_file_location("cutover_verify", ROOT / "infra/envs/cutover-access/verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class CutoverAccessTests(unittest.TestCase):
    def test_grant_only_current_application_and_pooler_exec(self):
        result = access.render("source-dev", "source-pgbouncer-config", ["app-a", "app-b"], "pooler-a")
        role, binding = result["items"]
        rules = role["rules"]
        self.assertEqual(rules[1]["resourceNames"], ["app-a", "app-b", "pooler-a"])
        self.assertEqual(set(rules[1]["verbs"]), {"get", "create"})
        self.assertEqual(rules[2]["resourceNames"], ["source-pgbouncer-config"])
        self.assertEqual(set(rules[2]["verbs"]), {"get", "patch"})
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount", "name": "bravo-executor", "namespace": "source-dev"}])
        self.assertEqual({r for rule in rules for r in rule["resources"]}, {"pods", "pods/exec", "configmaps"})
        self.assertNotIn("*", str(result))

    def test_separate_app_namespace_binds_same_executor_identity(self):
        result = access.render("target-app", "target-pooler", ["app-a"], "pooler-a", "platform-access")
        self.assertEqual(result["items"][1]["subjects"][0]["namespace"], "platform-access")
        self.assertEqual(result["items"][0]["metadata"]["namespace"], "target-app")

    def test_refuse_empty_or_indistinguishable_workload_set(self):
        for pods in ([], ["pooler-a"]):
            with self.assertRaises(ValueError):
                access.render("source-dev", "pooler-config", pods, "pooler-a")

    def test_explicit_pooler_only_grants_no_current_or_future_app(self):
        result = access.render("target-dev", "pooler-config", [], "pooler-a", pooler_only=True)
        self.assertEqual(result["items"][0]["rules"][1]["resourceNames"], ["pooler-a"])
        with self.assertRaises(ValueError):
            access.render("target-dev", "pooler-config", ["app-a"], "pooler-a", pooler_only=True)

    def test_empty_app_set_cannot_silently_pass_full_verification(self):
        with patch.object(verify, "token_config") as token, self.assertRaises(ValueError):
            verify.verify("private", "target-dev", "pooler-config", [], "pooler", "db", "pooler")
        token.assert_not_called()

    def test_pooler_only_report_does_not_claim_app_or_cutover_ready(self):
        def call(args, payload=None):
            verb = args[args.index("--namespace") + 2]
            if verb == "auth":
                allowed = ("configmaps/pooler-config" in args or "--subresource=exec" in args) and payload == "bravo-executor"
                return CompletedProcess(args, 0 if allowed else 1, "yes" if allowed else "no", "")
            if verb == "get" and args[-1] == "name" and ("pods" in args or payload == "bravo-executor"):
                return CompletedProcess(args, 0, "name", "")
            if verb == "exec" and "pooler" in args and payload == "bravo-executor":
                return CompletedProcess(args, 0, "writable", "")
            return CompletedProcess(args, 1, "", "Forbidden")

        with patch.object(verify, "token_config", side_effect=lambda _k, _n, identity: identity), patch.object(
            verify, "run", side_effect=call
        ):
            report = verify.verify("private", "target-dev", "pooler-config", [], "pooler", "db", "pooler", check_app=False)
        self.assertTrue(report["passed"])
        self.assertFalse(report["app_block_read_requested"])
        self.assertFalse(report["app_access_verified"])
        self.assertFalse(report["cutover_ready"])

    def test_reject_wildcards_and_system_namespaces(self):
        for ns in ("kube-system", "default", "*", "source;evil", "", None):
            with self.assertRaises(ValueError):
                access.render(ns, "pooler-config", ["app-a"], "pooler-a")

    def test_replacement_does_not_inherit_exec(self):
        rules = access.render("source-dev", "pooler-config", ["app-old"], "pooler-old")["items"][0]["rules"]
        allowed = rules[1]["resourceNames"]
        for pod in ("app-new", "pooler-new", "source-db-1", "operator"):
            self.assertNotIn(pod, allowed)

    def test_denial_requires_actual_forbidden_not_a_broken_connection(self):
        self.assertTrue(verify.denied(CompletedProcess([], 1, "", "Error: Forbidden")))
        for result in (CompletedProcess([], 0, "", ""), CompletedProcess([], 1, "", "timeout")):
            self.assertFalse(verify.denied(result))

    def test_auth_query_network_failure_is_not_a_successful_denial(self):
        with patch.object(verify, "token_config", return_value="private config"), patch.object(
            verify, "run", return_value=CompletedProcess([], 1, "", "connection refused")
        ), self.assertRaisesRegex(RuntimeError, "authorization_readback_failed"):
            verify.verify("private", "source-dev", "pooler-config", ["app"], "pooler", "db", "pooler")

    def test_app_probe_reads_local_token_without_returning_it_or_changing_block(self):
        self.assertIn("os.environ['ADMIN_TOKEN']", verify.APP_READ)
        self.assertIn("http://127.0.0.1:8080/admin/write-block", verify.APP_READ)
        self.assertNotIn("PUT", verify.APP_READ)
        self.assertNotIn("POST", verify.APP_READ)
        self.assertIn("print('blocked')", verify.APP_READ)

    def test_config_probe_never_overwrites_durable_or_active_config(self):
        self.assertIn("mktemp /etc/pgbouncer/.executor-probe.", verify.POOLER_WRITE)
        self.assertIn("trap", verify.POOLER_WRITE)
        self.assertNotIn("> /etc/pgbouncer/pgbouncer.ini", verify.POOLER_WRITE)
        self.assertNotIn("RELOAD", verify.POOLER_WRITE)
