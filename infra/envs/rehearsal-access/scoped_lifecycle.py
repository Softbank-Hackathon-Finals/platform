"""Trusted host composition: provision namespaces; give the runner only attempt access.

No daemon, credentials, namespace adoption or new namespace lifecycle implementation.
The host must authenticate execution authority before invoking its attempt factory.
"""

import json
import re
from dataclasses import dataclass, replace

RUNNER_NAMESPACE = "bravo-rehearsal-control"
RUNNER = "t15-runner"


EXEC_ACCOUNT = "rehearsal-exec"


def attempt_grants(namespace, *, exec_enabled=False):
    """Render grants owned by the already verified namespace UID.

    With T42 exec on, the runner may also act as this namespace's own `rehearsal-exec`
    account (templates/10-serviceaccount, 20-rbac) for inspect/probe. Nothing else is added:
    the account's Role stays namespace-scoped without Secret reads or RBAC writes.
    """
    if (re.fullmatch(r"rehearsal-[a-f0-9]{24}", namespace.name) is None
            or not isinstance(namespace.uid, str) or not namespace.uid.strip()):
        raise ValueError("verified rehearsal namespace required")
    owner = [{"apiVersion": "v1", "kind": "Namespace", "name": namespace.name,
              "uid": namespace.uid}]
    subject = [{"kind": "ServiceAccount", "name": RUNNER, "namespace": RUNNER_NAMESPACE}]
    name = "t15-runner-attempt"
    global_name = "t15-read-" + namespace.name

    def obj(kind, name, namespaced=True, **fields):
        return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": kind,
                "metadata": {"name": name, "ownerReferences": owner,
                             **({"namespace": namespace.name} if namespaced else {})}, **fields}

    rules = [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "create"]},
        {"apiGroups": [""], "resources": ["services", "configmaps", "secrets", "serviceaccounts"],
         "verbs": ["get", "create"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        {"apiGroups": ["batch"], "resources": ["jobs"],
         "verbs": ["get", "list", "create"]},
        {"apiGroups": [""], "resources": ["serviceaccounts"],
         "resourceNames": ["rehearsal-intruder"], "verbs": ["impersonate"]},
    ]
    if exec_enabled is True:
        rules.append({"apiGroups": [""], "resources": ["serviceaccounts"],
                      "resourceNames": [EXEC_ACCOUNT], "verbs": ["impersonate"]})
    return {"apiVersion": "v1", "kind": "List", "items": [
        obj("Role", name, rules=rules),
        obj("RoleBinding", name, roleRef={"apiGroup": "rbac.authorization.k8s.io",
                                          "kind": "Role", "name": name}, subjects=subject),
        obj("ClusterRole", global_name, False, rules=[
            {"apiGroups": [""], "resources": ["namespaces"],
             "resourceNames": [namespace.name], "verbs": ["get"]}]),
        obj("ClusterRoleBinding", global_name, False,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                     "name": global_name}, subjects=subject),
    ]}


@dataclass(frozen=True)
class ScopedLifecycle:
    """An existing NamespaceLifecycle using a trusted, separately configured client."""

    inner: object
    exec_enabled: bool = False

    def create(self, run_id):
        namespace = self.inner.create(run_id)
        try:
            # Reuse T15's UID/run/attempt validator instead of inventing another identity.
            self.inner._require_uid(namespace, run_id)
            grants = attempt_grants(namespace, exec_enabled=self.exec_enabled)
            result = self.inner.kube.run("create", "-f", "-", stdin=json.dumps(grants))
            if result.returncode:
                raise RuntimeError("attempt grants not confirmed")
            for expected in grants["items"]:
                args = [expected["kind"], expected["metadata"]["name"]]
                if "namespace" in expected["metadata"]:
                    args += ["-n", namespace.name]
                actual = self.inner.kube.get_json(*args)
                if (actual.get("metadata", {}).get("ownerReferences") != expected["metadata"]["ownerReferences"]
                        or any(actual.get(key) != value for key, value in expected.items()
                               if key not in {"apiVersion", "kind", "metadata"})):
                    raise RuntimeError("attempt grant changed")
            self.inner._require_uid(namespace, run_id)
        except Exception:
            # No retry/adoption after an ambiguous create. Cleanup uses the original UID.
            self.inner.cleanup(namespace)
            raise
        return namespace

    def cleanup(self, namespace):
        return self.inner.cleanup(namespace)


def scope_attempt(ports, *, provisioner, cleaner, exec_enabled=False):
    """Compose trusted lifecycle/T3 and scoped Job clients at the host boundary.

    Call on original factory output before any remote side effects. Provisioner
    credentials stay in the trusted host; never mount them into model or Job pods.
    Existing lifecycle wrappers must be composed explicitly, not silently discarded.
    """
    from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle
    from rehearsal_runner.runner import T3Gate

    if not isinstance(ports.lifecycle, NamespaceLifecycle) or not isinstance(ports.isolation, T3Gate):
        raise TypeError("unwrapped NamespaceLifecycle and T3Gate required")
    lifecycle = replace(ports.lifecycle, kube=provisioner, cleaner=cleaner)
    scoped = replace(ports, lifecycle=ScopedLifecycle(lifecycle),
                     isolation=replace(ports.isolation, kube=provisioner))
    phase = getattr(ports, "exec_phase", None)
    if phase is not None:
        # Space A gets the same scoped lifecycle; only it grants exec impersonation.
        phase_lifecycle = replace(phase.lifecycle, kube=provisioner, cleaner=cleaner)
        scoped = replace(scoped, exec_phase=replace(
            phase, lifecycle=ScopedLifecycle(phase_lifecycle, exec_enabled=exec_enabled),
            isolation=replace(phase.isolation, kube=provisioner)))
    return scoped


@dataclass(frozen=True)
class ScopedPlanner:
    """Wrap the credential planner without changing T15's core or Job clients.

    The separate management client creates only lifecycle/T3 resources. This
    composition does not grant that client to proposer, verifier or model code.
    Host admission/current authority must be verified before calling the factory.
    """

    inner: object
    provisioner: object
    cleaner: object

    def binding(self, admission):
        return self.inner.binding(admission)

    def factory(self, admission, task):
        return ScopedFactory(self.inner.factory(admission, task), self.provisioner, self.cleaner)


@dataclass(frozen=True)
class ScopedFactory:
    inner: object
    provisioner: object
    cleaner: object

    def prepare(self, attempt, feedback_ref):
        return scope_attempt(self.inner.prepare(attempt, feedback_ref),
                             provisioner=self.provisioner, cleaner=self.cleaner)
