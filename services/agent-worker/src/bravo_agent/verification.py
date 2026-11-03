"""Trusted T15 handoff; no model client and no receipt supplied by the model."""

import asyncio
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from .contracts import FailureBundle, Proposal, SnapshotManifest, VerificationReceipt, digest
from .coordinator import Coordinator
from .tools import Snapshot

# Candidate and verifier run in different Pods; neither has a service-account token.
CANDIDATE = '''import importlib.util,json
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  try:
   spec=importlib.util.spec_from_file_location("candidate","/candidate/config.py")
   module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
   data=json.dumps({"value":module.DB_HOST}).encode();code=200
  except Exception:
   data=b'{"missing":true}';code=503
  self.send_response(code);self.end_headers();self.wfile.write(data)
HTTPServer(("0.0.0.0",8080),Handler).serve_forever()
'''
VERIFIER = '''import json,os,urllib.request,urllib.error
def request(host):
 try:
  with urllib.request.urlopen("http://"+host+":8080/",timeout=10) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.load(e)
good=request(os.environ["PRESENT_HOST"]);missing=request(os.environ["MISSING_HOST"])
checks={"injected_value":good==(200,{"value":os.environ["EXPECTED_VALUE"]}),
        "missing_value_closed":missing==(503,{"missing":True})}
print(json.dumps(checks,sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 1)
'''


class Kube:
    def __init__(self, executable="kubectl"):
        self.executable = executable

    def call(self, *args, data=None):
        result = subprocess.run([self.executable, *args], input=json.dumps(data) if data else None,
                                capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise RuntimeError("KUBERNETES_REQUEST_FAILED")
        return result.stdout

    def create(self, obj):
        return json.loads(self.call("create", "-f", "-", "-o", "json", data=obj))

    def get(self, kind, name, namespace=None):
        args = ["get", kind, name, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        return json.loads(self.call(*args))

    def cleanup(self, namespace, uid):
        self.call("delete", "--raw", f"/api/v1/namespaces/{namespace}", "-f", "-",
                  data={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
        for _ in range(60):
            raw = self.call("get", "namespace", namespace, "--ignore-not-found", "-o", "json")
            if not raw.strip():
                return True
            if json.loads(raw)["metadata"]["uid"] != uid:
                return False
            time.sleep(1)
        return False


def job(namespace, name, image, maps, env, *, verifier=False):
    volumes = [{"name": key, "configMap": {"name": key}} for key in maps]
    container = {"name": "run", "image": image, "imagePullPolicy": "IfNotPresent",
                 "command": ["python", "-I", "/runner/main.py"],
                 "env": [{"name": key, "value": value} for key, value in env.items()],
                 "volumeMounts": [{"name": key, "mountPath": path, "readOnly": True} for key, path in maps.items()],
                 "resources": {"requests": {"cpu": "10m", "memory": "64Mi"},
                               "limits": {"cpu": "500m", "memory": "256Mi"}},
                 "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                     "capabilities": {"drop": ["ALL"]}}}
    if not verifier:
        container["readinessProbe"] = {"tcpSocket": {"port": 8080}, "periodSeconds": 1}
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": namespace},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 180, "ttlSecondsAfterFinished": 600,
                     "template": {"metadata": {"labels": {"role": "verifier" if verifier else "candidate", "job": name}},
                                  "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                                                               "runAsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
                                           "containers": [container], "volumes": volumes}}}}


def verify_environment(kube, proposal, image):
    """Bounded DB_HOST integration fixture, not the T9 product-template acceptance."""
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("digest-pinned verifier image required")
    if len(proposal.edits) != 1 or proposal.edits[0].path != "src/config.py":
        raise ValueError("unsupported template")
    namespace = "rehearsal-t15-" + uuid.uuid4().hex[:16]
    uid = None
    evidence = {"template": "db-environment-integration", "job_complete": False,
                "namespace_deleted": False, "checks": {}, "failure_code": "VERIFIER_UNAVAILABLE"}
    try:
        ns = kube.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace,
                          "labels": {"component": "platform", "pod-security.kubernetes.io/enforce": "restricted"}}})
        uid = ns["metadata"]["uid"]
        kube.create({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                     "metadata": {"name": "deny", "namespace": namespace},
                     "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}})
        kube.create({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                     "metadata": {"name": "candidate-ingress", "namespace": namespace},
                     "spec": {"podSelector": {"matchLabels": {"role": "candidate"}}, "policyTypes": ["Ingress"],
                              "ingress": [{"from": [{"podSelector": {"matchLabels": {"role": "verifier"}}}],
                                           "ports": [{"protocol": "TCP", "port": 8080}]}]}})
        kube.create({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                     "metadata": {"name": "verifier-egress", "namespace": namespace},
                     "spec": {"podSelector": {"matchLabels": {"role": "verifier"}}, "policyTypes": ["Egress"],
                              "egress": [{"to": [{"podSelector": {"matchLabels": {"role": "candidate"}}}],
                                          "ports": [{"protocol": "TCP", "port": 8080}]}]}})
        for name, contents in [("candidate", {"config.py": proposal.edits[0].replacement}),
                               ("wrapper", {"main.py": CANDIDATE}), ("verifier", {"main.py": VERIFIER})]:
            kube.create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace},
                         "immutable": True, "data": contents})
        marker = "db-" + uuid.uuid4().hex
        addresses = {}
        for name, env in [("present", {"DB_HOST": marker}), ("missing", {})]:
            kube.create(job(namespace, name, image, {"candidate": "/candidate", "wrapper": "/runner"}, env))
            for _ in range(30):
                pods = json.loads(kube.call("get", "pods", "-n", namespace, "-l", "job=" + name, "-o", "json"))["items"]
                if pods:
                    break
                time.sleep(1)
            kube.call("wait", "pod", "-n", namespace, "-l", "job=" + name, "--for=condition=Ready", "--timeout=40s")
            pods = json.loads(kube.call("get", "pods", "-n", namespace, "-l", "job=" + name, "-o", "json"))["items"]
            if len(pods) != 1:
                raise ValueError("ambiguous candidate pod")
            addresses[name] = pods[0]["status"]["podIP"]
        created = kube.create(job(namespace, "verify", image, {"verifier": "/runner"},
                                  {"PRESENT_HOST": addresses["present"], "MISSING_HOST": addresses["missing"],
                                   "EXPECTED_VALUE": marker}, verifier=True))
        verifier_uid = created["metadata"]["uid"]
        for _ in range(90):
            current = kube.get("job", "verify", namespace)
            if current["metadata"]["uid"] != verifier_uid:
                raise ValueError("verifier job replaced")
            conditions = current.get("status", {}).get("conditions", [])
            complete = any(c["type"] == "Complete" and c["status"] == "True" for c in conditions)
            failed = any(c["type"] == "Failed" and c["status"] == "True" for c in conditions)
            if complete or failed:
                checks = json.loads(kube.call("logs", "job/verify", "-n", namespace, "--limit-bytes=2048"))
                if set(checks) != {"injected_value", "missing_value_closed"} or any(type(v) is not bool for v in checks.values()):
                    raise ValueError("invalid verifier output")
                evidence.update(job_complete=complete, checks=checks,
                                failure_code="NONE" if complete and all(checks.values()) else "TEMPLATE_TEST_FAILED")
                break
            time.sleep(1)
    except Exception:
        evidence["failure_code"] = "VERIFIER_UNAVAILABLE"
    finally:
        if uid:
            try:
                evidence["namespace_deleted"] = kube.cleanup(namespace, uid)
            except Exception:
                evidence["namespace_deleted"] = False
    evidence["verifier_image"] = image
    return evidence


