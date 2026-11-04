"""Target-local probe. Registry credentials arrive only on stdin, never in argv."""

import base64
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

KUBE = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig", "/etc/rancher/rke2/rke2.yaml"]
NAMESPACE = "target-dev"
OWNER = "bravo.platform/t7-registry-probe"
NONE_POLICY = b"apiVersion: audit.k8s.io/v1\nkind: Policy\nmetadata: {}\nrules:\n- level: None\n"


class Failure(RuntimeError):
    """Fixed failure category with no provider body."""


def call(arguments, obj=None):
    try:
        result = subprocess.run(KUBE + arguments, input=None if obj is None else json.dumps(obj),
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise Failure("kubernetes_transport_unknown") from None
    if result.returncode:
        raise Failure("kubernetes_operation_failed")
    return result.stdout


def get(kind, name=None, namespace=NAMESPACE):
    args = ["get", kind, "-n", namespace, "--ignore-not-found", "-o", "json"]
    if name:
        args.append(name)
    raw = call(args)
    return json.loads(raw) if raw.strip() else None


def identity(obj):
    return obj["metadata"]["uid"]


def check_admitted_pod(requested, observed):
    spec = observed["spec"]
    expected = requested["spec"]
    # Kubelet writes the termination message using these API defaults.
    containers = [dict(container) for container in spec["containers"]]
    for container in containers:
        if (container.pop("terminationMessagePath", "/dev/termination-log") != "/dev/termination-log"
                or container.pop("terminationMessagePolicy", "File") != "File"):
            raise Failure("admitted_probe_spec_changed")
    if (containers != expected["containers"]
            or any(spec.get(k) != v for k, v in expected.items() if k not in ("containers", "imagePullSecrets"))
            or spec.get("imagePullSecrets", []) != expected.get("imagePullSecrets", [])
            or spec.get("serviceAccountName", "default") != "default"
            or any(spec.get(k) for k in ("volumes", "initContainers", "ephemeralContainers",
                                         "hostNetwork", "hostIPC", "hostPID", "shareProcessNamespace"))
            or observed["metadata"].get("labels", {}).get(OWNER) != requested["metadata"]["labels"][OWNER]):
        raise Failure("admitted_probe_spec_changed")


def validate(config):
    if not isinstance(config, dict):
        raise Failure("input_invalid")
    if not re.fullmatch(r"[a-f0-9]{32}", config.get("probe_id", "")):
        raise Failure("probe_id_invalid")
    if config.get("namespace") != NAMESPACE:
        raise Failure("namespace_invalid")
    if not re.fullmatch(r"[0-9]{12}\.dkr\.ecr\.ap-northeast-2\.amazonaws\.com/bravo/demo-chat",
                        config.get("repository_url", "")):
        raise Failure("repository_invalid")
    for field in ("digest", "platform_digest"):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", config.get(field, "")):
            raise Failure("digest_invalid")
    for field in ("cluster_uid", "namespace_uid", "node_uid"):
        try:
            uuid.UUID(config[field])
        except (ValueError, KeyError, TypeError):
            raise Failure("identity_invalid") from None
    pairs = config.get("authorizations")
    if not isinstance(pairs, list) or len(pairs) != 2:
        raise Failure("refresh_pair_invalid")
    for pair in pairs:
        try:
            if not isinstance(pair, dict):
                raise TypeError
            if pair["registry"] != config["repository_url"].split("/")[0]:
                raise ValueError
            raw = base64.b64decode(pair["authorization"], validate=True)
            if not raw.startswith(b"AWS:") or len(raw) < 20:
                raise ValueError
            limit = datetime.fromisoformat(pair["refresh_before"])
            if limit.tzinfo is None or (limit - datetime.now(UTC)).total_seconds() < 300:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise Failure("authorization_invalid_or_expiring") from None
    if pairs[0]["authorization"] == pairs[1]["authorization"]:
        raise Failure("refresh_not_distinct")


def preflight(config):
    validate(config)
    if identity(get("namespace", "kube-system")) != config["cluster_uid"]:
        raise Failure("cluster_identity_mismatch")
    if identity(get("namespace", NAMESPACE)) != config["namespace_uid"]:
        raise Failure("namespace_identity_mismatch")
    nodes = get("nodes")["items"]
    if len(nodes) != 1 or identity(nodes[0]) != config["node_uid"]:
        raise Failure("node_identity_mismatch")
    if Path("/etc/rancher/rke2/registries.yaml").exists():
        raise Failure("existing_registry_configuration_needs_review")
    # This path supports the existing, explicitly observed development VM only.
    # None protects bodies but does not satisfy Metadata audit requirements.
    path = Path("/etc/rancher/rke2/audit-policy.yaml")
    if path.read_bytes() != NONE_POLICY:
        raise Failure("audit_policy_not_reviewed")
    pods = get("pods", namespace="kube-system")["items"]
    servers = [p for p in pods if p["metadata"].get("labels", {}).get("component") == "kube-apiserver"]
    if len(servers) != 1:
        raise Failure("api_server_inventory_invalid")
    server = servers[0]
    args = [arg for c in server["spec"]["containers"] for arg in c.get("command", []) + c.get("args", [])]
    if "--audit-policy-file=" + str(path) not in args:
        raise Failure("audit_policy_binding_unknown")
    started = datetime.fromisoformat(server["status"]["startTime"])
    if path.stat().st_mtime > started.timestamp():
        raise Failure("audit_policy_changed_after_start")
    return {"audit_mode": "None", "metadata_audit_verified": False,
            "audit_policy_sha256": hashlib.sha256(NONE_POLICY).hexdigest()}


class Probe:
    def __init__(self, config):
        self.config = config
        self.run = config.get("probe_id", "") if isinstance(config, dict) else ""
        self.prefix = "t7-pull-" + self.run[:16]
        self.owned = []

    def metadata(self, suffix):
        return {"name": self.prefix + "-" + suffix, "namespace": NAMESPACE, "labels": {OWNER: self.run}}

    def create(self, obj, resource):
        name = obj["metadata"]["name"]
        if get(resource, name) is not None:
            raise Failure("resource_already_exists")
        record = {"resource": resource, "name": name, "uid": None}
        self.owned.append(record)  # Keep uncertain creates visible for independent reconciliation.
        created = json.loads(call(["create", "-f", "-", "-o", "json"], obj))
        metadata = created["metadata"]
        uid = metadata.get("uid")
        if (not isinstance(uid, str) or not uid or metadata.get("name") != name
                or metadata.get("namespace") != NAMESPACE or metadata.get("labels", {}).get(OWNER) != self.run):
            raise Failure("created_resource_identity_unknown")
        record["uid"] = uid
        actual = get(resource, name)
        if actual is None or identity(actual) != uid or actual["metadata"].get("labels", {}).get(OWNER) != self.run:
            raise Failure("created_resource_identity_unknown")
        return actual

    def secret(self, pair):
        docker = {"auths": {pair["registry"]: {"auth": pair["authorization"]}}}
        return {"apiVersion": "v1", "kind": "Secret", "metadata": self.metadata("auth"),
                "type": "kubernetes.io/dockerconfigjson",
                "data": {".dockerconfigjson": base64.b64encode(json.dumps(docker).encode()).decode()}}

    def refresh(self, current, pair):
        actual = get("secrets", current["metadata"]["name"])
        if identity(actual) != identity(current) or actual["metadata"].get("labels", {}).get(OWNER) != self.run:
            raise Failure("secret_identity_changed")
        desired = self.secret(pair)
        desired["metadata"].update(uid=identity(actual), resourceVersion=actual["metadata"]["resourceVersion"])
        call(["replace", "-f", "-", "-o", "json"], desired)
        observed = get("secrets", actual["metadata"]["name"])
        if identity(observed) != identity(actual) or observed.get("data") != desired["data"]:
            raise Failure("secret_refresh_unknown")
        return observed

    def pod(self, suffix, authenticated):
        config = self.config
        obj = {"apiVersion": "v1", "kind": "Pod", "metadata": self.metadata(suffix), "spec": {
            "restartPolicy": "Never", "activeDeadlineSeconds": 150, "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "probe", "image": config["repository_url"] + "@" + config["digest"],
                "imagePullPolicy": "Always", "command": ["/app/.venv/bin/python", "-c",
                    "import os,json; print(json.dumps({'uid':os.getuid()}))"],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                              "limits": {"cpu": "100m", "memory": "96Mi"}}}]}}
        if authenticated:
            obj["spec"]["imagePullSecrets"] = [{"name": self.prefix + "-auth"}]
        actual = self.create(obj, "pods")
        pod_uid = identity(actual)
        check_admitted_pod(obj, actual)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            actual = get("pods", obj["metadata"]["name"])
            if actual is None or identity(actual) != pod_uid:
                raise Failure("probe_pod_identity_changed")
            check_admitted_pod(obj, actual)
            states = actual.get("status", {}).get("containerStatuses", [])
            if not authenticated:
                for state in states:
                    waiting = state.get("state", {}).get("waiting", {})
                    if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff"):
                        message = waiting.get("message", "")
                        if "401 Unauthorized" in message or "no basic auth credentials" in message:
                            return {"authenticated": False, "unauthorized_observed": True, "pod_uid": pod_uid}
                        raise Failure("anonymous_failure_not_permission_proof")
            phase = actual.get("status", {}).get("phase")
            if phase == "Succeeded":
                if not authenticated:
                    raise Failure("anonymous_pull_succeeded")
                image_id = states[0].get("imageID", "") if states else ""
                digest = re.search(r"sha256:[a-f0-9]{64}$", image_id)
                if not digest or digest.group() not in (config["digest"], config["platform_digest"]):
                    raise Failure("runtime_digest_mismatch")
                logs = json.loads(call(["logs", obj["metadata"]["name"], "-n", NAMESPACE]))
                if logs != {"uid": 10001}:
                    raise Failure("runtime_identity_invalid")
                events = json.loads(call(["get", "events", "-n", NAMESPACE, "--field-selector",
                                         "involvedObject.uid=" + pod_uid, "-o", "json"]))["items"]
                if not any(e.get("reason") == "Pulled" and "Successfully pulled image" in e.get("message", "") for e in events):
                    raise Failure("actual_pull_event_missing")
                return {"authenticated": True, "runtime_digest": digest.group(), "pod_uid": pod_uid,
                        "pull_event": True, "runtime_uid": 10001}
            if phase == "Failed":
                raise Failure("probe_failed")
            time.sleep(2)
        raise Failure("probe_timeout_unknown")

    def cleanup(self):
        results = []
        for record in reversed(self.owned):
            try:
                if record["uid"] is None:
                    results.append(False)
                    continue
                actual = get(record["resource"], record["name"])
                if actual is None:
                    results.append(True)
                    continue
                if (actual["metadata"].get("labels", {}).get(OWNER) != self.run
                        or identity(actual) != record["uid"]):
                    results.append(False)
                    continue
                group = "/apis/networking.k8s.io/v1" if record["resource"] == "networkpolicies" else "/api/v1"
                path = f"{group}/namespaces/{NAMESPACE}/{record['resource']}/{record['name']}"
                call(["delete", "--raw", path, "-f", "-"], {
                    "apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": record["uid"]},
                    "gracePeriodSeconds": 0})
                deadline = time.monotonic() + 20
                while get(record["resource"], record["name"]) is not None and time.monotonic() < deadline:
                    time.sleep(1)
                results.append(get(record["resource"], record["name"]) is None)
            except (Failure, ValueError, KeyError, TypeError):
                results.append(False)
        return bool(results) and all(results)


