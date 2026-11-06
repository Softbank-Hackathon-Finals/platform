"""Installation objects for one existing workload identity. No secret material."""

import base64
import json
from urllib.parse import urlsplit


def refresh_issuer_route(registration, issuer, service, egress_proxy, config, *, apply_patch):
    """Resync an installed v1.104.4 egress route after the issuer becomes ready.

    Unresolved Service FQDNs are not retried on netmap changes in this version.
    A dedicated ConfigMap data key triggers its normal file watcher; existing
    rules, Pod UIDs and connections remain intact. Repeat calls are no-ops.
    """
    r = registration
    group = egress_proxy["metadata"]
    metadata = config["metadata"]
    conditions = {c["type"]: c for c in issuer.get("status", {}).get("conditions", [])}
    ready = conditions.get("KubeAPIServerProxyConfigured", {})
    annotations = service["metadata"].get("annotations", {})
    owners = metadata.get("ownerReferences", [])
    if (issuer["metadata"]["name"] != r.name + "-issuer"
            or issuer.get("status", {}).get("url") != "https://" + r.issuer.tls_server_name
            or ready.get("status") != "True"
            or not issuer["metadata"].get("generation")
            or ready.get("observedGeneration") != issuer["metadata"].get("generation")
            or annotations.get("tailscale.com/tailnet-fqdn") != r.issuer.tls_server_name
            or annotations.get("tailscale.com/proxy-group") != group["name"]
            or egress_proxy["spec"].get("type") != "egress"
            or r.issuer_group not in egress_proxy["spec"].get("tags", [])
            or metadata["name"] != group["name"] + "-egress-config"
            or not any(o.get("uid") == group["uid"] and o.get("kind") == "ProxyGroup"
                       and o.get("controller") is True for o in owners)):
        raise ValueError("ready registered issuer and owned egress configuration required")
    key = r.name + "-route"
    value = json.dumps([issuer["metadata"]["uid"], service["metadata"]["uid"],
                        ready["lastTransitionTime"]])
    if config.get("data", {}).get(key) == value:
        return False
    changes = [{"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
               {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]}]
    if "data" not in config:
        changes.append({"op": "add", "path": "/data", "value": {}})
    changes.append({"op": "add", "path": "/data/" + key, "value": value})
    apply_patch(metadata["name"], metadata["namespace"], changes)
    return True


