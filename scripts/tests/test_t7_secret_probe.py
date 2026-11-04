"""A 403 is a credential denial only if the API attributes this GET to the intended Pod."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "infra/envs/platform-data"))
from secret_probe import assess, run


def example(statuses=(200, 403, 200)):
    ns, fixture = "t7-secret-probe-0123456789ab", str(uuid4())
    accounts = {name: str(uuid4()) for name in ("reader", "denied")}
    receipt = {"namespace": ns, "fixture_uid": fixture, "cleanup_confirmed": True, "steps": [],
               "service_accounts": accounts}
    events = []
    for role, status in zip(("reader", "denied", "reader"), statuses, strict=True):
        pod, request = str(uuid4()), str(uuid4())
        receipt["steps"].append({"pod_uid": pod, "audit_id": request, "status": status,
                                 **({"uid": fixture} if status == 200 else {})})
        events.append({"auditID": request, "stage": "ResponseComplete", "verb": "get",
                       "user": {"username": f"system:serviceaccount:{ns}:{role}", "uid": accounts[role],
                                "extra": {"authentication.kubernetes.io/pod-uid": [pod]}},
                       "objectRef": {"namespace": ns, "resource": "secrets", "name": "t7-synthetic"},
                       "responseStatus": {"code": status},
                       "annotations": {"authorization.k8s.io/decision": "allow" if status == 200 else "forbid"}})
    return receipt, events


class SecretProbeTest(unittest.TestCase):
    def test_403_from_other_identity_object_or_authorizer_is_unknown(self):
        receipt, events = example()
        self.assertEqual(assess.classify(receipt, events), "PASS")
        for bad in ({"user": events[0]["user"]}, {"impersonatedUser": events[1]["user"]}, {"annotations": {}},
                    {"objectRef": {**events[1]["objectRef"], "subresource": "status"}},
                    {"user": {**events[1]["user"], "extra": events[0]["user"]["extra"]}}):
            with self.subTest(bad=bad):
                self.assertEqual(assess.classify(receipt, [events[0], {**events[1], **bad}, events[2]]), "UNKNOWN")

    def test_deny_account_real_read_fails_and_broken_controls_are_unknown(self):
        self.assertEqual(assess.classify(*example((200, 200, 200))), "FAIL")
        receipt, events = example()
        for sample in (events[:2], [*events, events[1]]):
            self.assertEqual(assess.classify(receipt, sample), "UNKNOWN")
        changed = copy.deepcopy(receipt)
        changed["steps"][-1]["uid"] = str(uuid4())
        self.assertEqual(assess.classify(changed, events), "UNKNOWN")
        self.assertEqual(assess.classify({**receipt, "cleanup_confirmed": False}, events), "UNKNOWN")

    def test_replaced_object_is_preserved_and_other_owned_objects_are_still_removed(self):
        probe = object.__new__(run.Probe)
        probe.ns = "t7-secret-probe-0123456789ab"
        probe.check_ns = Mock()
        probe.save = Mock()
        probe.state = {"owned": [{"kind": "ServiceAccount", "name": "reader", "uid": "original-reader"},
                                 {"kind": "Secret", "name": "t7-synthetic", "uid": "original-secret"}]}
        probe.kube = Mock(side_effect=["replaced-secret", "original-reader", "{}", "deleted", ""])
        with self.assertRaisesRegex(RuntimeError, "cleanup_unconfirmed"):
            probe.cleanup()
        deletes = [(args, kwargs) for args, kwargs in probe.kube.call_args_list if args[0] == "delete"]
        self.assertEqual(len(deletes), 1)
        args, kwargs = deletes[0]
        self.assertTrue(args[2].endswith("/serviceaccounts/reader"))
        self.assertEqual(kwargs["obj"]["preconditions"], {"uid": "original-reader"})
        self.assertFalse(probe.state["cleanup_confirmed"])

    def test_foreign_api_status_or_changed_audit_id_is_unknown(self):
        receipt, events = example()
        events[1]["responseStatus"]["code"] = 404
        self.assertEqual(assess.classify(receipt, events), "UNKNOWN")
        events[1]["responseStatus"]["code"] = 403
        events[1]["auditID"] = str(uuid4())
        self.assertEqual(assess.classify(receipt, events), "UNKNOWN")