def run(config):
    result = {"status": "UNKNOWN", "observed_at": datetime.now(UTC).isoformat(), "pulls": [],
              "cleanup_verified": False, "automatic_renewal_verified": False,
              "expiry_and_renewal_observed": False, "product_deployment_verified": False}
    probe = Probe(config)
    try:
        result.update(preflight(config))
        result["cluster_uid"] = config["cluster_uid"]
        result["namespace_uid"] = config["namespace_uid"]
        before = {x["metadata"]["name"]: identity(x) for x in get("pods")["items"]}
        secret = probe.create(probe.secret(config["authorizations"][0]), "secrets")
        probe.create({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                      "metadata": probe.metadata("deny"), "spec": {"podSelector": {"matchLabels": {OWNER: probe.run}},
                      "policyTypes": ["Ingress", "Egress"]}}, "networkpolicies")
        result["pulls"].append(probe.pod("before", True))
        result["pulls"].append(probe.pod("anonymous", False))
        probe.refresh(secret, config["authorizations"][1])
        result["credential_refresh_readback_verified"] = True
        result["pulls"].append(probe.pod("after", True))
        after = {x["metadata"]["name"]: identity(x) for x in get("pods")["items"]}
        result["existing_pod_uids_preserved"] = all(after.get(k) == v for k, v in before.items())
        if not result["existing_pod_uids_preserved"]:
            raise Failure("existing_pod_identity_changed")
        result["status"] = "PASS"
    except Failure as error:
        result["error_category"] = str(error)
    except (OSError, ValueError, KeyError, TypeError):
        result["error_category"] = "invalid_probe_state"
    finally:
        result["cleanup_verified"] = probe.cleanup()
        result["owned_resources"] = probe.owned
        if not result["cleanup_verified"]:
            result["status"] = "UNKNOWN"
    return result


if __name__ == "__main__":
    try:
        result = run(json.loads(sys.stdin.read(65537)))
    except (ValueError, TypeError):
        result = {"status": "UNKNOWN", "error_category": "input_invalid"}
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "PASS" else 2)
