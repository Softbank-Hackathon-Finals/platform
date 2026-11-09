"""Run three ordered, token-projected Jobs in an operator-owned disposable verifier namespace."""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe_job import PYTHON

NAME = "t7-synthetic"
ROLE = "t7-probe-reader"
LABEL = {"bravo.dev/probe": NAME}
RBAC = "rbac.authorization.k8s.io/v1"
NETWORK = "networking.k8s.io/v1"
JOB = "batch/v1"
PROGRAM = Path(__file__).with_name("observe.py").read_text()
PLURAL = {"NetworkPolicy": "networkpolicies", "ServiceAccount": "serviceaccounts", "Secret": "secrets",
          "Role": "roles", "RoleBinding": "rolebindings", "Job": "jobs"}


def resource(kind, name, api="v1", **fields):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "labels": LABEL}, **fields}


def api_peer(spec):
    host, port = spec.rsplit(":", 1)
    addr = ipaddress.ip_address(host.strip("[]"))
    if not 1 <= int(port) <= 65535 or addr.is_unspecified or addr.is_multicast:
        raise ValueError("invalid_api_peer")
    return {"to": [{"ipBlock": {"cidr": f"{addr}/{addr.max_prefixlen}"}}],
            "ports": [{"protocol": "TCP", "port": int(port)}]}


def fixtures(ns, peers):
    yield resource("NetworkPolicy", NAME, NETWORK, spec={
        "podSelector": {"matchLabels": LABEL}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [], "egress": peers})
    for sa in ("reader", "denied"):
        yield resource("ServiceAccount", sa, automountServiceAccountToken=False)
    yield resource("Secret", NAME, type="Opaque", immutable=True)
    yield resource("Role", ROLE, RBAC, rules=[{"apiGroups": [""], "resources": ["secrets"],
                                              "resourceNames": [NAME], "verbs": ["get"]}])
    yield resource("RoleBinding", ROLE, RBAC, roleRef={"apiGroup": RBAC.split("/")[0], "kind": "Role", "name": ROLE},
                   subjects=[{"kind": "ServiceAccount", "namespace": ns, "name": "reader"}])


def job(ns, name, role):
    projection = [{"serviceAccountToken": {"path": "token", "expirationSeconds": 600}},
                  {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]
    return resource("Job", name, JOB, spec={"backoffLimit": 0, "activeDeadlineSeconds": 120, "template": {
        "metadata": {"labels": LABEL}, "spec": {
            "automountServiceAccountToken": False, "serviceAccountName": role, "restartPolicy": "Never",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "observer", "image": PYTHON, "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-I", "-B", "-c", PROGRAM],
                            "env": [{"name": "PROBE_NAMESPACE", "value": ns}],
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}},
                            "resources": {"requests": {"cpu": "10m", "memory": "24Mi"},
                                          "limits": {"cpu": "100m", "memory": "64Mi"}},
                            "volumeMounts": [{"name": "identity", "mountPath": "/identity", "readOnly": True}]}],
            "volumes": [{"name": "identity", "projected": {"sources": projection}}]}}})


