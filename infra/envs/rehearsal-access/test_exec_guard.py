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


def pod(**spec):
    return {"kind": "Pod", "spec": {"containers": [{"name": "app", "image": "x"}]} | spec}


def deployment(**spec):
    return {"kind": "Deployment", "spec": {"template": pod(**spec)}}


SECRET_VOLUME = {"volumes": [{"name": "s", "secret": {"secretName": "app"}}]}
PROJECTED = {"volumes": [{"name": "p", "projected": {"sources": [{"secret": {"name": "app"}}]}}]}
ENV_REF = {"containers": [{"name": "app", "image": "x",
                           "env": [{"name": "P", "valueFrom": {"secretKeyRef": {"name": "app", "key": "p"}}}]}]}
ENV_FROM = {"containers": [{"name": "app", "image": "x", "envFrom": [{"secretRef": {"name": "app"}}]}]}
INIT_ENV = {"initContainers": ENV_REF["containers"]}


class ExecGuard(unittest.TestCase):
    def test_exec_identity_cannot_reach_a_secret_through_a_workload(self):
        for spec in (SECRET_VOLUME, PROJECTED, ENV_REF, ENV_FROM, INIT_ENV, {"automountServiceAccountToken": True}):
            with self.subTest(spec=spec):
                self.assertFalse(admitted(guard.NAME, request(), pod(**spec)))
                self.assertFalse(admitted(guard.NAME, request(resource="deployments"), deployment(**spec)))

    def test_exec_identity_keeps_plain_workloads_and_configmaps(self):
        plain = {"volumes": [{"name": "c", "configMap": {"name": "cfg"}}, {"name": "e", "emptyDir": {}}],
                 "automountServiceAccountToken": False}
        self.assertTrue(admitted(guard.NAME, request(), pod(**plain)))
        self.assertTrue(admitted(guard.NAME, request(resource="deployments"), deployment()))

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
