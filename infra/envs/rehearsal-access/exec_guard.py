"""Render the admission boundary that makes `rehearsal-exec` unable to read Secrets.

The T3 Role (services/rehearsal-runner/templates/20-rbac.yaml) grants no Secret verbs, but
`pods`/`deployments`/`statefulsets`/`jobs` create would still let that identity mount a Secret
or exec into an app Pod that has one. With T42 exec this identity acts for AI tool calls, so
the boundary is enforced here, cluster-wide but only in rehearsal namespaces and only for
requests made as the namespace's own `rehearsal-exec` account. No RBAC is granted.

    python3 infra/envs/rehearsal-access/exec_guard.py > /private/exec-guard.json
"""

import json

NAME = "rehearsal-exec-no-secrets"
EXEC_NAME = "rehearsal-exec-tool-only"
TOOL_NAME = "rehearsal-exec-tool-runner-owned"
TOOL_POD = "rehearsal-exec-tool"  # rehearsal_runner.exec_actions.TOOL_POD
SELECTOR = {"matchLabels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}}  # templates/00-namespace.yaml
ACTOR = "request.userInfo.username == 'system:serviceaccount:' + request.namespace + ':rehearsal-exec'"


def _binding(name):
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name}, "spec": {"policyName": name, "validationActions": ["Deny"]}}


def render():
    workloads = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": NAME},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"namespaceSelector": SELECTOR, "resourceRules": [
                {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                 "resources": ["pods"], "scope": "Namespaced"},
                {"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                 "resources": ["deployments", "statefulsets"], "scope": "Namespaced"},
                {"apiGroups": ["batch"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                 "resources": ["jobs"], "scope": "Namespaced"},
            ]},
            # Only the exec identity is constrained; the runner's own templates are untouched.
            "matchConditions": [{"name": "exec-identity", "expression": ACTOR}],
            "variables": [
                {"name": "spec", "expression":
                    "request.resource.resource == 'pods' ? object.spec : object.spec.template.spec"},
                {"name": "containers", "expression":
                    "has(variables.spec.initContainers) ? "
                    "variables.spec.containers + variables.spec.initContainers : variables.spec.containers"},
            ],
            "validations": [
                # Allow-list, not deny-list: a new volume kind (CSI inline, ephemeral, ...) is refused.
                {"expression": "has(variables.spec.volumes) ? variables.spec.volumes.all(v, "
                               "has(v.configMap) || has(v.emptyDir) || has(v.persistentVolumeClaim) || "
                               "has(v.downwardAPI) || (has(v.projected) ? v.projected.sources.all(s, "
                               "has(s.configMap) || has(s.downwardAPI)) : false)) : true",
                 "message": "rehearsal-exec may use only configMap, emptyDir, PVC and downwardAPI volumes",
                 "reason": "Forbidden"},
                {"expression": "variables.containers.all(c, "
                               "(has(c.env) ? c.env.all(e, has(e.valueFrom) ? !has(e.valueFrom.secretKeyRef) : true) "
                               ": true) && (has(c.envFrom) ? c.envFrom.all(f, !has(f.secretRef)) : true))",
                 "message": "rehearsal-exec may not read a Secret into the environment", "reason": "Forbidden"},
                # Explicit, never defaulted: an omitted field inherits the account's automount.
                {"expression": "has(variables.spec.serviceAccountName) && "
                               "variables.spec.serviceAccountName in ['default', 'rehearsal-exec'] && "
                               "has(variables.spec.automountServiceAccountToken) && "
                               "variables.spec.automountServiceAccountToken == false",
                 "message": "rehearsal-exec workloads name default/rehearsal-exec and set "
                            "automountServiceAccountToken: false", "reason": "Forbidden"},
                {"expression": f"request.resource.resource != 'pods' || !has(object.metadata.name) || "
                               f"object.metadata.name != {json.dumps(TOOL_POD)}",
                 "message": "only the runner creates the tool Pod", "reason": "Forbidden"},
            ],
        },
    }
    tool_pod = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": TOOL_NAME},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"namespaceSelector": SELECTOR, "resourceRules": [
                {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["DELETE"],
                 "resources": ["pods"], "scope": "Namespaced"}]},
            "matchConditions": [{"name": "exec-identity", "expression": ACTOR}],
            # Exec is judged by name, so the name must stay the runner's: no delete-and-recreate.
            "validations": [{"expression": f"request.name != {json.dumps(TOOL_POD)}",
                             "message": "only the runner removes the tool Pod", "reason": "Forbidden"}],
        },
    }
    exec_only = {
        "apiVersion": "admissionregistration.k8s.io/v1",
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": EXEC_NAME},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"namespaceSelector": SELECTOR, "resourceRules": [
                {"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CONNECT"],
                 "resources": ["pods/exec", "pods/attach", "pods/portforward"], "scope": "Namespaced"}]},
            "matchConditions": [{"name": "exec-identity", "expression": ACTOR}],
            "validations": [{
                "expression": f"request.subResource == 'exec' && request.name == {json.dumps(TOOL_POD)}",
                "message": "rehearsal-exec may exec only into the tokenless tool Pod",
                "reason": "Forbidden"}],
        },
    }
    return {"apiVersion": "v1", "kind": "List",
            "items": [workloads, _binding(NAME), tool_pod, _binding(TOOL_NAME), exec_only, _binding(EXEC_NAME)]}


if __name__ == "__main__":
    print(json.dumps(render(), indent=2))