class Probe:
    def __init__(self, args):
        self.ns = args.namespace
        if not re.fullmatch(r"t7-secret-probe-[a-f0-9]{12}", self.ns):
            raise ValueError("invalid_probe_namespace")
        self.cmd = ["kubectl", "--kubeconfig", str(args.kubeconfig), "-n", self.ns, "--request-timeout=20s"]
        self.path = args.receipt
        self.state = {"namespace": self.ns, "namespace_uid": str(UUID(args.namespace_uid)), "owned": [],
                      "service_accounts": {}, "steps": [], "cleanup_confirmed": False}

    def kube(self, *args, obj=None):
        p = subprocess.run(self.cmd + list(args), input=json.dumps(obj) if obj is not None else None,
                           text=True, capture_output=True, timeout=150, check=False)
        if p.returncode:
            raise RuntimeError("kubernetes_operation_failed")
        return p.stdout.strip()

    def save(self):
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as f:
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(f.name, self.path)

    def check_ns(self):
        actual = self.kube("get", "namespace", self.ns, "-o", "jsonpath={.metadata.uid}")
        if actual != self.state["namespace_uid"]:
            raise RuntimeError("namespace_replaced")

    def create(self, obj):
        self.check_ns()
        kind, name = obj["kind"], obj["metadata"]["name"]
        obj["metadata"]["namespace"] = self.ns
        entry = {"kind": kind, "name": name, "uid": None}
        self.state["owned"].append(entry)
        self.save()
        uid = str(UUID(self.kube("create", "-f", "-", "-o", "jsonpath={.metadata.uid}", obj=obj)))
        entry["uid"] = uid
        self.save()
        return uid

    def step(self, index, role):
        name = f"probe-{index}-{role}"
        uid = self.create(job(self.ns, name, role))
        self.kube("wait", "--request-timeout=140s", "--for=condition=complete", "job/" + name, "--timeout=130s")
        pods = json.loads(self.kube("get", "pods", "-l", f"batch.kubernetes.io/controller-uid={uid}", "-o", "json"))["items"]
        if len(pods) != 1:
            raise RuntimeError("ambiguous_job_pod")
        pod = pods[0]
        if (pod["spec"]["serviceAccountName"] != role or pod["status"]["phase"] != "Succeeded"
                or not any(ref.get("controller") and ref["uid"] == uid for ref in pod["metadata"]["ownerReferences"])):
            raise RuntimeError("pod_identity_mismatch")
        data = json.loads(self.kube("logs", pod["metadata"]["name"], "-c", "observer"))
        # Keep only typed metadata, even if an unexpected process wrote the log.
        status = data.get("status")
        step = {"status": status if type(status) is int else None, "pod_uid": str(UUID(pod["metadata"]["uid"]))}
        for field in ("audit_id", "uid"):
            if data.get(field):
                step[field] = str(UUID(data[field]))
        self.state["steps"].append(step)
        self.save()

    def remove(self, entry, api):
        self.check_ns()
        kind, name = entry["kind"], entry["name"]
        prefix = "/api/v1" if api == "v1" else "/apis/" + api
        path = f"{prefix}/namespaces/{self.ns}/{PLURAL[kind]}/{name}"
        current = self.kube("get", kind, name, "--ignore-not-found", "-o", "jsonpath={.metadata.uid}")
        if not current:
            return
        if entry["uid"] != current:
            raise RuntimeError("ownership_unconfirmed")
        self.kube("delete", "--raw", path, "-f", "-", obj={
            "apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
            "preconditions": {"uid": current}})
        self.kube("wait", "--request-timeout=70s", "--for=delete", kind, name, "--timeout=60s")
        if self.kube("get", kind, name, "--ignore-not-found", "-o", "name"):
            raise RuntimeError("object_still_present")

    def cleanup(self):
        targets = {(obj["kind"], obj["metadata"]["name"]): obj["apiVersion"] for obj in fixtures(self.ns, [])}
        targets.update({("Job", f"probe-{i}-{role}"): JOB for i, role in enumerate(("reader", "denied", "reader"), 1)})
        errors = []
        for entry in reversed(self.state["owned"]):
            try:
                self.remove(entry, targets[entry["kind"], entry["name"]])
            except (RuntimeError, OSError, subprocess.SubprocessError) as err:
                errors.append(type(err).__name__)
        self.check_ns()
        self.state["cleanup_confirmed"] = not errors
        self.save()
        if errors:
            raise RuntimeError("cleanup_unconfirmed")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True, type=Path)
    p.add_argument("--namespace", required=True)
    p.add_argument("--namespace-uid", required=True)
    p.add_argument("--receipt", required=True, type=Path)
    p.add_argument("--api-peer", action="append", type=api_peer, default=[])
    p.add_argument("--cleanup-only", action="store_true")
    a = p.parse_args()
    os.umask(0o077)
    probe = Probe(a)
    probe.check_ns()
    if a.cleanup_only:
        state = json.loads(a.receipt.read_text())
        if (state["namespace"], state["namespace_uid"]) != (probe.ns, probe.state["namespace_uid"]):
            raise ValueError("wrong_receipt")
        probe.state = state
        probe.cleanup()
        return
    if not a.api_peer:
        p.error("supply the approved API peer IP and port")
    a.receipt.touch(exist_ok=False)
    probe.save()
    try:
        for item in fixtures(probe.ns, a.api_peer):
            uid = probe.create(item)
            if item["kind"] == "ServiceAccount":
                probe.state["service_accounts"][item["metadata"]["name"]] = uid
            if item["kind"] == "Secret":
                probe.state["fixture_uid"] = uid
        for index, role in enumerate(("reader", "denied", "reader"), start=1):
            probe.step(index, role)
    finally:
        probe.cleanup()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as err:
        print("UNKNOWN", type(err).__name__, file=sys.stderr)
        raise SystemExit(2) from None
