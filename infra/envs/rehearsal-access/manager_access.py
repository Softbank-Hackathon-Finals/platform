"""T15 namespace manager: RBAC plus the admission boundary that confines its writes.

The credential host gives this account to ScopedPlanner as provisioner/cleaner.
It creates random `rehearsal-<24 hex>` namespaces, applies the T3 templates and
the runner's per-attempt grants inside them, and deletes them by UID. Rehearsal
names are unknown in advance, so RBAC alone would reach every namespace; the
policy denies any write outside the registered rehearsal shape. Tokens come from
the existing T7 enrollment (credential_install.render) for this account.
"""

import json
import sys

NAMESPACE = "bravo-rehearsal-control"
MANAGER = "t15-manager"
RUNNER = "t15-runner"
NAME = "bravo-t15-manager"
PATTERN = "^rehearsal-[a-f0-9]{24}$"
PART_OF = "bravo-rehearsal"
# The T3 templates and scoped_lifecycle.attempt_grants are the only objects written.
NAMESPACED = {"": ["serviceaccounts", "resourcequotas", "limitranges"],
              "networking.k8s.io": ["networkpolicies"],
              "rbac.authorization.k8s.io": ["roles", "rolebindings"]}


def render():
    labels = {"app.kubernetes.io/part-of": "bravo-target-access"}
    user = f"system:serviceaccount:{NAMESPACE}:{MANAGER}"
    q = json.dumps

    def obj(api, kind, name=NAME, **body):
        return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "labels": labels}, **body}

    rules = [
        # get covers the kube-system UID check and the UID-precondition delete loop.
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["get", "create", "delete"]},
        # Owner references to the namespace remove these with it; no delete needed.
        {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["clusterroles", "clusterrolebindings"],
         "verbs": ["get", "create"]},
        # escalate/bind let it write the T3 and attempt Roles; the policy keeps them in rehearsal namespaces.
        {"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["roles"],
         "verbs": ["get", "create", "patch", "escalate", "bind"]},
        *({"apiGroups": [group], "resources": [r for r in resources if r != "roles"],
           "verbs": ["get", "create", "patch"]} for group, resources in NAMESPACED.items()
          if [r for r in resources if r != "roles"]),
    ]
    def label(target, key, value):
        return (f"has({target}.metadata.labels) && {q(key)} in {target}.metadata.labels && "
                f"{target}.metadata.labels[{q(key)}] == {q(value)}")

    def rule(group, names):
        return {"apiGroups": [group], "apiVersions": ["v1"], "operations": ["*"], "resources": names}

    subject = {"name": "subject", "expression": "request.operation == 'DELETE' ? oldObject : object"}
    read_target = ("variables.subject.metadata.name.startsWith('t15-read-') && "
                   "variables.subject.metadata.name.substring(9).matches(" + q(PATTERN) + ")")
    # One policy per object shape keeps every expression type-checked against its own schema.
    policies = {
        NAME: ([rule(group, names) for group, names in NAMESPACED.items()] + [rule("", ["namespaces"])], [
            {"expression": "request.resource.resource != 'namespaces' || "
                           "(variables.subject.metadata.name.matches(" + q(PATTERN) + ") && "
                           + label("variables.subject", "app.kubernetes.io/part-of", PART_OF)
                           + " && (request.operation == 'DELETE' || "
                           + label("variables.subject", "pod-security.kubernetes.io/enforce", "restricted") + "))",
             "message": "manager namespaces must be restricted rehearsal namespaces"},
            # A Namespace request carries its own name as request.namespace; it is checked above.
            {"expression": "request.resource.resource == 'namespaces' || "
                           "(request.namespace.matches(" + q(PATTERN) + ") && namespaceObject != null && "
                           + label("namespaceObject", "app.kubernetes.io/part-of", PART_OF) + ")",
             "message": "manager writes only inside rehearsal namespaces"}]),
        NAME + "-clusterroles": ([rule("rbac.authorization.k8s.io", ["clusterroles"])], [
            {"expression": read_target + " && (request.operation == 'DELETE' || (size(object.rules) == 1 && "
                           "object.rules[0].apiGroups == [''] && object.rules[0].resources == ['namespaces'] && "
                           "object.rules[0].verbs == ['get'] && "
                           "object.rules[0].resourceNames == [object.metadata.name.substring(9)]))",
             "message": "manager cluster roles only read their own rehearsal namespace"}]),
        NAME + "-clusterrolebindings": ([rule("rbac.authorization.k8s.io", ["clusterrolebindings"])], [
            {"expression": read_target + " && (request.operation == 'DELETE' || ("
                           "object.roleRef.kind == 'ClusterRole' && object.roleRef.name == object.metadata.name && "
                           "size(object.subjects) == 1 && object.subjects[0].kind == 'ServiceAccount' && "
                           "object.subjects[0].name == " + q(RUNNER) + " && "
                           "object.subjects[0].namespace == " + q(NAMESPACE) + "))",
             "message": "manager cluster bindings only grant the runner its namespace read"}]),
    }
    boundary = []
    for name, (resources, validations) in policies.items():
        boundary += [
            obj("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy", name, spec={
                "failurePolicy": "Fail",
                "matchConstraints": {"resourceRules": resources},
                "matchConditions": [{"name": "manager", "expression": "request.userInfo.username == " + q(user)}],
                "variables": [subject], "validations": validations}),
            obj("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding", name,
                spec={"policyName": name, "validationActions": ["Deny"]})]
    # Install the denial boundary before the account holds any permission.
    return {"apiVersion": "v1", "kind": "List", "items": boundary + [
        {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
         "metadata": {"name": MANAGER, "namespace": NAMESPACE, "labels": labels}},
        obj("rbac.authorization.k8s.io/v1", "ClusterRole", rules=rules),
        obj("rbac.authorization.k8s.io/v1", "ClusterRoleBinding",
            subjects=[{"kind": "ServiceAccount", "name": MANAGER, "namespace": NAMESPACE}],
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": NAME}),
    ]}

if __name__ == "__main__":
    if sys.argv[1:] != ["render"]:
        raise SystemExit("usage: manager_access.py render")
    print(json.dumps(render(), indent=2))
