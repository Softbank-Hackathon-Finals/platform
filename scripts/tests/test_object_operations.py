"""Failure and ownership behavior of fixed keyless object operations."""
import copy
import importlib.util
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "infra/envs/target-workloads")]
import object_exposure
from object_operations import worker
from object_operations.install import manifests

spec = importlib.util.spec_from_file_location("object_cli", ROOT / "scripts/object_operations.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def request(action="cycle"):
    return {"run_id": "objects-" + "a" * 32, "action": action, "owner": "system:serviceaccount:bravo-team-access:member-01",
            "hold_seconds": 0, "acknowledge_anonymous_read": True,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}


class FakeAPI:
    def __init__(self, phase="PENDING", action="cycle"):
        self.value = {"phase": phase, "request": request(action), "config": {}, "history": [],
                      "report": {"checks": [], "cleanup": "UNCONFIRMED"}}
        self.rv = 1

    def state(self):
        return {"metadata": {"uid": "state-uid", "resourceVersion": str(self.rv)}}, copy.deepcopy(self.value)

    def save(self, doc, value):
        if doc["metadata"]["resourceVersion"] != str(self.rv):
            raise worker.Failure("kubernetes_409")
        self.rv += 1
        self.value = copy.deepcopy(value)


class Runner(worker.Worker):
    def __init__(self, api):
        super().__init__(api, "pod-uid")
        self.open_count = 0
        self.close_count = 0

    def baseline(self):
        pass

    def opened(self):
        self.open_count += 1

    def close(self):
        self.close_count += 1
        self.report["cleanup"] = "CLEAN"


class OperationsTest(unittest.TestCase):
    def document(self):
        env = {}
        for name, pair in object_exposure.ENV.items():
            env[pair[0]], env[pair[1]] = name[0].upper() * 20, name[0].lower() * 40
        env[object_exposure.REHEARSAL_ENV[0]] = "R" * 20
        env[object_exposure.REHEARSAL_ENV[1]] = "r" * 40
        return object_exposure.render("closed", worker.BUCKET, env, rehearsal_bucket=worker.OTHER_BUCKET)

    def test_renderer_preserves_existing_credentials_and_rehearsal_identity(self):
        original = self.document()
        opened, _, state = worker.render_existing(original, "open-demo", request()["run_id"])
        self.assertEqual(state, "closed")
        closed, _, state = worker.render_existing(opened, "closed", request()["run_id"])
        self.assertEqual(state, "open-demo")
        self.assertEqual(closed, original)

    def test_installed_legacy_identity_names_are_preserved(self):
        original = self.document()
        original["identities"][0]["name"] = "target-object-bootstrap"
        original["identities"][1]["name"] = "target-object-app"
        opened, _, _ = worker.render_existing(original, "open-demo", request()["run_id"])
        closed, _, _ = worker.render_existing(opened, "closed", request()["run_id"])
        self.assertEqual(closed, original)

    def test_unknown_identity_or_rotation_is_not_silently_discarded(self):
        for change in ("identity", "credential", "action"):
            doc = self.document()
            if change == "identity":
                doc["identities"].append({"name": "foreign", "actions": []})
            elif change == "credential":
                doc["identities"][0]["credentials"].append({"accessKey": "rotating", "secretKey": "private"})
            else:
                doc["identities"][0]["actions"].append("Read:foreign-bucket")
            with self.subTest(change=change), self.assertRaises(worker.Failure):
                worker.render_existing(doc, "open-demo", request()["run_id"])

    def test_expired_pending_window_never_restarts_or_opens_store(self):
        api = FakeAPI()
        api.value["request"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        runner = Runner(api)
        runner.run()
        self.assertEqual((runner.open_count, runner.close_count), (0, 0))
        self.assertEqual(api.value["report"]["verdict"], "FAIL")

    def test_cancel_before_claim_never_opens_store(self):
        api = FakeAPI()
        api.value["request"]["cancel"] = True
        runner = Runner(api)
        runner.run()
        self.assertEqual((runner.open_count, runner.close_count), (0, 0))

    def test_interrupted_cycle_only_closes_without_replaying(self):
        api = FakeAPI("RUNNING")
        runner = Runner(api)
        runner.run()
        self.assertEqual((runner.open_count, runner.close_count), (0, 1))
        self.assertEqual(api.value["report"]["verdict"], "UNCONFIRMED")
        self.assertEqual(api.value["report"]["cleanup"], "CLEAN")

    def test_interrupted_read_only_check_does_not_restart_store(self):
        api = FakeAPI("RUNNING", "check")
        runner = Runner(api)
        runner.run()
        self.assertEqual((runner.open_count, runner.close_count), (0, 0))

    def test_open_error_closes_and_preserves_original_failure(self):
        api = FakeAPI()
        runner = Runner(api)
        with patch.object(runner, "opened", side_effect=worker.Failure("probe_unavailable")):
            runner.run()
        self.assertEqual(runner.close_count, 1)
        self.assertEqual(api.value["report"]["error"], "probe_unavailable")
        self.assertEqual(api.value["report"]["verdict"], "FAIL")

    def test_close_error_remains_recoverable_and_not_clean(self):
        api = FakeAPI()
        runner = Runner(api)
        with patch.object(runner, "close", side_effect=worker.Failure("kubernetes_unavailable")):
            runner.run()
        self.assertEqual(api.value["phase"], "RECOVERY_REQUIRED")
        self.assertEqual(api.value["report"]["cleanup"], "UNCONFIRMED")

    def test_stale_executor_cannot_write_after_recovery_takes_over(self):
        api = FakeAPI()
        runner = Runner(api)
        runner.request = api.value["request"]
        api.value["executor"] = "replacement-pod"
        with self.assertRaisesRegex(worker.Failure, "execution_fenced"):
            runner.persist("RUNNING")

    def test_public_policy_is_deleted_and_absence_is_read_back(self):
        runner = worker.Worker(FakeAPI(), "pod-uid")
        runner.request = request()
        runner.credentials = {"bootstrap": {}}
        with patch.object(runner, "own"), patch.object(runner.s3, "call", side_effect=[(200, None, b""), (204, None, b""), (404, "NoSuchBucketPolicy", b"")]) as call:
            self.assertTrue(runner.policy(remove=True))
        self.assertEqual([entry.args[0] for entry in call.call_args_list], ["GET", "DELETE", "GET"])
        self.assertTrue(runner.report["removed_existing_bucket_policy"])

    def test_policy_forbidden_or_transport_error_is_not_absent(self):
        runner = worker.Worker(FakeAPI(), "pod-uid")
        runner.credentials = {"bootstrap": {}}
        for response in [(403, None, b""), ("UNAVAILABLE", None, b""), (404, None, b"")]:
            with self.subTest(response=response), patch.object(runner.s3, "call", return_value=response), self.assertRaisesRegex(worker.Failure, "bucket_policy_not_absent"):
                runner.policy(remove=True)

    def test_changed_target_uid_prevents_secret_or_deployment_mutation(self):
        runner = worker.Worker(FakeAPI(), "pod-uid")
        runner.config = {"cluster_uid": "cluster", "secret_uid": "original", "deployment_uid": "deployment"}
        with patch.object(runner.api, "call", create=True, side_effect=[{"metadata": {"uid": "cluster"}}, {"metadata": {"uid": "replacement"}}, {"metadata": {"uid": "deployment"}}]) as call, self.assertRaisesRegex(worker.Failure, "target_uid_changed"):
            runner.transition("open-demo")
        self.assertTrue(all(entry.args[0] == "GET" for entry in call.call_args_list))

    def test_installed_worker_cannot_create_jobs_read_other_secrets_or_delete_namespaces(self):
        objects, initial = manifests("cluster-uid", "secret-uid", "deployment-uid")
        role = next(item for item in objects if item["kind"] == "Role")
        self.assertEqual({name for rule in role["rules"] for name in rule["resourceNames"]},
                         {worker.STATE, worker.SECRET, worker.DEPLOYMENT, "target-app-objects"})
        self.assertTrue(all(set(rule["verbs"]) <= {"get", "patch"} for rule in role["rules"]))
        self.assertEqual(next(rule for rule in role["rules"] if rule["resourceNames"] == ["target-app-objects"])["verbs"], ["get"])
        cron = next(item for item in objects if item["kind"] == "CronJob")
        self.assertEqual(cron["spec"]["concurrencyPolicy"], "Forbid")
        self.assertEqual(cron["spec"]["jobTemplate"]["spec"]["backoffLimit"], 0)
        self.assertEqual(json.loads(initial["data"]["state.json"])["phase"], "IDLE")


if __name__ == "__main__":
    unittest.main()