def bootstrap_issuer(registration, proxy, config, *, apply_patch):
    """Break the v1.104.4 certificate/advertisement startup cycle once.

    Only the owned auth proxy's service is advertised. It still serves TLS on
    443, so no plaintext or unauthenticated API is opened. The operator and
    certificate manager take over normally after certificate issuance. The
    callback must send this patch directly, without logging/persisting it.
    """
    r = registration
    name = r.name + "-issuer"
    metadata = config["metadata"]
    labels = metadata.get("labels", {})
    expected_service = "svc:" + r.issuer.tls_server_name.split(".")[0]
    owned = any(owner.get("kind") == "ProxyGroup" and owner.get("name") == name
                and owner.get("uid") == proxy["metadata"].get("uid")
                and owner.get("controller") is True
                for owner in metadata.get("ownerReferences", []))
    if (proxy["metadata"]["name"] != name
            or proxy["spec"].get("kubeAPIServer") != {
                "hostname": r.issuer.tls_server_name.split(".")[0], "mode": "auth"}
            or metadata["name"] != name + "-0-config"
            or labels.get("tailscale.com/parent-resource") != name or not owned):
        raise ValueError("owned credential issuer required")
    # k8s-proxy's config can contain bootstrap credentials. Keep it in memory.
    raw = json.loads(base64.b64decode(config["data"]["config.hujson"]))
    api = raw.get("APIServerProxy", {})
    if (api.get("Mode") != "auth" or api.get("ServiceName") != expected_service
            or api.get("IssueCerts") is not True):
        raise ValueError("issuer is not configured for managed certificates")
    if raw.get("AdvertiseServices") == [expected_service]:
        return False
    if raw.get("AdvertiseServices"):
        raise ValueError("unexpected advertised service")
    raw["AdvertiseServices"] = [expected_service]
    apply_patch(metadata["name"], metadata["namespace"], [
        {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
        {"op": "replace", "path": "/data/config.hujson", "value":
            base64.b64encode(json.dumps(raw).encode()).decode()},
    ])
    return True


def render(registration):
    r = registration
    name = r.name
    labels = {"app.kubernetes.io/part-of": "bravo-target-access"}

    def obj(kind, *, namespaced=False, **body):
        api = "v1" if kind == "Secret" else "rbac.authorization.k8s.io/v1"
        if kind.startswith("ValidatingAdmission"):
            api = "admissionregistration.k8s.io/v1"
        return {"apiVersion": api, "kind": kind, "metadata": {
            "name": name, "labels": labels,
            **({"namespace": r.namespace} if namespaced else {})}, **body}

    subjects = [{"kind": "Group", "apiGroup": "rbac.authorization.k8s.io", "name": r.issuer_group}]
    rules = [
        {"apiGroups": [""], "resources": ["serviceaccounts/token"],
         "resourceNames": [r.service_account], "verbs": ["create"]},
        {"apiGroups": [""], "resources": ["serviceaccounts"],
         "resourceNames": [r.service_account], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": [name], "verbs": ["get"]},
    ]
    q = json.dumps
    # Restrict requests addressed to this registered account, including any other
    # workload that later receives overlapping RBAC. Human operators remain separate.
    expression = (
        f"object.spec.audiences == [{q(r.audience)}] && "
        "has(object.spec.expirationSeconds) && object.spec.expirationSeconds == 600 && "
        "has(object.spec.boundObjectRef) && "
        "object.spec.boundObjectRef.kind == 'Secret' && "
        f"object.spec.boundObjectRef.name == {q(name)} && "
        "has(object.spec.boundObjectRef.uid) && size(object.spec.boundObjectRef.uid) > 0"
    )
    # Install the denial boundary before exposing any issuer binding.
    return {"apiVersion": "v1", "kind": "List", "items": [
        obj("ValidatingAdmissionPolicy", spec={
            "failurePolicy": "Fail",
            "matchConstraints": {"resourceRules": [{"apiGroups": [""], "apiVersions": ["v1"],
                "operations": ["CREATE"], "resources": ["serviceaccounts/token"]}]},
            "matchConditions": [{"name": "registered-issuer", "expression":
                f"request.userInfo.groups.exists(g, g == {q(r.issuer_group)}) && "
                f"request.namespace == {q(r.namespace)} && request.name == {q(r.service_account)}"}],
            "validations": [{"expression": expression,
                             "message": "registered short-lived revocable token required"}]}),
        obj("ValidatingAdmissionPolicyBinding", spec={"policyName": name, "validationActions": ["Deny"]}),
        obj("Secret", namespaced=True, type="Opaque", immutable=True),
        obj("Role", namespaced=True, rules=rules),
        obj("RoleBinding", namespaced=True, subjects=subjects,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name}),
        obj("ClusterRole", rules=[{"apiGroups": [""], "resources": ["namespaces"],
                                  "resourceNames": ["kube-system"], "verbs": ["get"]}]),
        obj("ClusterRoleBinding", subjects=subjects,
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": name}),
    ]}


def issuer_proxy(registration, observed_noauth_proxy):
    """Reuse the registered tailnet/tag/image class; keep the bearer proxy unchanged."""
    r = registration
    source = observed_noauth_proxy["spec"]
    if (source.get("type") != "kube-apiserver"
            or source.get("kubeAPIServer", {}).get("mode") != "noauth"
            or observed_noauth_proxy.get("status", {}).get("url") != "https://" + r.target.tls_server_name):
        raise ValueError("observed noauth proxy does not match registration")
    return {"apiVersion": observed_noauth_proxy["apiVersion"], "kind": "ProxyGroup",
            "metadata": {"name": r.name + "-issuer", "labels": {
                "app.kubernetes.io/part-of": "bravo-target-access"}},
            "spec": {key: source[key] for key in ("type", "tailnet", "tags", "proxyClass")} | {
                "replicas": 1, "kubeAPIServer": {"mode": "auth",
                    "hostname": r.issuer.tls_server_name.split(".")[0]}}}


def issuer_service(registration, observed_noauth_service):
    """Reuse the caller's existing egress role; never borrow another role's route."""
    r = registration
    metadata = observed_noauth_service["metadata"]
    annotations = metadata.get("annotations", {})
    hostname = urlsplit(r.issuer.server).hostname
    name, separator, domain = hostname.partition(".")
    if (annotations.get("tailscale.com/tailnet-fqdn") != r.target.tls_server_name
            or observed_noauth_service["spec"].get("type") != "ExternalName"
            or "tailscale.com/proxy-group" not in annotations
            or not separator or domain != metadata["namespace"] + ".svc.cluster.local"):
        raise ValueError("observed caller route does not match registration")
    return {"apiVersion": "v1", "kind": "Service", "metadata": {
        "name": name, "namespace": metadata["namespace"],
        "labels": {"app.kubernetes.io/part-of": "bravo-target-access"},
        "annotations": {"tailscale.com/tailnet-fqdn": r.issuer.tls_server_name,
                        "tailscale.com/proxy-group": annotations["tailscale.com/proxy-group"]}},
        "spec": {"type": "ExternalName", "externalName": "placeholder",
                 "ports": [{"name": "https", "port": 443, "protocol": "TCP", "targetPort": 443}]}}


def issuer_tailnet_fragment(registration, observed_noauth_proxy):
    proxy = issuer_proxy(registration, observed_noauth_proxy)
    return {"autoApprovers": {"services": {
        "svc:" + proxy["spec"]["kubeAPIServer"]["hostname"]: proxy["spec"]["tags"]}}}
