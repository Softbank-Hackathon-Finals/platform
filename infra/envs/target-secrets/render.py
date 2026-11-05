#!/usr/bin/env python3
"""Render the target's scoped OpenBao/ESO delivery; never emit credential values."""
import argparse
import ipaddress
import json
import re
import sys

BAO_CHART_VERSION = "0.30.6"
BAO_CHART_SHA256 = "76e90c2315a4922b4b0c6c63fd8a00b9f4bdf0cffda160664f8ea5762d59911c"
BAO_IMAGE = "localhost/bravo-openbao:2.7.2-bravo.3-nonroot@sha256:c85ac2d5f89ac756c3e0b448377ce125b007e9563b03ceb31afafafa3df12bec"
ESO_VERSION = "2.14.0"
ESO_CHART_SHA256 = "0a6d9e91b1ff50adfdbf24b50b582babfa7c5b7df42b21974ca4b3fb88ee5e34"
ESO_TAG = "v2.14.0@sha256:10608eb53e636e744903d5f3657a4d9c44e28d9d9c181b73e07bc7dd2e6884dc"
NAMESPACE = "bravo-secrets"
RELEASE = "target-openbao"
APP_NAMESPACE = "target-dev"
LABELS = {"component": "target", "app.kubernetes.io/part-of": "bravo-target-secrets"}
DB_PATH = "bravo/target/precutover/database"
OBJECT_PATH = "bravo/target/objects"
ADMIN_PATH = "bravo/target/application-admin"
# T15 rehearsals read synthetic credentials from their own KV subtree; the application paths
# above are never readable by a rehearsal identity (design 8.4 "시험용 OpenBao 경로").
REHEARSAL_PREFIX = "bravo/rehearsal"
REHEARSAL_NAMESPACE_RE = re.compile(r"^rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?$")
REHEARSAL_LABEL = {"app.kubernetes.io/part-of": "bravo-rehearsal"}
# The T15 prep Job that writes the run's synthetic values, reader policy and reader role
# (agreed with the T15 owner on 2026-11-06). Its namespace matches ``rehearsal-*`` by name,
# so it is excluded from every reader binding explicitly.
WRITER_NAMESPACE = "rehearsal-control"
WRITER_ACCOUNT = "rehearsal-openbao-writer"
WRITER_POD_LABEL = {"app.kubernetes.io/component": "rehearsal-openbao-writer"}
READER_PREFIX = "rehearsal-app-reader-"
# Chart 0.30.6 defaults are timeout 3 s, period 5 s, failure 2; the TLS-verified
# ``bao status`` exceeded 3 s on the target and Ready flapped.
READINESS_TIMING = {"timeoutSeconds": 10, "periodSeconds": 10, "failureThreshold": 3}
# Fixed fields of every reader role; the writer may set only these exact values.
READER_ROLE_FIXED = {"bound_service_account_names": "rehearsal-eso-reader", "audience": "openbao",
                     "token_ttl": "300s", "token_max_ttl": "900s", "token_no_default_policy": True}


def obj(kind, name, spec=None, namespace=NAMESPACE, api="v1", **fields):
    result = {"apiVersion": api, "kind": kind,
              "metadata": {"name": name, "labels": LABELS.copy()}, **fields}
    if namespace:
        result["metadata"]["namespace"] = namespace
    if spec is not None:
        result["spec"] = spec
    return result


def foundation(node_name):
    if not isinstance(node_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", node_name):
        raise ValueError("verified Kubernetes node name required")
    namespace = obj("Namespace", NAMESPACE, namespace=None)
    namespace["metadata"]["labels"].update({"pod-security.kubernetes.io/enforce": "restricted"})
    volume = obj("PersistentVolume", "bravo-target-openbao-data", namespace=None, spec={
        "capacity": {"storage": "3Gi"}, "volumeMode": "Filesystem",
        "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "", "local": {"path": "/var/lib/bravo-target-dev/openbao"},
        "claimRef": {"namespace": NAMESPACE, "name": "data-target-openbao-0"},
        "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{
            "key": "kubernetes.io/hostname", "operator": "In", "values": [node_name],
        }]}]}},
    })
    claim = obj("PersistentVolumeClaim", "data-target-openbao-0", spec={
        "volumeName": "bravo-target-openbao-data", "storageClassName": "",
        "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "3Gi"}},
    })
    role = obj("ClusterRole", "bravo-target-openbao-tokenreview", namespace=None,
               api="rbac.authorization.k8s.io/v1", rules=[{
                   "apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"],
                   "verbs": ["create"],
               }])
    binding = obj("ClusterRoleBinding", "bravo-target-openbao-tokenreview", namespace=None,
                  api="rbac.authorization.k8s.io/v1",
                  roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                           "name": "bravo-target-openbao-tokenreview"},
                  subjects=[{"kind": "ServiceAccount", "name": RELEASE, "namespace": NAMESPACE}])
    return {"apiVersion": "v1", "kind": "List", "items": [namespace, volume, claim, role, binding]}


