"""HTTP authentication, ownership and lost-response idempotency contracts."""
import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "infra/envs/target-workloads")]
from object_operations import server
from object_operations.worker import Failure

OWNER = "system:serviceaccount:bravo-team-access:member-01"
OTHER = "system:serviceaccount:bravo-team-access:member-02"


class FakeAPI:
    def __init__(self):
        self.value = {"phase": "IDLE", "config": {"cluster_uid": "cluster"}, "history": []}
        self.rv = self.saved = 0
        self.user = OWNER

    def call(self, method, path, body=None):
        if path.endswith("tokenreviews"):
            return {"status": {"authenticated": True, "audiences": [server.AUDIENCE], "user": {"username": self.user}}}
        return {"metadata": {"uid": "cluster"}}

    def state(self):
        return {"metadata": {"resourceVersion": self.rv}}, copy.deepcopy(self.value)

    def save(self, doc, value):
        if doc["metadata"]["resourceVersion"] != self.rv:
            raise Failure("kubernetes_409")
        self.value, self.rv = copy.deepcopy(value), self.rv + 1
        self.saved += 1


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.ops = server.Operations(self.api)
        self.key = str(int(time.time())) + "-" + "a" * 32
        self.body = {"action": "check"}

    def assertError(self, code, fn):
        with self.assertRaises(server.HTTPError) as caught:
            fn()
        self.assertEqual(caught.exception.status, code)

    def test_token_review_binds_actor_and_requires_audience(self):
        self.assertEqual(self.ops.authenticate("dummy"), OWNER)
        with patch.object(self.api, "call", return_value={"status": {"authenticated": True, "user": {"username": OWNER}}}):
            self.assertError(401, lambda: self.ops.authenticate("dummy"))
        self.api.user = "system:serviceaccount:target-dev:foreign"
        self.assertError(403, lambda: self.ops.authenticate("dummy"))

    def test_missing_or_invalid_token_is_not_anonymous_access(self):
        self.assertError(401, lambda: self.ops.authenticate(""))
        with patch.object(self.api, "call", return_value={"status": {"authenticated": False}}):
            self.assertError(401, lambda: self.ops.authenticate("dummy"))

    def test_response_loss_repeat_returns_same_run_without_another_write(self):
        code, first = self.ops.submit(OWNER, self.key, self.body)
        code2, repeat = self.ops.submit(OWNER, self.key, self.body)
        self.assertEqual((code, code2), (202, 200))
        self.assertEqual(first["run_id"], repeat["run_id"])
        self.assertEqual(self.api.saved, 1)
        self.assertTrue(repeat["existing_request"])

    def test_same_key_changed_action_is_conflict(self):
        self.ops.submit(OWNER, self.key, self.body)
        self.assertError(409, lambda: self.ops.submit(OWNER, self.key, {"action": "close"}))
        self.assertEqual(self.api.saved, 1)

    def test_in_flight_different_request_is_conflict(self):
        self.ops.submit(OWNER, self.key, self.body)
        self.assertError(409, lambda: self.ops.submit(OTHER, self.key, self.body))
        self.assertEqual(self.api.saved, 1)

    def test_finished_repeat_reads_retained_receipt_even_after_key_age(self):
        _, first = self.ops.submit(OWNER, self.key, self.body)
        self.api.value["history"] = [{**first, "phase": "FINISHED", "verdict": "PASS"}]
        self.api.value.pop("request")
        self.api.value["phase"] = "FINISHED"
        with patch.object(server.time, "time", return_value=time.time() + 900):
            code, result = self.ops.submit(OWNER, self.key, self.body)
        self.assertEqual(code, 200)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(self.api.saved, 1)

    def test_evicted_old_key_can_never_create_a_new_execution(self):
        old = str(int(time.time()) - 600) + "-" + "b" * 32
        self.assertError(410, lambda: self.ops.submit(OWNER, old, self.body))
        self.assertEqual(self.api.saved, 0)

    def test_spoofed_owner_unknown_arguments_and_unacknowledged_open_are_rejected(self):
        for body in [{"action": "check", "owner": OTHER}, {"action": "cycle"}, {"action": "close", "hold_seconds": True}, {"action": "shell"}]:
            self.assertError(400, lambda body=body: self.ops.submit(OWNER, self.key, body))
        self.assertEqual(self.api.saved, 0)

    def test_other_owner_cannot_read_cancel_or_list_a_run(self):
        _, result = self.ops.submit(OWNER, self.key, self.body)
        self.assertError(404, lambda: self.ops.get(OTHER, result["run_id"]))
        self.assertError(404, lambda: self.ops.cancel(OTHER, result["run_id"]))
        self.assertEqual(self.ops.list(OTHER)["runs"], [])
        self.assertEqual(self.api.saved, 1)

    def test_cancel_marks_same_request_never_submits_close_as_second_run(self):
        _, result = self.ops.submit(OWNER, self.key, self.body)
        cancelled = self.ops.cancel(OWNER, result["run_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(self.api.value["request"]["run_id"], result["run_id"])
        self.assertEqual(self.api.value["history"], [])

    def test_explicit_open_persists_desired_state_and_rejects_cycle_until_close(self):
        _, opened = self.ops.submit(OWNER, self.key, {"action": "open", "acknowledge_anonymous_read": True})
        self.assertEqual(self.api.value["desired_state"], "open")
        self.api.value["phase"] = "FINISHED"
        next_key = self.key[:-1] + "b"
        self.assertError(409, lambda: self.ops.submit(OWNER, next_key, {"action": "cycle", "acknowledge_anonymous_read": True}))
        self.ops.submit(OWNER, next_key, {"action": "close"})
        self.assertEqual(self.api.value["desired_state"], "closed")
        self.assertNotEqual(self.api.value["request"]["run_id"], opened["run_id"])

    def test_cancel_pending_request_restores_previous_open_work_mode(self):
        self.api.value["desired_state"] = "open"
        _, result = self.ops.submit(OWNER, self.key, {"action": "close"})
        self.assertEqual(self.api.value["desired_state"], "closed")
        self.ops.cancel(OWNER, result["run_id"])
        self.assertEqual(self.api.value["desired_state"], "open")

    def test_state_is_timestamped_observation_not_an_unperformed_live_probe(self):
        self.api.value.update(desired_state="open", phase="FINISHED", report={"final_state": "open", "security_state": "PUBLIC_READ", "finished_at": "observed-at"})
        result = self.ops.status()
        self.assertEqual(result["last_observed_state"], "open")
        self.assertEqual(result["observed_at"], "observed-at")
        self.assertEqual(result["security_state"], "PUBLIC_READ")
        self.assertFalse(result["operation_in_progress"])

    def test_status_retains_last_observation_while_another_request_is_pending(self):
        self.api.value.update(desired_state="open", phase="PENDING", report={}, history=[{"final_state": "open", "security_state": "PUBLIC_READ", "finished_at": "prior-observation"}])
        result = self.ops.status()
        self.assertEqual(result["last_observed_state"], "open")
        self.assertEqual(result["observed_at"], "prior-observation")
        self.assertTrue(result["operation_in_progress"])

    def test_changed_cluster_refuses_even_reads(self):
        with patch.object(self.api, "call", return_value={"metadata": {"uid": "different"}}):
            self.assertError(503, lambda: self.ops.list(OWNER))

    def test_compare_and_swap_conflict_does_not_overwrite_other_request(self):
        with patch.object(self.api, "save", side_effect=Failure("kubernetes_409")), self.assertRaises(Failure):
            self.ops.submit(OWNER, self.key, self.body)
        self.assertEqual(self.api.value["phase"], "IDLE")


if __name__ == "__main__":
    unittest.main()