def integrate(bundle, manifest, snapshot_root, result, kube, image):
    bundle = FailureBundle.model_validate(bundle)
    manifest = SnapshotManifest.model_validate(manifest)
    proposal = Proposal.model_validate(result["proposal"])
    if (result.get("state") != "PROPOSAL_READY" or result.get("attempt") != 1
            or result.get("input_digest") != digest(bundle)
            or result.get("snapshot_digest") != digest(manifest)
            or result.get("proposal_digest") != digest(proposal)):
        raise ValueError("unbound proposal result")
    coordinator = Coordinator(bundle, Snapshot(Path(snapshot_root), manifest))

    class ValidatedProposal:
        async def propose(self, bundle, tools):
            tools.dispatch("mcp__bravo__plan_propose", proposal.model_dump())

    asyncio.run(coordinator.propose(ValidatedProposal()))
    if coordinator.state != "PROPOSAL_READY":
        raise ValueError("proposal failed independent input validation")
    evidence = verify_environment(kube, proposal, image)
    known = evidence["namespace_deleted"] and evidence["failure_code"] != "VERIFIER_UNAVAILABLE"
    passed = known and evidence["job_complete"] and all(evidence["checks"].values())
    verdict = "PASS" if passed else "FAIL" if known else "UNKNOWN"
    evidence_digest = digest(evidence)
    receipt = VerificationReceipt(run_id=bundle.run_id, source_sha=bundle.source_sha,
                                  proposal_digest=digest(proposal), attempt=1, verdict=verdict,
                                  effects_known=known, evidence_digest=evidence_digest,
                                  failure=FailureBundle(run_id=bundle.run_id, source_sha=bundle.source_sha,
                                      failure_code="TEMPLATE_TEST_FAILED", summary="Independent environment contract failed.",
                                      evidence_digest=evidence_digest) if verdict == "FAIL" else None)
    coordinator.verify(receipt)
    return {"state": coordinator.state, "receipt": receipt.model_dump(), "evidence": evidence,
            "candidate_sha256": hashlib.sha256(proposal.edits[0].replacement.encode()).hexdigest(),
            "scope": "DB environment integration fixture; T9 product templates remain separate"}


def main():
    import argparse
    import os
    import stat

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "manifest", "snapshot", "result"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--kubectl", default="kubectl")
    args = parser.parse_args()

    def read(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1048576:
                raise ValueError("bounded regular input required")
            return json.loads(stream.read(1048577))

    try:
        output = integrate(read(args.bundle), read(args.manifest), args.snapshot,
                           read(args.result), Kube(args.kubectl), args.image)
        print(json.dumps(output, sort_keys=True))
        return 0
    except Exception:
        print('{"state":"NEEDS_HUMAN","reason":"INVALID_OR_UNAVAILABLE_VERIFICATION"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