def eso_namespace():
    namespace = obj("Namespace", "external-secrets", namespace=None)
    namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = "restricted"
    return namespace


def bao_values():
    return {
        "global": {"tlsDisable": False}, "injector": {"enabled": False},
        "csi": {"enabled": False}, "ui": {"enabled": False},
        "server": {
            "image": {"registry": "localhost", "repository": "bravo-openbao",
                      "tag": BAO_IMAGE.split(":", 1)[1], "pullPolicy": "IfNotPresent"},
            "authDelegator": {"enabled": False},
            "serviceAccount": {"create": True, "name": RELEASE, "serviceDiscovery": {"enabled": False}},
            # The chart reserves component=server for its selectors.
            "extraLabels": {"bravo-component": "target", "app.kubernetes.io/part-of": LABELS["app.kubernetes.io/part-of"]},
            "dev": {"enabled": False}, "ha": {"enabled": False,
                                                   "apiAddr": "https://target-openbao.bravo-secrets.svc:8200"},
            # Chart 0.30.6 interpolates this value unquoted; emit an explicit empty class.
            "dataStorage": {"enabled": True, "size": "3Gi", "storageClass": '""'},
            "persistentVolumeClaimRetentionPolicy": {"whenDeleted": "Retain", "whenScaled": "Retain"},
            "readinessProbe": {"enabled": True, **READINESS_TIMING},
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                          "limits": {"cpu": "500m", "memory": "512Mi"}},
            "extraEnvironmentVars": {"BAO_CACERT": "/openbao/tls/ca.crt"},
            "volumes": [{"name": "tls", "secret": {"secretName": "target-openbao-tls", "defaultMode": 288}},
                        {"name": "private-tmp", "emptyDir": {}}],
            "volumeMounts": [{"name": "tls", "mountPath": "/openbao/tls", "readOnly": True},
                             {"name": "private-tmp", "mountPath": "/tmp"}],
            "statefulSet": {"securityContext": {
                "pod": {"runAsNonRoot": True, "runAsUser": 100, "runAsGroup": 1000,
                        "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                "container": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                              "capabilities": {"drop": ["ALL"]}},
            }},
            "standalone": {"enabled": True, "config": '''ui = false
disable_mlock = true
api_addr = "https://target-openbao.bravo-secrets.svc:8200"
listener "tcp" {
  address = "0.0.0.0:8200"
  cluster_address = "127.0.0.1:8201"
  tls_cert_file = "/openbao/tls/tls.crt"
  tls_key_file = "/openbao/tls/tls.key"
  tls_min_version = "tls12"
}
storage "pebbledb" { path = "/openbao/data/pebbledb" }
'''},
        },
    }


def eso_values():
    image = {"tag": ESO_TAG}
    return {"replicaCount": 1, "image": image, "webhook": {"image": image},
            "certController": {"image": image}, "scopedNamespace": APP_NAMESPACE,
            "scopedRBAC": True, "processClusterStore": False, "processClusterExternalSecret": False,
            "processClusterPushSecret": False, "processPushSecret": False,
            "resources": {"requests": {"cpu": "10m", "memory": "64Mi"},
                          "limits": {"cpu": "250m", "memory": "256Mi"}}}


