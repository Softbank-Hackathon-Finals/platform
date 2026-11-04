"""Disposable local RKE2 SeaweedFS policy experiment; never logs credentials."""
import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
from contextlib import suppress

import boto3
from botocore.exceptions import ClientError

from rehearsal_runner.bucket_probe import observe_bucket_reads
from rehearsal_runner.bucket_s3 import bucket_readers

os.umask(0o077)
os.environ["KUBECONFIG"] = "/etc/rancher/rke2/rke2.yaml"
K = "/var/lib/rancher/rke2/bin/kubectl"
parser = argparse.ArgumentParser(description="Create an isolated local RKE2 bucket fixture")
parser.add_argument("--report", required=True, help="new private report path")
args = parser.parse_args()
report_fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
suffix = secrets.token_hex(4)
namespace = "rehearsal-bucket-proof-" + suffix
bucket = "rehearsal-proof-" + suffix
access, secret = "T15" + secrets.token_hex(9).upper(), secrets.token_hex(24)
canary = b"t15 local bucket policy test\n"
results = {"environment": "local-rke2", "seaweedfs": "4.07", "namespace": namespace,
           "bucket": bucket, "policy_applied": False}


def create(obj):
    proc = subprocess.run([K, "create", "-f", "-"], input=json.dumps(obj), text=True,
                          capture_output=True, timeout=30)
    if proc.returncode:
        raise RuntimeError("kubernetes_create_failed")


def obs(signed, unsigned):
    value = observe_bucket_reads(signed, unsigned, bucket=bucket, key="canary",
                                 expected_sha256=hashlib.sha256(canary).hexdigest())
    return {"authenticated_before": value.authenticated_before, "anonymous": value.anonymous,
            "authenticated_after": value.authenticated_after}


forward = None
try:
    if socket.gethostname() != "bravo-t15-rke2":
        raise RuntimeError("not_local_test_vm")
    create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace,
           "labels": {"purpose": "t15-bucket-policy-test", "pod-security.kubernetes.io/enforce": "restricted"}}})
    create({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "deny-ingress", "namespace": namespace},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}})
    config = {"identities": [{"name": "t15-test-admin", "credentials": [{"accessKey": access,
               "secretKey": secret}], "actions": ["Admin", "Read", "Write", "List"]}]}
    create({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "t15-s3-config", "namespace": namespace},
            "type": "Opaque", "data": {"s3.json": base64.b64encode(json.dumps(config).encode()).decode()}})
    security = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    create({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "seaweed", "namespace": namespace,
           "labels": {"app": "t15-bucket-policy-test"}}, "spec": {"automountServiceAccountToken": False,
           "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                               "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
           "volumes": [{"name": "data", "emptyDir": {"sizeLimit": "1Gi"}},
                       {"name": "auth", "secret": {"secretName": "t15-s3-config", "defaultMode": 288}}],
           "containers": [{"name": "seaweed", "image": "chrislusf/seaweedfs:4.07",
              "args": ["server", "-s3", "-dir=/data", "-ip=$(POD_IP)", "-ip.bind=0.0.0.0",
                       "-s3.config=/etc/t15/s3.json", "-master.volumeSizeLimitMB=128", "-volume.max=1"],
              "env": [{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}],
              "ports": [{"containerPort": 8333}], "securityContext": security,
              "volumeMounts": [{"name": "data", "mountPath": "/data"},
                               {"name": "auth", "mountPath": "/etc/t15", "readOnly": True}],
              "readinessProbe": {"tcpSocket": {"port": 8333}, "periodSeconds": 5}}]}})
    wait = subprocess.run([K, "-n", namespace, "wait", "--for=condition=Ready", "pod/seaweed",
                           "--timeout=180s"], capture_output=True, text=True, timeout=190)
    if wait.returncode:
        raise RuntimeError("seaweed_not_ready")
    forward = subprocess.Popen([K, "-n", namespace, "port-forward", "pod/seaweed", ":8333"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    first = forward.stdout.readline().strip()
    if not first.startswith("Forwarding from 127.0.0.1:"):
        raise RuntimeError("port_forward_unavailable")
    port = int(first.split(":")[1].split()[0])
    endpoint = "http://127.0.0.1:" + str(port)
    session = boto3.Session(aws_access_key_id=access, aws_secret_access_key=secret)
    signed, unsigned = bucket_readers(session, endpoint_url=endpoint, region="us-east-1")
    signed.client.create_bucket(Bucket=bucket)
    signed.client.put_object(Bucket=bucket, Key="canary", Body=canary)
    results["before"] = obs(signed, unsigned)
    policy = {"Version": "2012-10-17", "Statement": [{"Sid": "DenyAnonymousRead", "Effect": "Deny",
              "Principal": "*", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::" + bucket + "/*",
              "Condition": {"StringEquals": {"aws:PrincipalType": "Anonymous"}}}]}
    try:
        signed.client.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
        results["policy_applied"] = True
        results["after"] = obs(signed, unsigned)
    except ClientError as exc:
        results["policy_error_code"] = str(exc.response.get("Error", {}).get("Code", "unknown"))[:80]
        results["policy_http_status"] = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    results["status"] = "observed"
except Exception as exc:
    results["status"] = "inconclusive"
    results["failure_type"] = type(exc).__name__
    if isinstance(exc, ClientError):
        results["failure_code"] = str(exc.response.get("Error", {}).get("Code", "unknown"))[:80]
finally:
    if forward is not None:
        forward.terminate()
        with suppress(Exception):
            forward.wait(timeout=5)
    results["fixture_ready"] = (results.get("policy_applied") is True and results.get("after") ==
        {"authenticated_before": "matches", "anonymous": "denied", "authenticated_after": "matches"})
    results["fixture_id"] = suffix
    output = args.report
    with os.fdopen(report_fd, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(json.dumps(results, sort_keys=True))
    print("RESULT_FILE=" + output)

sys.exit(0 if results["fixture_ready"] else 1)
