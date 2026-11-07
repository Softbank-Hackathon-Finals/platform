"""Customer intake rejects stale, mixed and foreign observations without write calls."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("customer_status", REPO / "infra/iac/customer_status.py")
customer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(customer)
NOW = datetime(2026, 11, 7, 8, tzinfo=UTC)
REVISION = "a" * 40


def application():
    source = {"repoURL": customer.REPOSITORY, "targetRevision": REVISION,
              "path": "infra/iac/charts/target-dev", "helm": {"valuesObject": {"tailnet": "example-tail.ts.net"}}}
    destination = {"server": "https://kubernetes.default.svc"}
    return {
        "metadata": {"name": "bravo-iac-target-dev", "namespace": customer.NAMESPACE,
                     "uid": "owned-application", "resourceVersion": "17"},
        "spec": {"project": "bravo-iac", "source": deepcopy(source), "destination": deepcopy(destination),
                 "syncPolicy": {"automated": {"enabled": True, "prune": False}}},
        "status": {"sync": {"status": "Synced", "revision": REVISION,
                            "comparedTo": {"source": deepcopy(source), "destination": deepcopy(destination)}},
                   "health": {"status": "Healthy"}, "reconciledAt": NOW.isoformat(),
                   "operationState": {"phase": "Succeeded", "syncResult": {"revision": REVISION, "source": deepcopy(source)}},
                   "resources": [{"kind": "NetworkPolicy", "status": "Synced"}]},
    }


def workloads():
    return [{"metadata": {"generation": 1, "labels": {
        "app.kubernetes.io/instance": customer.NAMESPACE, "app.kubernetes.io/component": component}},
        "spec": {"replicas": 1}, "status": {"observedGeneration": 1, "readyReplicas": 1, "updatedReplicas": 1}}
        for component in sorted(customer.COMPONENTS)]


def assess(app=None, pods=None, **kwargs):
    return customer.assess(app, workloads() if pods is None else pods, environment="target-dev",
                           revision=REVISION, now=NOW, **kwargs)


class CustomerStatus(unittest.TestCase):
    def test_exact_current_sync_proves_only_gitops(self):
        result = assess(application())
        self.assertEqual(result["status"], "SYNC_VERIFIED")
        self.assertEqual(result["synced_resource_count"], 1)
        for key in ("migration_verified", "control_equivalence_verified", "authorizes_cutover"):
            self.assertIs(result[key], False)

    def test_bootstrap_is_not_a_workload_deployment(self):
        self.assertEqual(assess()["status"], "BOOTSTRAP_ONLY")
        self.assertEqual(assess(pods=[])["status"], "BOOTSTRAP_PENDING")

    def test_rejects_every_foreign_source_and_destination(self):
        for section, field, value in (
            ("source", "repoURL", "https://example.invalid/foreign.git"),
            ("source", "path", "infra/iac/charts/source-dev"),
            ("source", "targetRevision", "develop"),
            ("source", "targetRevision", "b" * 40),
            ("destination", "server", "https://example.invalid"),
            ("destination", "namespace", "other-tenant"),
        ):
            with self.subTest(field=field, value=value):
                app = application()
                app["spec"][section][field] = value
                self.assertEqual(assess(app)["status"], "BLOCKED")

    def test_rejects_wrong_project_multiple_sources_and_identity(self):
        for change in (lambda a: a["spec"].update(project="default"),
                       lambda a: a["spec"].update(sources=[a["spec"]["source"]]),
                       lambda a: a["metadata"].update(namespace="other"),
                       lambda a: a["metadata"].update(name="foreign"),
                       lambda a: a["metadata"].update(deletionTimestamp=NOW.isoformat()),
                       lambda a: a["metadata"].pop("resourceVersion")):
            app = application()
            change(app)
            self.assertEqual(assess(app)["status"], "BLOCKED")

    def test_stale_or_incomplete_sync_never_passes(self):
        changes = (
            lambda a: a["status"]["sync"].update(revision="b" * 40),
            lambda a: a["status"]["sync"].update(status="OutOfSync"),
            lambda a: a["status"]["operationState"].update(phase="Failed"),
            lambda a: a["status"]["operationState"]["syncResult"].update(revision="b" * 40),
            lambda a: a["status"]["health"].update(status="Progressing"),
            lambda a: a["status"].update(conditions=[{"type": "ComparisonError"}]),
            lambda a: a["status"].update(resources=[]),
            lambda a: a["status"]["resources"][0].update(status="OutOfSync"),
            lambda a: a["status"].update(reconciledAt=(NOW-timedelta(minutes=6)).isoformat()),
            lambda a: a["status"].update(reconciledAt=(NOW+timedelta(seconds=1)).isoformat()),
            lambda a: a["status"].update(reconciledAt="invalid"),
            lambda a: a["spec"]["source"]["helm"]["valuesObject"].update(tailnet="other.ts.net"),
            lambda a: a["status"]["operationState"]["syncResult"].pop("source"),
        )
        for change in changes:
            app = application()
            change(app)
            self.assertNotEqual(assess(app)["status"], "SYNC_VERIFIED")

    def test_pause_is_explicit_and_does_not_claim_running_sync_cancelled(self):
        app = application()
        app["spec"]["syncPolicy"]["automated"]["enabled"] = False
        self.assertEqual(assess(app)["status"], "SYNC_PAUSED")
        app["operation"] = {"sync": {}}
        self.assertEqual(assess(app)["status"], "SYNC_IN_PROGRESS")
        app.pop("operation")
        app["status"]["operationState"]["phase"] = "Running"
        self.assertEqual(assess(app)["status"], "SYNC_IN_PROGRESS")
        app["status"]["operationState"]["phase"] = "Succeeded"
        app["spec"]["syncPolicy"]["automated"].pop("enabled")
        self.assertEqual(assess(app)["status"], "BLOCKED")

    def test_prune_needs_separate_acknowledgement(self):
        app = application()
        app["spec"]["syncPolicy"]["automated"]["prune"] = True
        self.assertEqual(assess(app)["status"], "BLOCKED")
        self.assertEqual(assess(app, allow_prune=True)["status"], "SYNC_VERIFIED")

    def test_missing_duplicate_unready_controllers_never_pass(self):
        for entries in ([], workloads()[:-1], [*workloads(), workloads()[0]]):
            self.assertEqual(assess(application(), entries)["status"], "BLOCKED")
        for key, value in (("readyReplicas", 0), ("updatedReplicas", 0), ("observedGeneration", 0)):
            entries = workloads()
            entries[0]["status"][key] = value
            self.assertEqual(assess(application(), entries)["status"], "BLOCKED")

    def test_wrong_cluster_refused_before_application_query(self):
        calls = []
        def get(*args, **kwargs):
            calls.append(args)
            return {"metadata": {"uid": "foreign-cluster"}}
        with self.assertRaisesRegex(customer.IntakeError, "CLUSTER_IDENTITY_MISMATCH"):
            customer.inspect(get, expected_cluster_uid="customer-cluster", environment="target-dev", revision=REVISION)
        self.assertEqual(calls, [("namespace", "kube-system")])

    def test_crd_absence_and_concurrent_change_are_not_verified(self):
        for crd, changing in ((False, False), (True, True), (True, False)):
            app_reads = []
            def get(kind, name=None, *, crd=crd, changing=changing, app_reads=app_reads, **kwargs):
                if kind == "namespace":
                    return {"metadata": {"uid": "customer-cluster"}}
                if kind == "crd":
                    return {"metadata": {"name": name}} if crd else {}
                if kind == "deployments,statefulsets":
                    return {"items": workloads()}
                app = application()
                app_reads.append(1)
                if changing and len(app_reads) > 1:
                    app["metadata"]["resourceVersion"] = "18"
                return app
            result = customer.inspect(get, expected_cluster_uid="customer-cluster", environment="target-dev", revision=REVISION, now=NOW)
            expected = "NOT_INSTALLED" if not crd else "BLOCKED" if changing else "SYNC_VERIFIED"
            self.assertEqual(result["status"], expected)

    def test_transport_uses_only_get_and_config_and_hides_stderr(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if "config" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"clusters": [{"cluster": {"server": "https://example.invalid"}}]}))
            return subprocess.CompletedProcess(argv, 0, "{}")
        with tempfile.NamedTemporaryFile() as config, patch.object(customer.subprocess, "run", side_effect=run):
            get = customer.reader(config.name)
            get("namespace", "kube-system")
            self.assertTrue(all("get" in c or "config" in c for c in calls))
        with tempfile.NamedTemporaryFile() as config, patch.object(customer.subprocess, "run", side_effect=subprocess.CalledProcessError(1, [], stderr="SECRET_VALUE")):
            with self.assertRaisesRegex(customer.IntakeError, "KUBERNETES_READ_UNCONFIRMED") as error:
                customer.reader(config.name)
            self.assertNotIn("SECRET_VALUE", str(error.exception))

    def test_plain_or_unverified_tls_is_rejected(self):
        for server, insecure in (("http://example.invalid", False), ("https://example.invalid", True)):
            config = {"clusters": [{"cluster": {"server": server, "insecure-skip-tls-verify": insecure}}]}
            with tempfile.NamedTemporaryFile() as file, patch.object(customer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(config))):
                with self.assertRaisesRegex(customer.IntakeError, "VERIFIED_API_TLS_REQUIRED"):
                    customer.reader(file.name)


if __name__ == "__main__":
    unittest.main()