def finalize_bao(objects):
    """Validate the pinned chart and make its local readiness probe verify TLS."""
    if any(r["metadata"].get("namespace", NAMESPACE) != NAMESPACE for r in objects):
        raise ValueError("chart objects must stay in the intended OpenBao namespace")
    statefulsets = [r for r in objects if r["kind"] == "StatefulSet"]
    if len(statefulsets) != 1 or statefulsets[0]["metadata"]["name"] != RELEASE:
        raise ValueError("unexpected OpenBao StatefulSet")
    if any(r["kind"] in {"Secret", "Pod", "Job", "ClusterRoleBinding"} for r in objects):
        raise ValueError("unexpected secret, test workload, or cluster binding from chart")
    sts = statefulsets[0]
    pod = sts["spec"]["template"]
    if any(pod["metadata"]["labels"].get(k) != v
           for k, v in sts["spec"]["selector"]["matchLabels"].items()):
        raise ValueError("chart selectors do not match the Pod")
    if sts["spec"]["volumeClaimTemplates"][0]["spec"].get("storageClassName") != "":
        raise ValueError("OpenBao must use its prebound retained local volume")
    containers = pod["spec"]["containers"]
    if len(containers) != 1 or containers[0]["image"] != BAO_IMAGE:
        raise ValueError("unexpected OpenBao image")
    probe = containers[0].get("readinessProbe") or {}
    if any(probe.get(key) != value for key, value in READINESS_TIMING.items()) or "exec" not in probe:
        raise ValueError("render the chart with bao-values: readiness probe timing differs")
    probe["exec"]["command"] = ["bao", "status", "-format=json"]
    for service in (r for r in objects if r["kind"] == "Service"):
        if any(pod["metadata"]["labels"].get(k) != v
               for k, v in service["spec"]["selector"].items()):
            raise ValueError("chart Service does not select the OpenBao Pod")
    return objects


def policy():
    paths = ["secret/data/" + p for p in (DB_PATH, OBJECT_PATH, ADMIN_PATH)]
    return "\n".join(f'path "{p}" {{ capabilities = ["read"] }}' for p in paths) + '''
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self" { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
'''


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _run_id(run_id):
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("rehearsal run id required")
    return run_id


def _rehearsal_namespace(namespace):
    if (not isinstance(namespace, str) or not REHEARSAL_NAMESPACE_RE.fullmatch(namespace)
            or namespace == WRITER_NAMESPACE):
        raise ValueError("rehearsal namespace name required")
    return namespace


def rehearsal_policy(run_id):
    """OpenBao policy ``rehearsal-app-reader-<run>``: exactly this run's KV path. A rehearsal
    token can read neither another run's path nor anything under ``bravo/target``."""
    run_id = _run_id(run_id)
    return f'path "secret/data/{REHEARSAL_PREFIX}/{run_id}/*" {{ capabilities = ["read"] }}' + '''
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self" { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
'''


def rehearsal_auth_role(namespace, run_id):
    """Body for ``auth/kubernetes/role/rehearsal-app-reader-<run>``: the reader ServiceAccount in
    exactly this rehearsal namespace, with this run's policy only. Explicit namespace binding
    needs no ``namespaces get`` RBAC for OpenBao (a label selector would), and ties the token's
    read scope to the run that owns the namespace.

    Every value is a scalar: OpenBao matches ``allowed_parameters`` only against strings and
    exact JSON values, so the writer's policy (``rehearsal_writer_policy``) refuses list-valued
    fields. Send this JSON on stdin (``bao write <path> -``) to keep the boolean type."""
    namespace, run_id = _rehearsal_namespace(namespace), _run_id(run_id)
    return {**READER_ROLE_FIXED, "bound_service_account_namespaces": namespace,
            "token_policies": f"{READER_PREFIX}{run_id}"}


def _hcl(value):
    return json.dumps(value)  # JSON strings/lists/booleans are valid HCL literals


