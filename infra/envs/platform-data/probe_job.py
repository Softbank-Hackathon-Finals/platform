"""Build a bounded verification Job from a reviewed T10 checkout and locked dependencies."""
import argparse
import base64
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

PYTHON = "docker.io/library/python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
NS = "platform-state"
RUNNER = '''import json, os, pathlib, subprocess, tarfile
os.umask(0o077)
with tarfile.open('/bundle/source.tgz') as t: t.extractall('/work', filter='data')
r=subprocess.run(['python','-m','pip','install','--disable-pip-version-check','--no-cache-dir','--require-hashes','--only-binary=:all:','--target','/work/deps','-r','/work/requirements.txt'],capture_output=True,text=True)
if r.returncode:
 print(json.dumps({'status':'FAILED','stage':'locked_dependency_install'})); raise SystemExit(1)
os.environ['PYTHONPATH']='/work/src:/work/deps'
os.execv('/usr/local/bin/python',['python','/work/verify_t10.py'])
'''


def resources(consumer, requirements, name, probe_id):
    if not re.fullmatch(r"t7-[a-z0-9-]{1,48}", name) or not re.fullmatch(r"t7-[a-z0-9-]{1,48}", probe_id):
        raise ValueError("invalid_probe_identifier")
    files = {str(p.relative_to(consumer)): p.read_bytes() for p in sorted((consumer / "src").rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts}
    files["requirements.txt"] = requirements.read_bytes()
    for module in ("verify_t10.py", "tls_verification.py"):
        files[module] = Path(__file__).with_name(module).read_bytes()
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(content), 0o444, 0
            tar.addfile(info, io.BytesIO(content))
    bundle = gzip.compress(data.getvalue(), mtime=0)
    digest = hashlib.sha256(bundle).hexdigest()
    config_name = "t7-probe-" + digest[:16]
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config_name, "namespace": NS},
              "immutable": True, "binaryData": {"source.tgz": base64.b64encode(bundle).decode()}, "data": {"runner.py": RUNNER}}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": NS}, "spec": {
        "backoffLimit": 0, "activeDeadlineSeconds": 600, "template": {"metadata": {"labels": {
            "bravo.dev/db-client": "runtime", "bravo.dev/probe": "t7-database"}}, "spec": {
            "restartPolicy": "Never", "serviceAccountName": "platform-runtime", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "verify", "image": PYTHON, "command": ["python", "/bundle/runner.py"],
                "env": [{"name": "BRAVO_PROBE_ID", "value": probe_id}, {"name": "HOME", "value": "/work"},
                        {"name": "TMPDIR", "value": "/work"}, {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "768Mi"}},
                "volumeMounts": [{"name": "bundle", "mountPath": "/bundle", "readOnly": True},
                                 {"name": "work", "mountPath": "/work"},
                                 {"name": "credential", "mountPath": "/credential", "readOnly": True},
                                 {"name": "ca", "mountPath": "/ca", "readOnly": True}]}],
            "volumes": [{"name": "bundle", "configMap": {"name": config_name}}, {"name": "work", "emptyDir": {"sizeLimit": "512Mi"}},
                        {"name": "credential", "secret": {"secretName": "platform-runtime-db", "defaultMode": 288}},
                        {"name": "ca", "secret": {"secretName": "platform-db-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]}}}}
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "t7-verification", "namespace": NS},
              "spec": {"podSelector": {"matchLabels": {"bravo.dev/probe": "t7-database"}}, "policyTypes": ["Egress"], "egress": [
                  {"to": [{"podSelector": {"matchLabels": {"cnpg.io/cluster": "platform-db"}}}], "ports": [{"protocol": "TCP", "port": 5432}]},
                  {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                   "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                  # Only this temporary probe can fetch hash-pinned Python wheels; removed after verification.
                  {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}],
                   "ports": [{"protocol": "TCP", "port": 443}]}]}}
    return {"apiVersion": "v1", "kind": "List", "items": [config, policy, job]}, digest


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--consumer", type=Path, required=True)
    p.add_argument("--requirements", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--probe-id", required=True)
    args = p.parse_args()
    value, _ = resources(args.consumer, args.requirements, args.name, args.probe_id)
    print(json.dumps(value))
