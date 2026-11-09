"""Credential probes must preserve foreign resources and clean their own UIDs."""

import base64
import copy
import importlib
import json
import sys
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
module = importlib.import_module("target_registry_probe")
REGISTRY = "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com"


def config():
    return {"probe_id": uuid.uuid4().hex, "namespace": "target-dev", "repository_url": REGISTRY + "/bravo/demo-chat",
            "digest": "sha256:" + "a" * 64, "platform_digest": "sha256:" + "b" * 64,
            "cluster_uid": str(uuid.uuid4()), "node_uid": str(uuid.uuid4()), "namespace_uid": str(uuid.uuid4()),
            "authorizations": [{"registry": REGISTRY,
                "authorization": base64.b64encode(("AWS:" + role * 30).encode()).decode(),
                "refresh_before": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
                for role in ("a", "b")]}


class Kube:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.lost_create_response = False
        self.replace_before_readback = False

    def get(self, kind, name=None, namespace="target-dev"):
        return copy.deepcopy(self.objects.get((kind, name)))

    def call(self, args, obj=None):
        self.calls.append((args, copy.deepcopy(obj)))
        if args[0] == "create":
            current = copy.deepcopy(obj)
            current["metadata"].update(uid=str(uuid.uuid4()), resourceVersion="1")
            key = ("secrets", current["metadata"]["name"])
            self.objects[key] = current
            if self.replace_before_readback:
                replacement = copy.deepcopy(current)
                replacement["metadata"]["uid"] = str(uuid.uuid4())
                self.objects[key] = replacement
            if self.lost_create_response:
                raise module.Failure("kubernetes_transport_unknown")
            return json.dumps(current)
        if args[0] == "replace":
            key = ("secrets", obj["metadata"]["name"])
            current = self.objects[key]
            if any(obj["metadata"][k] != current["metadata"][k] for k in ("uid", "resourceVersion")):
                raise module.Failure("kubernetes_operation_failed")
            replacement = copy.deepcopy(obj)
            replacement["metadata"]["resourceVersion"] = "2"
            self.objects[key] = replacement
            return json.dumps(replacement)
        if args[0] == "delete":
            pieces = args[2].split("/")
            key = (pieces[-2], pieces[-1])
            if obj["preconditions"]["uid"] != self.objects[key]["metadata"]["uid"]:
                raise module.Failure("kubernetes_operation_failed")
            del self.objects[key]
            return "{}"
        raise AssertionError("unexpected mutation")


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.kube = Kube()
        self.input = config()
        self.probe = module.Probe(self.input)

    def create_secret(self):
        return self.probe.create(self.probe.secret(self.input["authorizations"][0]), "secrets")

    def test_refresh_uses_resource_version_and_cleanup_uses_uid_precondition(self):
        with patch.object(module, "get", self.kube.get), patch.object(module, "call", self.kube.call):
            initial = self.create_secret()
            updated = self.probe.refresh(initial, self.input["authorizations"][1])
            self.assertEqual(updated["metadata"]["uid"], initial["metadata"]["uid"])
            self.assertNotEqual(updated["data"], initial["data"])
            self.assertTrue(self.probe.cleanup())
        self.assertFalse(self.kube.objects)
        replace = next(obj for args, obj in self.kube.calls if args[0] == "replace")
        self.assertEqual(replace["metadata"]["resourceVersion"], "1")
        deletion = next(obj for args, obj in self.kube.calls if args[0] == "delete")
        self.assertEqual(deletion["preconditions"], {"uid": initial["metadata"]["uid"]})

    def test_replacement_between_create_and_first_get_is_never_adopted(self):
        self.kube.replace_before_readback = True
        with patch.object(module, "get", self.kube.get), patch.object(module, "call", self.kube.call):
            with self.assertRaises(module.Failure):
                self.create_secret()
            self.assertFalse(self.probe.cleanup())
        self.assertEqual(len(self.kube.objects), 1)
        self.assertFalse(any(args[0] == "delete" for args, _ in self.kube.calls))

    def test_lost_create_response_never_authorizes_delete_by_name_and_label(self):
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                self.setUp()
                self.kube.lost_create_response = True
                self.kube.replace_before_readback = replaced
                with patch.object(module, "get", self.kube.get), patch.object(module, "call", self.kube.call):
                    with self.assertRaises(module.Failure):
                        self.create_secret()
                    self.assertFalse(self.probe.cleanup())
                self.assertIsNone(self.probe.owned[0]["uid"])
                self.assertEqual(len(self.kube.objects), 1)
                self.assertFalse(any(args[0] == "delete" for args, _ in self.kube.calls))

    def test_replaced_or_relabeled_resource_is_never_deleted(self):
        for field in ("uid", "labels"):
            with self.subTest(field=field):
                self.setUp()
                with patch.object(module, "get", self.kube.get), patch.object(module, "call", self.kube.call):
                    initial = self.create_secret()
                    item = self.kube.objects[("secrets", initial["metadata"]["name"])]
                    item["metadata"][field] = str(uuid.uuid4()) if field == "uid" else {}
                    self.assertFalse(self.probe.cleanup())
                self.assertFalse(any(args[0] == "delete" for args, _ in self.kube.calls))

    def test_wrong_uid_blocks_secret_refresh_before_write(self):
        with patch.object(module, "get", self.kube.get), patch.object(module, "call", self.kube.call):
            initial = self.create_secret()
            self.kube.objects[("secrets", initial["metadata"]["name"])]["metadata"]["uid"] = str(uuid.uuid4())
            with self.assertRaisesRegex(module.Failure, "secret_identity_changed"):
                self.probe.refresh(initial, self.input["authorizations"][1])
        self.assertFalse(any(args[0] == "replace" for args, _ in self.kube.calls))

    def test_invalid_identity_or_expiring_authorization_is_rejected(self):
        for field, value in (("namespace", "default"), ("cluster_uid", "bad"), ("node_uid", "bad")):
            with self.subTest(field=field), self.assertRaises(module.Failure):
                module.validate({**self.input, field: value})
        self.input["authorizations"][0]["refresh_before"] = datetime.now(UTC).isoformat()
        with self.assertRaises(module.Failure):
            module.validate(self.input)

    def test_the_reviewed_metadata_policy_is_the_one_the_nodes_get(self):
        import hashlib
        policy = Path(__file__).resolve().parents[2] / "services/control-runner/templates/rke2-audit-policy.yaml"
        self.assertEqual(module.METADATA_POLICY_SHA256, hashlib.sha256(policy.read_bytes()).hexdigest())

    def test_preflight_identity_failure_precedes_any_write(self):
        wrong = {"metadata": {"uid": str(uuid.uuid4())}}
        with patch.object(module, "get", return_value=wrong), patch.object(module, "call") as call:
            result = module.run(self.input)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["error_category"], "cluster_identity_mismatch")
        call.assert_not_called()

    def test_failed_final_control_or_cleanup_cannot_pass(self):
        for fail_control in (False, True):
            with self.subTest(fail_control=fail_control):
                controls = [{"authenticated": True}, {"unauthorized_observed": True},
                            module.Failure("probe_timeout_unknown") if fail_control else {"authenticated": True}]
                with patch.object(module, "preflight", return_value={"metadata_audit_verified": False}), \
                        patch.object(module, "get", return_value={"items": []}), \
                        patch.object(module.Probe, "create", return_value={}), \
                        patch.object(module.Probe, "refresh", return_value={}), \
                        patch.object(module.Probe, "pod", side_effect=controls), \
                        patch.object(module.Probe, "cleanup", return_value=fail_control):
                    result = module.run(self.input)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["cleanup_verified"], fail_control)

    def test_anonymous_timeout_is_not_permission_denial(self):
        for message, denied in (("unexpected status: 401 Unauthorized", True),
                                ("no basic auth credentials", True), ("connection timeout", False)):
            with self.subTest(message=message):
                observed = {}

                def create(obj, resource, observed=observed, message=message):
                    observed.update(copy.deepcopy(obj))
                    observed["metadata"]["uid"] = str(uuid.uuid4())
                    observed["status"] = {"phase": "Pending", "containerStatuses": [{
                        "state": {"waiting": {"reason": "ErrImagePull", "message": message}}}]}
                    return copy.deepcopy(observed)

                with patch.object(self.probe, "create", side_effect=create), \
                        patch.object(module, "get", side_effect=lambda *a, observed=observed, **kw: copy.deepcopy(observed)):
                    if denied:
                        self.assertTrue(self.probe.pod("anonymous", False)["unauthorized_observed"])
                    else:
                        with self.assertRaisesRegex(module.Failure, "anonymous_failure_not_permission_proof"):
                            self.probe.pod("anonymous", False)

    def run_admitted_pod(self, mutate, *, after_create=False):
        observed = {}
        pod_uid = str(uuid.uuid4())

        def create(obj, resource):
            observed.update(copy.deepcopy(obj))
            observed["metadata"]["uid"] = pod_uid
            observed["spec"].update(dnsPolicy="ClusterFirst", schedulerName="default-scheduler",
                                    serviceAccountName="default")
            observed["spec"]["containers"][0].update(
                terminationMessagePolicy="File", terminationMessagePath="/dev/termination-log")
            observed["status"] = {"phase": "Succeeded", "containerStatuses": [{
                "imageID": self.input["digest"]}]}
            if not after_create:
                mutate(observed)
            return copy.deepcopy(observed)

        def read(*_):
            if after_create:
                mutate(observed)
            return copy.deepcopy(observed)

        def call(args):
            if args[0] == "logs":
                return '{"uid": 10001}'
            self.assertIn("involvedObject.uid=" + pod_uid, args)
            return '{"items": [{"reason":"Pulled","message":"Successfully pulled image"}]}'

        with patch.object(self.probe, "create", side_effect=create), patch.object(module, "get", side_effect=read), \
                patch.object(module, "call", side_effect=call):
            return self.probe.pod("before", True)

    def test_normal_kubernetes_defaults_preserve_probe_contract(self):
        self.assertTrue(self.run_admitted_pod(lambda pod: None)["pull_event"])

    def test_forged_uid_log_cannot_mask_admission_changes(self):
        token = {"name": "api", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}}
        mutations = {
            "command": lambda p: p["spec"]["containers"][0].update(command=["echo", '{"uid":10001}']),
            "args": lambda p: p["spec"]["containers"][0].update(args=["different"]),
            "root": lambda p: p["spec"]["securityContext"].update(runAsUser=0, runAsNonRoot=False),
            "container privilege": lambda p: p["spec"]["containers"][0]["securityContext"].update(privileged=True),
            "env": lambda p: p["spec"]["containers"][0].update(env=[{"name": "PYTHONHOME", "value": "/tmp"}]),
            "token": lambda p: p["spec"].update(volumes=[token]),
            "mount": lambda p: p["spec"]["containers"][0].update(volumeMounts=[{"name": "api", "mountPath": "/api"}]),
            "init": lambda p: p["spec"].update(initContainers=[{"name": "init"}]),
            "ephemeral": lambda p: p["spec"].update(ephemeralContainers=[{"name": "debug"}]),
            "owner": lambda p: p["metadata"]["labels"].clear(),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), self.assertRaises(module.Failure):
                self.run_admitted_pod(mutate)

    def test_late_ephemeral_container_cannot_pass_from_initial_admission_snapshot(self):
        with self.assertRaises(module.Failure):
            self.run_admitted_pod(lambda p: p["spec"].update(ephemeralContainers=[{"name": "late"}]), after_create=True)

    def test_provider_failure_does_not_emit_secret_or_raw_error(self):
        secret = "synthetic-credential-in-provider-error"
        with patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=secret, stderr=secret)), \
                self.assertRaisesRegex(module.Failure, "^kubernetes_operation_failed$"):
            module.call(["create", "-f", "-"], {"data": secret})


if __name__ == "__main__":
    unittest.main()