def rehearsal_writer_policy():
    """OpenBao policy ``rehearsal-openbao-writer`` for the T15 prep Job.

    Allowed: the rehearsal KV subtree, reader policies and reader roles named
    ``rehearsal-app-reader-*`` and its own token. Nothing under ``bravo/target``, no other
    policy or role name, no ``sys/auth`` or mounts. OpenBao globs: a trailing ``*`` is a prefix
    match (``rehearsal/*`` does not match ``rehearsal-x``), ``+`` is one path segment.

    Reader roles are constrained by parameter: exact SA name, audience and TTLs, a
    ``rehearsal-*`` namespace other than the writer's own, and a reader-prefixed policy.
    Comma lists (one string expanding to several values), glob values and whitespace are denied
    because OpenBao checks the raw string but the auth method splits, trims and globs it.
    ``required_parameters`` also applies to reads in OpenBao 2.7.2, so the role path grants no
    ``read`` (it would always be refused). The reader *policy body* cannot be constrained; see
    README "Writer residual risk"."""
    crud = _hcl(["create", "read", "update", "delete"])
    allowed = {key: [value] for key, value in READER_ROLE_FIXED.items()}
    allowed["bound_service_account_namespaces"] = ["rehearsal-*"]
    allowed["token_policies"] = [READER_PREFIX + "*"]
    unsafe = ["*,*", "***", "* *"]
    denied = {"bound_service_account_namespaces": [WRITER_NAMESPACE + "*"] + unsafe,
              "token_policies": unsafe}
    role_rule = "\n".join(
        [f'path "auth/kubernetes/role/{READER_PREFIX}*" {{',
         f'  capabilities = {_hcl(["create", "update", "delete"])}',
         f"  required_parameters = {_hcl(sorted(allowed))}",
         "  allowed_parameters = {"]
        + [f"    {_hcl(key)} = {_hcl(allowed[key])}" for key in sorted(allowed)]
        + ["  }", "  denied_parameters = {"]
        + [f"    {_hcl(key)} = {_hcl(denied[key])}" for key in sorted(denied)]
        + ["  }", "}"])
    return "\n".join([
        f'path "secret/data/{REHEARSAL_PREFIX}/*" {{ capabilities = {crud} }}',
        f'path "secret/metadata/{REHEARSAL_PREFIX}/*" {{ capabilities = {crud} }}',
        f'path "sys/policies/acl/{READER_PREFIX}*" {{ capabilities = {crud} }}',
        role_rule,
        'path "auth/token/lookup-self" { capabilities = ["read"] }',
        'path "auth/token/renew-self" { capabilities = ["update"] }',
        'path "auth/token/revoke-self" { capabilities = ["update"] }',
    ]) + "\n"


def rehearsal_writer_auth_role():
    """Body for ``auth/kubernetes/role/rehearsal-openbao-writer``: exactly the writer SA in
    ``rehearsal-control``, audience ``openbao``, its policy only, short-lived tokens. The
    writer's own role and policy names fall outside ``rehearsal-app-reader-*``, so the writer
    cannot widen itself."""
    return {**READER_ROLE_FIXED, "bound_service_account_names": WRITER_ACCOUNT,
            "bound_service_account_namespaces": WRITER_NAMESPACE, "token_policies": WRITER_ACCOUNT}


def rehearsal_writer_identity():
    """Optional: the management namespace and writer ServiceAccount. The T15 owner coordinates
    namespace creation with T30; apply this only if that is agreed. Deliberately *not* labelled
    ``bravo-rehearsal`` so attempt cleanup and rehearsal selectors never pick it up."""
    namespace = obj("Namespace", WRITER_NAMESPACE, namespace=None)
    namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] = "restricted"
    account = obj("ServiceAccount", WRITER_ACCOUNT, namespace=WRITER_NAMESPACE,
                  automountServiceAccountToken=False)
    return {"apiVersion": "v1", "kind": "List", "items": [namespace, account]}


def rehearsal_delivery(namespace, run_id):
    """Objects for one rehearsal namespace: reader ServiceAccount, a SecretStore bound to this
    run's role, and an ExternalSecret that projects the synthetic credentials under the *same*
    Secret name the application manifests use, so the candidate image runs unchanged.
    Note: the scoped ESO release reconciles ``target-dev`` only; see README."""
    namespace, run_id = _rehearsal_namespace(namespace), _run_id(run_id)
    remote = f"{REHEARSAL_PREFIX}/{run_id}/database"
    account = obj("ServiceAccount", "rehearsal-eso-reader", namespace=namespace, automountServiceAccountToken=False)
    store = obj("SecretStore", "rehearsal-openbao", namespace=namespace, api="external-secrets.io/v1", spec={"provider": {"vault": {
        "server": "https://target-openbao.bravo-secrets.svc:8200", "path": "secret", "version": "v2",
        "caProvider": {"type": "ConfigMap", "name": "target-openbao-ca", "key": "ca.crt"},
        "auth": {"kubernetes": {"mountPath": "kubernetes", "role": f"rehearsal-app-reader-{run_id}",
                                "serviceAccountRef": {"name": "rehearsal-eso-reader", "audiences": ["openbao"]}}}}}})
    secret = obj("ExternalSecret", "target-app-credentials", namespace=namespace, api="external-secrets.io/v1", spec={
        "refreshInterval": "30s", "secretStoreRef": {"kind": "SecretStore", "name": "rehearsal-openbao"},
        "target": {"name": "target-app-credentials", "creationPolicy": "Owner", "deletionPolicy": "Delete"},
        "data": [{"secretKey": "credentials.json", "remoteRef": {"key": remote, "property": "credentials_json"}}]})
    for item in (account, store, secret):
        item["metadata"]["labels"].update(REHEARSAL_LABEL)
    return {"apiVersion": "v1", "kind": "List", "items": [account, store, secret]}


