"""Keep the Executor's new capability away from database/operator pods."""

import importlib.util
import json
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


def rules_response(args, rules=None, **status):
    return CompletedProcess(args, 0, json.dumps({
        "kind": "SelfSubjectRulesReview", "apiVersion": "authorization.k8s.io/v1",
        "status": {"incomplete": False, "resourceRules": rules or [], **status},
    }), "")


def permitted_workload_call(args, payload=None, *, secret_holder=None, rules=None,
                            secret_namespace="target-dev", status=None, seen=None):
    if seen is not None:
        seen.append((args, payload))
    namespace = args[args.index("--namespace") + 1]
    verb = args[args.index("--namespace") + 2]
    if "--raw" in args:
        request = json.loads(Path(args[args.index("-f") + 1]).read_text())
        assert request["spec"]["namespace"] == namespace
        relevant = payload == secret_holder and namespace == secret_namespace
        return rules_response(args, rules if relevant else [], **(status or {}))
    if verb == "auth":
        allowed = ("configmaps/pooler-config" in args or "--subresource=exec" in args) and payload == "bravo-executor"
        # An additive Role grants only GET of an absent/future named Secret.
        # Generic get/list/watch queries remain denied.
        if (payload == secret_holder and namespace == secret_namespace and
                "get" in args and "secrets/future-private" in args):
            allowed = True
        return CompletedProcess(args, 0 if allowed else 1, "yes" if allowed else "no", "")
    if verb == "get" and args[-1] == "name" and ("pods" in args or payload == "bravo-executor"):
        return CompletedProcess(args, 0, "name", "")
    if verb == "exec" and "pooler" in args and payload == "bravo-executor":
        return CompletedProcess(args, 0, "writable", "")
    return CompletedProcess(args, 1, "", "Forbidden")


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
            if "--raw" in args:
                return rules_response(args)
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

    def test_token_check_queries_named_token_subresource(self):
        seen = []

        def call(args, payload=None):
            seen.append(args)
            if "can-i" in args:
                allowed = "--subresource=token" in args
                return CompletedProcess(args, 0 if allowed else 1, "yes" if allowed else "no", "")
            return CompletedProcess(args, 1, "", "Forbidden")

        with patch.object(verify, "token_config", return_value="private config"), patch.object(
            verify, "run", side_effect=call
        ):
            report = verify.verify("private", "target-dev", "pooler-config", [], "pooler", "db", "pooler", check_app=False)
        for identity in ("executor", "collector"):
            self.assertFalse(report["checks"][identity + "_token_issue_denied"])
            self.assertTrue(any("serviceaccounts/bravo-" + identity in args and "--subresource=token" in args for args in seen))
        self.assertFalse(any("serviceaccounts/token" in args for args in seen))

    def test_reject_wildcards_and_system_namespaces(self):
        for ns in ("kube-system", "default", "*", "source;evil", "", None):
            with self.assertRaises(ValueError):
                access.render(ns, "pooler-config", ["app-a"], "pooler-a")

    def test_own_token_grant_is_checked_in_the_identity_namespace(self):
        for token_holder in (None, "bravo-executor", "bravo-collector"):
            with self.subTest(token_holder=token_holder):
                token_queries = []

                def call(args, payload=None, token_queries=token_queries, token_holder=token_holder):
                    if "--raw" in args:
                        return rules_response(args)
                    namespace = args[args.index("--namespace") + 1]
                    verb = args[args.index("--namespace") + 2]
                    if verb == "auth":
                        if "--subresource=token" in args:
                            token_queries.append((namespace, payload))
                            allowed = namespace == "platform-access" and payload == token_holder
                        else:
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
                    report = verify.verify(
                        "private", "target-dev", "pooler-config", [], "pooler", "db", "pooler",
                        identity_namespace="platform-access", check_app=False,
                    )
                self.assertEqual(report["passed"], token_holder is None)
                self.assertEqual(token_queries, [
                    ("platform-access", "bravo-executor"), ("platform-access", "bravo-collector"),
                ])
                for identity in ("executor", "collector"):
                    self.assertEqual(report["checks"][identity + "_token_issue_denied"], "bravo-" + identity != token_holder)

    def test_named_secret_get_overgrant_fails_for_each_identity(self):
        for holder in ("bravo-executor", "bravo-collector"):
            for secret_namespace in ("target-dev", "platform-access"):
                with self.subTest(holder=holder, namespace=secret_namespace):
                    seen = []
                    rule = {"apiGroups": [""], "resources": ["secrets"],
                            "verbs": ["get"], "resourceNames": ["future-private"]}

                    def call(args, payload=None):
                        return permitted_workload_call(
                            args, payload, secret_holder=holder, rules=[rule],
                            secret_namespace=secret_namespace, seen=seen)

                    with patch.object(verify, "token_config", side_effect=lambda _k, _n, identity: identity), patch.object(
                        verify, "run", side_effect=call
                    ):
                        report = verify.verify("private", "target-dev", "pooler-config", [], "pooler", "db", "pooler",
                                               identity_namespace="platform-access", check_app=False)
                    self.assertFalse(report["passed"])
                    self.assertTrue(report["checks"][holder.removeprefix("bravo-") + "_secret_list_denied"])
                    self.assertTrue(any("secrets/future-private" in args and payload == holder for args, payload in seen))
                    # Secret data must never be fetched, even when authorization is accidentally broad.
                    self.assertFalse(any("can-i" not in args and "secrets" in args for args, _ in seen))

    def test_secret_audit_rejects_wildcard_or_named_read_rules(self):
        for action in ("get", "list", "watch", "*"):
            for names in ([], ["future-private"]):
                with self.subTest(action=action, names=names):
                    rule = {"apiGroups": ["*"], "resources": ["*"],
                            "verbs": [action], "resourceNames": names}
                    call = lambda *a, **kw: rules_response(a, [rule])
                    queries = []

                    def can(verb, resource, target_namespace):
                        queries.append((verb, resource, target_namespace))
                        return resource == "secrets/future-private" and verb in (action, "get" if action == "*" else action)

                    clean, audit = verify.secret_read_audit(call, can, "target-dev")
                    self.assertFalse(clean)
                    self.assertEqual(audit["read_rule_count"], 1)
                    self.assertEqual(audit["named_targets_checked"], len(names))
                    self.assertEqual(len(queries), 3 + 3 * len(names))
                    self.assertTrue(all(ns == "target-dev" for _, _, ns in queries))
                    self.assertNotIn("future-private", json.dumps(audit))

    def test_live_secret_authorization_overrides_empty_rule_inventory(self):
        for allowed in ("get", "list", "watch"):
            with self.subTest(allowed=allowed):
                call = lambda *a, **kw: rules_response(a)
                can = lambda verb, resource, target_namespace: verb == allowed
                clean, audit = verify.secret_read_audit(call, can, "target-dev")
                self.assertFalse(clean)
                self.assertEqual(audit["allowed_read_queries"], 1)

    def test_incomplete_unavailable_and_malformed_rules_never_pass(self):
        responses = [
            rules_response([], incomplete=True),
            rules_response([], evaluationError="authorizer cannot enumerate"),
            rules_response([], incomplete=None),
            rules_response([], resourceRules=None),
            rules_response([], [{"apiGroups": [""], "resources": "secrets", "verbs": ["get"]}]),
            rules_response([], [{"apiGroups": [], "resources": ["secrets"], "verbs": ["get"]}]),
            CompletedProcess([], 0, '{"status": {"incomplete": false}}', ""),
            CompletedProcess([], 0, "[]", ""),
            CompletedProcess([], 0, "not-json", ""),
            CompletedProcess([], 1, "", "Forbidden"),
        ]
        for response in responses:
            with self.subTest(response=response):
                call = lambda *a, **kw: response
                clean, audit = verify.secret_read_audit(call, lambda *a, **kw: False, "target-dev")
                self.assertFalse(clean)
                self.assertFalse(audit["rules_complete"])

    def test_non_secret_rules_pass_and_request_file_contains_no_credentials(self):
        paths = []

        def call(*args, target_namespace):
            body = Path(args[args.index("-f") + 1])
            paths.append(body)
            self.assertEqual(json.loads(body.read_text()), {
                "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview",
                "spec": {"namespace": "target-dev"},
            })
            self.assertEqual(target_namespace, "target-dev")
            return rules_response(args, [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                {"apiGroups": ["example.test"], "resources": ["secrets"], "verbs": ["*"]},
            ])

        clean, audit = verify.secret_read_audit(call, lambda *a, **kw: False, "target-dev")
        self.assertTrue(clean)
        self.assertTrue(audit["rules_complete"])
        self.assertTrue(paths and all(not path.exists() for path in paths))

    def test_secret_audit_request_file_is_removed_on_transport_failure(self):
        paths = []

        def call(*args, **kwargs):
            paths.append(Path(args[args.index("-f") + 1]))
            raise OSError("connection failed")

        with self.assertRaises(OSError):
            verify.secret_read_audit(call, lambda *a, **kw: False, "target-dev")
        self.assertTrue(paths and all(not path.exists() for path in paths))

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
