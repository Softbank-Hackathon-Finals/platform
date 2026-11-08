"""Failure and ownership behavior of fixed keyless object operations."""
import base64
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
        self.report["effects_started"] = True

    def opened(self):
        self.open_count += 1

    def keep_open(self):
        self.report.update(cleanup="CLEAN", final_state="open", automatically_closes=False)

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

    def test_successful_explicit_open_is_not_automatically_closed(self):
        api = FakeAPI(action="open")
        runner = Runner(api)
        runner.run()
        self.assertEqual((runner.open_count, runner.close_count), (1, 0))
        self.assertEqual(api.value["report"]["final_state"], "open")
        self.assertEqual(api.value["report"]["verdict"], "PASS")
        self.assertEqual(api.value["report"]["cleanup"], "CLEAN")

    def test_cycle_cannot_close_persistent_open_work_mode(self):
        api = FakeAPI()
        api.value["desired_state"] = "open"
        runner = Runner(api)
        runner.run()
        self.assertEqual((runner.open_count, runner.close_count), (0, 0))
        self.assertEqual(api.value["report"]["error"], "open_work_mode_requires_explicit_close")

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

    def test_recovery_preserves_original_failure_after_closure_succeeds(self):
        api = FakeAPI()
        runner = Runner(api)
        with patch.object(runner, "opened", side_effect=worker.Failure("anonymous_boundary_mismatch")), \
                patch.object(runner, "close", side_effect=worker.Failure("kubernetes_unavailable")):
            runner.run()
        Runner(api).run()
        self.assertEqual(api.value["phase"], "FINISHED")
        self.assertEqual(api.value["report"]["error"], "anonymous_boundary_mismatch")
        self.assertEqual(api.value["report"]["verdict"], "FAIL")
        self.assertEqual(api.value["report"]["cleanup"], "CLEAN")

    def test_repeated_recovery_keeps_bounded_receipt(self):
        api = FakeAPI("RECOVERY_REQUIRED")
        api.value["report"]["effects_started"] = True
        for index in range(100):
            runner = Runner(api)
            runner.pod_uid = str(index).zfill(36)
            def failed_close(runner=runner):
                runner.report.setdefault("transitions", []).append({"state": "closed", "ready_seconds": 22})
                runner.report.setdefault("previous_closure_checks", []).append([{"name": "closed_anonymous_get", "pass": False}])
                raise worker.Failure("kubernetes_unavailable")
            with patch.object(runner, "close", side_effect=failed_close):
                runner.run()
        self.assertEqual(api.value["phase"], "RECOVERY_REQUIRED")
        self.assertLess(len(json.dumps(api.value)), 6000)
        self.assertEqual(len(api.value["report"]["recovery_executor_pod_uids"]), 8)

    def test_waited_incomplete_result_is_not_success_exit(self):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "profile.json"
            profile.write_text('{"cluster_uid":"cluster"}')
            for command in ("open", "cleanup", "status"):
                args = SimpleNamespace(command=command, run_id="objects-" + "b" * 32,
                                       access=profile, wait_seconds=1, request_key=None,
                                       hold_seconds=0, acknowledge_anonymous_read=True)
                for phase in ("PENDING", "RECOVERY_REQUIRED"):
                    with self.subTest(command=command, phase=phase), \
                            patch.object(cli, "kubectl", side_effect=[json.dumps({"status":{"userInfo":{"username":request()["owner"]}}}), "cluster"]), \
                            patch.object(cli, "Client") as client, patch.object(cli.time, "monotonic", side_effect=[0, 2]):
                        client.return_value.call.return_value = {"phase": phase}
                        client.return_value.run_id.return_value = args.run_id
                        client.return_value.new_key.return_value = "fixture-key"
                        self.assertEqual(cli.execute(args), 3)

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


class StoreAPI(FakeAPI):
    """In-memory Kubernetes/S3 boundary; the production Worker runs unchanged."""
    def __init__(self, action="cycle"):
        super().__init__(action=action)
        self.value["config"] = {"cluster_uid": "cluster", "secret_uid": "secret", "deployment_uid": "deployment"}
        self.auth = OperationsTest().document()
        self.app_pair = self.auth["identities"][1]["credentials"][0].copy()
        self.generation = 1
        self.mutations = []
        self.secret_annotations = {}

    def call(self, method, path, body=None, patch=False):
        if method == "PATCH":
            self.mutations.append(path)
            if path.endswith(worker.SECRET):
                self.auth = json.loads(base64.b64decode(next(item["value"] for item in body if item["path"] == "/data/s3.json")))
            else:
                self.generation += 1
        if path.endswith("kube-system"):
            return {"metadata": {"uid": "cluster"}}
        if path.endswith("target-app-objects"):
            return {"data": {name: base64.b64encode(self.app_pair[key].encode()).decode() for name, key in (("access_key", "accessKey"), ("secret_key", "secretKey"))}}
        if path.endswith(worker.SECRET):
            return {"metadata": {"uid": "secret", "resourceVersion": "1", "annotations": dict(self.secret_annotations)},
                    "data": {"s3.json": base64.b64encode(json.dumps(self.auth).encode()).decode()}}
        if path == worker.APPS:
            return {"metadata": {"uid": "deployment", "resourceVersion": "1", "generation": self.generation},
                    "spec": {"replicas": 1, "template": {"metadata": {}, "spec": {"containers": [{"image": "fixture"}]}}},
                    "status": {"observedGeneration": self.generation, "updatedReplicas": 1, "readyReplicas": 1, "replicas": 1}}
        raise AssertionError(path)