def rehearsal_fetch_script(run_id):
    """Shell for the init container. Fetch into a private temp file, check it, replace the
    target atomically (``mv`` succeeds even over an earlier 0400 file, so a restart on the same
    emptyDir completes), and always attempt token revocation on exit. A failed fetch leaves no
    half-written file and exits non-zero, so a stale earlier file is never mistaken for success."""
    run_id = _run_id(run_id)
    return (
        "set -eu; umask 077; "
        "export BAO_ADDR=https://target-openbao.bravo-secrets.svc:8200 BAO_CACERT=/openbao/ca/ca.crt; "
        "DIR=/run/bravo/database; OUT=$DIR/credentials.json; TMP=$DIR/.credentials.json.tmp.$$; TOKEN=''; "
        "cleanup() { rc=$?; rm -f \"$TMP\"; "
        "if [ -n \"$TOKEN\" ]; then BAO_TOKEN=\"$TOKEN\" bao token revoke -self >/dev/null 2>&1 || echo 'openbao-fetch: token revoke failed' >&2; fi; "
        "exit $rc; }; trap cleanup EXIT; "
        "JWT=$(cat /var/run/secrets/openbao/token); "
        f"TOKEN=$(bao write -field=token auth/kubernetes/login role=rehearsal-app-reader-{run_id} jwt=\"$JWT\"); "
        f"BAO_TOKEN=\"$TOKEN\" bao kv get -field=credentials_json -mount=secret {REHEARSAL_PREFIX}/{run_id}/database > \"$TMP\"; "
        "[ -s \"$TMP\" ] || { echo 'openbao-fetch: empty credentials' >&2; exit 1; }; "
        "grep -q '\"password\"' \"$TMP\" || { echo 'openbao-fetch: unexpected credentials shape' >&2; exit 1; }; "
        "chmod 0400 \"$TMP\"; mv -f \"$TMP\" \"$OUT\""
    )


def rehearsal_fetch_init_container(run_id):
    """Alternative to ESO for ``rehearsal-*`` while ESO stays scoped to ``target-dev``: an init
    container that logs in with the reader ServiceAccount token and writes ``credentials.json``.
    No secret value appears in this manifest."""
    run_id = _run_id(run_id)
    return {"name": "openbao-fetch", "image": BAO_IMAGE, "command": ["/bin/sh", "-c", rehearsal_fetch_script(run_id)],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "bravo-database", "mountPath": "/run/bravo/database"},
                             {"name": "openbao-ca", "mountPath": "/openbao/ca", "readOnly": True},
                             {"name": "openbao-token", "mountPath": "/var/run/secrets/openbao", "readOnly": True}],
            "x-bravo-remote-path": f"secret/data/{REHEARSAL_PREFIX}/{run_id}/database"}


def delivery():
    account = obj("ServiceAccount", "target-eso-reader", namespace=APP_NAMESPACE,
                  automountServiceAccountToken=False)
    store = obj("SecretStore", "target-openbao", namespace=APP_NAMESPACE,
                api="external-secrets.io/v1", spec={"provider": {"vault": {
                    "server": "https://target-openbao.bravo-secrets.svc:8200",
                    "path": "secret", "version": "v2",
                    "caProvider": {"type": "ConfigMap", "name": "target-openbao-ca", "key": "ca.crt"},
                    "auth": {"kubernetes": {"mountPath": "kubernetes", "role": "target-app-reader",
                        "serviceAccountRef": {"name": "target-eso-reader", "audiences": ["openbao"]}}},
                }}})
    items = [account, store]
    for name, remote, values in [
        ("target-app-credentials", DB_PATH, {"credentials.json": "credentials_json"}),
        ("target-app-objects", OBJECT_PATH, {"access_key": "access_key", "secret_key": "secret_key"}),
        ("target-app-admin", ADMIN_PATH, {"token": "token"}),
    ]:
        items.append(obj("ExternalSecret", name, namespace=APP_NAMESPACE,
                         api="external-secrets.io/v1", spec={
                             "refreshInterval": "30s", "secretStoreRef": {"kind": "SecretStore", "name": "target-openbao"},
                             "target": {"name": name, "creationPolicy": "Owner", "deletionPolicy": "Retain"},
                             "data": [{"secretKey": key, "remoteRef": {"key": remote, "property": prop}}
                                      for key, prop in values.items()],
                         }))
    return {"apiVersion": "v1", "kind": "List", "items": items}


