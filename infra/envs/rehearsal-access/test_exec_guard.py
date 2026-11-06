# /// script
# dependencies = ["cel-python==0.2.0"]
# ///
"""Evaluate the exec guard's own CEL against allowed and refused requests (no cluster)."""
import importlib.util
import unittest
from pathlib import Path

import celpy

spec = importlib.util.spec_from_file_location("exec_guard", Path(__file__).with_name("exec_guard.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
POLICIES = {item["metadata"]["name"]: item["spec"] for item in guard.render()["items"]
            if item["kind"] == "ValidatingAdmissionPolicy"}
ENV = celpy.Environment()


def run(expression, activation):
    return bool(ENV.program(ENV.compile(expression)).evaluate(
        {k: celpy.json_to_cel(v) for k, v in activation.items()}))


def admitted(policy, request, obj=None):
    """True when the request passes: not matched, or every validation holds."""
    spec = POLICIES[policy]
    activation = {"request": request, "object": obj or {}}
    if not all(run(c["expression"], activation) for c in spec["matchConditions"]):
        return True
    variables = {}

    def scope():
        return {"request": celpy.json_to_cel(request), "object": celpy.json_to_cel(activation["object"]),
                "variables": celpy.celtypes.MapType({celpy.celtypes.StringType(k): v for k, v in variables.items()})}
    for variable in spec.get("variables", []):
        variables[variable["name"]] = ENV.program(ENV.compile(variable["expression"])).evaluate(scope())
    context = scope()
    return all(ENV.program(ENV.compile(v["expression"])).evaluate(context) for v in spec["validations"])


def request(user="rehearsal-exec", resource="pods", **extra):
    return {"userInfo": {"username": f"system:serviceaccount:rehearsal-a1:{user}"}, "namespace": "rehearsal-a1",
            "resource": {"group": "", "resource": resource}} | extra


SAFE = {"serviceAccountName": "default", "automountServiceAccountToken": False}


def pod(name="app", safe=True, **spec):
    base = {"containers": [{"name": "app", "image": "x"}]} | (SAFE if safe else {})
    return {"kind": "Pod", "metadata": {"name": name}, "spec": base | spec}


def deployment(**spec):
    return {"kind": "Deployment", "metadata": {"name": "app"}, "spec": {"template": pod(**spec)}}


SECRET_VOLUME = {"volumes": [{"name": "s", "secret": {"secretName": "app"}}]}
PROJECTED = {"volumes": [{"name": "p", "projected": {"sources": [{"secret": {"name": "app"}}]}}]}
ENV_REF = {"containers": [{"name": "app", "image": "x",
                           "env": [{"name": "P", "valueFrom": {"secretKeyRef": {"name": "app", "key": "p"}}}]}]}
ENV_FROM = {"containers": [{"name": "app", "image": "x", "envFrom": [{"secretRef": {"name": "app"}}]}]}
INIT_ENV = {"initContainers": ENV_REF["containers"]}
TOKEN = {"volumes": [{"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "t"}}]}}]}
CSI = {"volumes": [{"name": "c", "csi": {"driver": "secrets-store.csi.k8s.io"}}]}
EPHEMERAL = {"volumes": [{"name": "e", "ephemeral": {"volumeClaimTemplate": {"spec": {}}}}]}


class ExecGuard(unittest.TestCase):
    def test_exec_identity_cannot_reach_a_secret_through_a_workload(self):
        for spec in (SECRET_VOLUME, PROJECTED, ENV_REF, ENV_FROM, INIT_ENV, TOKEN, CSI, EPHEMERAL,
                     {"automountServiceAccountToken": True}):
            with self.subTest(spec=spec):
                self.assertFalse(admitted(guard.NAME, request(), pod(**spec)))
                self.assertFalse(admitted(guard.NAME, request(resource="deployments"), deployment(**spec)))

    def test_exec_identity_keeps_plain_workloads_and_configmaps(self):
        plain = {"volumes": [{"name": "c", "configMap": {"name": "cfg"}}, {"name": "e", "emptyDir": {}},
                             {"name": "d", "projected": {"sources": [{"configMap": {"name": "cfg"}}]}}]}
        self.assertTrue(admitted(guard.NAME, request(), pod(**plain)))
        self.assertTrue(admitted(guard.NAME, request(resource="deployments"), deployment()))
        self.assertTrue(admitted(guard.NAME, request(), pod(serviceAccountName="rehearsal-exec")))

    def test_token_must_be_explicitly_off_and_account_fixed(self):
        # The cases reproduced on a real API server in review (kind v1.36.1).
        self.assertFalse(admitted(guard.NAME, request(), pod(safe=False)))  # both fields omitted
        self.assertFalse(admitted(guard.NAME, request(), pod(safe=False, serviceAccountName="other",
                                                             automountServiceAccountToken=False)))
        self.assertFalse(admitted(guard.NAME, request(), pod(safe=False, serviceAccountName="default")))
        self.assertFalse(admitted(guard.NAME, request(), pod(serviceAccountName="other", **TOKEN)))

    def test_exec_identity_cannot_replace_the_tool_pod(self):
        self.assertFalse(admitted(guard.NAME, request(), pod(name="rehearsal-exec-tool")))
        self.assertFalse(admitted(guard.TOOL_NAME, request(name="rehearsal-exec-tool", operation="DELETE")))
        self.assertTrue(admitted(guard.TOOL_NAME, request(name="demo-chat-7f9c", operation="DELETE")))
        self.assertTrue(admitted(guard.TOOL_NAME, request(user="t15-runner", name="rehearsal-exec-tool")))

    def test_runner_and_other_identities_are_not_constrained(self):
        for user in ("t15-runner", "default"):
            self.assertTrue(admitted(guard.NAME, request(user=user), pod(**SECRET_VOLUME)))

    def test_exec_only_into_the_tool_pod(self):
        self.assertTrue(admitted(guard.EXEC_NAME, request(subResource="exec", name="rehearsal-exec-tool")))
        self.assertFalse(admitted(guard.EXEC_NAME, request(subResource="exec", name="demo-chat-7f9c")))
        self.assertFalse(admitted(guard.EXEC_NAME, request(subResource="attach", name="rehearsal-exec-tool")))
        self.assertFalse(admitted(guard.EXEC_NAME, request(subResource="portforward", name="rehearsal-exec-tool")))

    def test_scope_is_rehearsal_namespaces_and_deny_bindings(self):
        items = guard.render()["items"]
        for item in items:
            if item["kind"] == "ValidatingAdmissionPolicy":
                self.assertEqual(item["spec"]["matchConstraints"]["namespaceSelector"],
                                 {"matchLabels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}})
                self.assertEqual(item["spec"]["failurePolicy"], "Fail")
            else:
                self.assertEqual(item["spec"]["validationActions"], ["Deny"])


if __name__ == "__main__":
    unittest.main()