class StoreS3:
    def __init__(self, api):
        self.api = api
        self.objects = {"original.txt": b"original attachment"}
        self.puts = 0
        self.delete_canary_during_open = False

    def call(self, method, bucket, key="", *, query=(), body=b"", credentials=None):
        if query == [("policy", "")]:
            return 404, "NoSuchBucketPolicy", b""
        opened = any(item["name"] == "anonymous" for item in self.api.auth["identities"])
        if credentials is None:
            if bucket == worker.OTHER_BUCKET and self.delete_canary_during_open:
                self.objects.pop("original.txt", None)
            if not opened or bucket != worker.BUCKET or method not in ("GET", "HEAD") or not key:
                return 403, None, b""
        if method == "GET" and not key:
            data = "<ListBucketResult>" + "".join(f"<Contents><Key>{k}</Key><Size>{len(v)}</Size></Contents>" for k, v in self.objects.items()) + "</ListBucketResult>"
            return 200, None, data.encode()
        if method in ("GET", "HEAD"):
            return (200, None, self.objects[key]) if key in self.objects else (404, None, b"")
        if method == "PUT":
            self.puts += 1
            self.objects[key] = body
            return 200, None, b""
        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, None, b""
        raise AssertionError(method)


class WorkerBoundaryTest(unittest.TestCase):
    def run_worker(self, api, store):
        runner = worker.Worker(api, "executor")
        runner.s3 = store
        # The renderer and entire Worker run; only the T9 HTTP probe is an I/O fake.
        def probe(state):
            actual = "open" if any(i["name"] == "anonymous" for i in api.auth["identities"]) else "closed"
            return runner.case(state + "_t9_probe", actual, state)
        with patch.object(runner, "probe", side_effect=probe):
            runner.run()
        return api.value["report"]

    def test_empty_bucket_close_finishes_with_app_failure_and_clean_objects(self):
        api = StoreAPI("close")
        store = StoreS3(api)
        store.objects.clear()
        report = self.run_worker(api, store)
        self.assertEqual(api.value["phase"], "FINISHED")
        self.assertEqual((report["verdict"], report["cleanup"], report["final_state"]), ("FAIL", "CLEAN", "closed"))
        self.assertEqual(report["application_error"], "existing_app_canary_unavailable")

    def test_unknown_identity_cycle_refuses_without_any_effect_or_recovery_lock(self):
        api = StoreAPI()
        api.auth["identities"].append({"name": "backup-writer", "actions": []})
        store = StoreS3(api)
        report = self.run_worker(api, store)
        self.assertEqual(api.value["phase"], "FINISHED")
        self.assertEqual(report["error"], "unsupported_identity_layout")
        self.assertEqual((api.mutations, store.puts), ([], 0))

    def test_app_secret_mismatch_cycle_refuses_without_restart(self):
        api = StoreAPI()
        api.app_pair["accessKey"] = "different"
        store = StoreS3(api)
        report = self.run_worker(api, store)
        self.assertEqual(api.value["phase"], "FINISHED")
        self.assertEqual(report["error"], "application_secret_does_not_match_client_identity")
        self.assertEqual((api.mutations, store.puts), ([], 0))

    def test_deleted_app_canary_does_not_prevent_closure_and_owned_cleanup(self):
        api = StoreAPI()
        store = StoreS3(api)
        store.delete_canary_during_open = True
        report = self.run_worker(api, store)
        self.assertEqual(api.value["phase"], "FINISHED")
        self.assertEqual((report["verdict"], report["cleanup"], report["final_state"]), ("FAIL", "CLEAN", "closed"))
        self.assertFalse(store.objects)
        self.assertEqual(report["application_error"], "application_readback_failed")

    def test_platform_owned_exposure_is_never_reopened_and_refuses_before_any_effect(self):
        for action in ("open", "cycle"):
            api = StoreAPI(action)
            api.secret_annotations[worker.EXPOSURE_OWNER] = "t31/pol-0001"
            store = StoreS3(api)
            report = self.run_worker(api, store)
            self.assertEqual(api.value["phase"], "FINISHED")
            self.assertEqual(report["error"], "exposure_owned_by_platform")
            self.assertEqual((api.mutations, store.puts), ([], 0))
            self.assertFalse(any(i["name"] == "anonymous" for i in api.auth["identities"]))

    def test_platform_owned_exposure_can_still_be_closed(self):
        api = StoreAPI("close")
        api.secret_annotations[worker.EXPOSURE_OWNER] = "t31/pol-0001"
        report = self.run_worker(api, StoreS3(api))
        self.assertEqual(api.value["phase"], "FINISHED")
        self.assertNotEqual(report.get("error"), "exposure_owned_by_platform")
        self.assertEqual(report["final_state"], "closed")

    def test_open_checks_ownership_again_on_the_write_it_fences(self):
        api = StoreAPI("open")
        runner = worker.Worker(api, "executor")
        runner.request = {"run_id": "objects-" + "a" * 32}
        runner.config = api.value["config"]
        api.secret_annotations[worker.EXPOSURE_OWNER] = "t31/pol-0002"  # T31 closed after the request began
        with self.assertRaises(worker.Failure) as raised:
            runner.transition("open-demo")
        self.assertEqual(str(raised.exception), "exposure_owned_by_platform")
        self.assertEqual(api.mutations, [])

    def test_real_worker_open_keeps_original_and_deletes_only_own_objects(self):
        api = StoreAPI("open")
        store = StoreS3(api)
        report = self.run_worker(api, store)
        self.assertEqual((report["verdict"], report["cleanup"], report["final_state"]), ("PASS", "CLEAN", "open"))
        self.assertEqual(store.objects, {"original.txt": b"original attachment"})
        self.assertEqual(sum(path == worker.APPS for path in api.mutations), 1)


if __name__ == "__main__":
    unittest.main()