def network(api_ip, api_endpoint_ip):
    addresses = [ipaddress.ip_address(value) for value in (api_ip, api_endpoint_ip)]
    if any(a.version != 4 or not a.is_private or a.is_loopback for a in addresses):
        raise ValueError("verified private Kubernetes API Service and endpoint addresses required")
    return obj("NetworkPolicy", "target-openbao", api="networking.k8s.io/v1", spec={
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "openbao"}},
        "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "external-secrets"}},
             "podSelector": {"matchLabels": {"app.kubernetes.io/name": "external-secrets"}}},
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": APP_NAMESPACE}},
             "podSelector": {"matchLabels": {"bravo-secret-probe": "true"}}},
            # T15 prep Job (rehearsal writer); the Pod template must carry WRITER_POD_LABEL.
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": WRITER_NAMESPACE}},
             "podSelector": {"matchLabels": WRITER_POD_LABEL.copy()}},
        ], "ports": [{"protocol": "TCP", "port": 8200}]}],
        "egress": [{"to": [{"ipBlock": {"cidr": str(address) + "/32"}}],
                    "ports": [{"protocol": "TCP", "port": port}]}
                   for address, port in zip(addresses, (443, 6443), strict=True)] + [
                   {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}],
    })


def eso_network(api_ip, api_endpoint_ip):
    base = network(api_ip, api_endpoint_ip)
    return obj("NetworkPolicy", "external-secrets", namespace="external-secrets",
               api="networking.k8s.io/v1", spec={
                   "podSelector": {}, "policyTypes": ["Ingress", "Egress"],
                   "ingress": [{"from": [{"ipBlock": {"cidr": api_endpoint_ip + "/32"}}],
                                "ports": [{"protocol": "TCP", "port": 10250}]}],
                   "egress": base["spec"]["egress"] + [{
                       "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}},
                               "podSelector": {"matchLabels": {"app.kubernetes.io/name": "openbao"}}}],
                       "ports": [{"protocol": "TCP", "port": 8200}],
                   }],
               })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=[
        "foundation", "eso-namespace", "bao-values", "eso-values", "finalize-bao",
        "policy", "delivery", "network", "eso-network",
        "rehearsal-policy", "rehearsal-auth-role", "rehearsal-delivery", "rehearsal-init-container",
        "rehearsal-writer-policy", "rehearsal-writer-auth-role", "rehearsal-writer-identity",
    ])
    parser.add_argument("--node-name")
    parser.add_argument("--namespace")
    parser.add_argument("--run-id")
    parser.add_argument("--api-ip")
    parser.add_argument("--api-endpoint-ip")
    args = parser.parse_args(argv)
    if args.stage == "policy":
        print(policy())
    elif args.stage == "rehearsal-policy":
        print(rehearsal_policy(args.run_id))
    elif args.stage == "rehearsal-writer-policy":
        print(rehearsal_writer_policy(), end="")
    elif args.stage == "finalize-bao":
        # Only nonsecret, parsed Helm objects belong on this input. JSON makes
        # the renderer independent of an implicit YAML package installation.
        objects = json.load(sys.stdin)
        if isinstance(objects, dict) and objects.get("kind") == "List":
            objects = objects.get("items")
        if not isinstance(objects, list) or not all(isinstance(item, dict) for item in objects):
            raise ValueError("a JSON array or Kubernetes List of chart objects is required")
        print(json.dumps({"apiVersion": "v1", "kind": "List", "items": finalize_bao(objects)}, indent=2))
    else:
        value = {"foundation": lambda: foundation(args.node_name), "bao-values": bao_values,
                 "eso-namespace": eso_namespace, "eso-values": eso_values, "delivery": delivery,
                 "network": lambda: network(args.api_ip, args.api_endpoint_ip),
                 "eso-network": lambda: eso_network(args.api_ip, args.api_endpoint_ip),
                 "rehearsal-auth-role": lambda: rehearsal_auth_role(args.namespace, args.run_id),
                 "rehearsal-delivery": lambda: rehearsal_delivery(args.namespace, args.run_id),
                 "rehearsal-init-container": lambda: rehearsal_fetch_init_container(args.run_id),
                 "rehearsal-writer-auth-role": rehearsal_writer_auth_role,
                 "rehearsal-writer-identity": rehearsal_writer_identity}[args.stage]()
        print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
