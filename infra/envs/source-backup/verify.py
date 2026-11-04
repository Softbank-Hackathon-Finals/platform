#!/usr/bin/env python3
"""Read back the T35 source backup bucket and prove that non-custodians cannot delete.

Run as a custodian. The deletion probe writes one empty object under the registered
prefix, tries to delete it from short-lived Pods that use each registered ServiceAccount,
checks it is still there, then deletes it as the custodian. Real backups are never
touched. Prints a JSON summary without credentials.

Only an identity whose Pod ran as its registered role and got AccessDenied for both
deletes counts as verified. Missing ServiceAccounts, lookup or credential failures and
role mismatches are UNKNOWN, never a pass. Exit codes: 0 PASS, 1 FAIL, 2 UNKNOWN,
3 PARTIAL (before the restore Cluster's ServiceAccount exists).
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from string import Template

AWS_CLI = "docker.io/amazon/aws-cli:2.38.0@sha256:ed3233c28ad56d6a66e9e7623a495ab2d7eb08d425f8eea0fdbdfacb12de2ba8"
APP = ("source-dev", "demo-chat")
DELETE_ACTIONS = {"s3:DeleteObject", "s3:DeleteObjectVersion"}
PROTECT_ACTIONS = {"s3:DeleteBucket", "s3:DeleteBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketVersioning",
                   "s3:PutLifecycleConfiguration", "s3:PutEncryptionConfiguration",
                   "s3:PutBucketPublicAccessBlock", "s3:PutBucketOwnershipControls"}
ROLE_ARN = re.compile(r"arn:aws:iam::([0-9]{12}):role/(?:[A-Za-z0-9+=,.@_-]+/)*([A-Za-z0-9+=,.@_-]+)\Z")
ASSUMED = re.compile(r"arn:aws:sts::([0-9]{12}):assumed-role/([A-Za-z0-9+=,.@_-]+)/[A-Za-z0-9+=,.@_-]+\Z")
EXIT = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2, "PARTIAL": 3}
# Every run must show these were refused; the restore identity is required for the final PASS.
REQUIRED = ("writer", "app", "executor")


def run(command, check=True):
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{command[0]} {command[1]} failed; provider output suppressed")
    return result


def _set(value):
    return set(value) if isinstance(value, list) else {value}


def guarded_policy(policy, bucket, custodians):
    """The deployed policy must be exactly the stack's three Deny statements."""
    bucket_arn = Template("arn:aws:s3:::${bucket}").substitute(bucket=bucket)
    statements = policy.get("Statement") if isinstance(policy, dict) else None
    if not isinstance(statements, list) or not all(isinstance(s, dict) for s in statements):
        return False
    by_sid = {s.get("Sid"): s for s in statements}
    if len(by_sid) != len(statements) or set(by_sid) != {
            "DenyInsecureTransport", "DenyBackupDeletionExceptCustodians", "DenyProtectionChangesExceptCustodians"}:
        return False
    if any(s.get("Effect") != "Deny" or s.get("Principal") not in ("*", {"AWS": "*"}) or "NotAction" in s
           or "NotResource" in s or "NotPrincipal" in s for s in statements):
        return False

    def except_custodians(s):
        condition = s.get("Condition")
        return (isinstance(condition, dict) and set(condition) == {"ArnNotEquals"}
                and set(condition["ArnNotEquals"]) == {"aws:PrincipalArn"}
                and _set(condition["ArnNotEquals"]["aws:PrincipalArn"]) == set(custodians))

    tls, deletion, protect = (by_sid["DenyInsecureTransport"], by_sid["DenyBackupDeletionExceptCustodians"],
                              by_sid["DenyProtectionChangesExceptCustodians"])
    return (_set(tls.get("Action")) == {"s3:*"} and _set(tls.get("Resource")) == {bucket_arn, bucket_arn + "/*"}
            and tls.get("Condition") == {"Bool": {"aws:SecureTransport": "false"}}
            and _set(deletion.get("Action")) == DELETE_ACTIONS and _set(deletion.get("Resource")) == {bucket_arn + "/*"}
            and except_custodians(deletion)
            and _set(protect.get("Action")) == PROTECT_ACTIONS and _set(protect.get("Resource")) == {bucket_arn}
            and except_custodians(protect))


def readback(aws, reg):
    bucket = reg["bucket"]
    s3 = aws + ["s3api"]
    out = {}
    out["versioning_enabled"] = json.loads(run(s3 + ["get-bucket-versioning", "--bucket", bucket]).stdout).get("Status") == "Enabled"
    rule = json.loads(run(s3 + ["get-bucket-encryption", "--bucket", bucket]).stdout)[
        "ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]
    out["kms_with_stack_key"] = rule["SSEAlgorithm"] == "aws:kms" and rule.get("KMSMasterKeyID") == reg["kms_key_arn"]
    block = json.loads(run(s3 + ["get-public-access-block", "--bucket", bucket]).stdout)["PublicAccessBlockConfiguration"]
    out["public_access_blocked"] = all(block.values()) and len(block) == 4
    owner = json.loads(run(s3 + ["get-bucket-ownership-controls", "--bucket", bucket]).stdout)
    out["bucket_owner_enforced"] = owner["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"
    policy = json.loads(json.loads(run(s3 + ["get-bucket-policy", "--bucket", bucket]).stdout)["Policy"])
    out["policy_matches_stack"] = guarded_policy(policy, bucket, reg["custodians"])
    for key in ("writer", "reader"):
        role = reg[f"{key}_role_arn"].rsplit("/", 1)[1]
        trust = json.loads(run(aws + ["iam", "get-role", "--role-name", role]).stdout)["Role"]["AssumeRolePolicyDocument"]
        subjects = [v for s in trust["Statement"] for k, v in s["Condition"]["StringEquals"].items() if k.endswith(":sub")]
        sa = reg[key]
        out[f"{key}_trusts_one_service_account"] = subjects == [f"system:serviceaccount:{sa['namespace']}:{sa['name']}"]
        actions = []
        for name in json.loads(run(aws + ["iam", "list-role-policies", "--role-name", role]).stdout)["PolicyNames"]:
            doc = json.loads(run(aws + ["iam", "get-role-policy", "--role-name", role, "--policy-name", name]).stdout)
            actions += [a for s in doc["PolicyDocument"]["Statement"] for a in _set(s["Action"])]
        attached = json.loads(run(aws + ["iam", "list-attached-role-policies", "--role-name", role]).stdout)
        out[f"{key}_cannot_delete"] = not attached["AttachedPolicies"] and not any(
            "*" in a or a.startswith(("s3:Delete", "s3:PutBucket")) for a in actions)
    return out


def pod(namespace, service_account, name, region, bucket, key, version):
    script = ('echo "principal=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)"; '
              'aws s3api delete-object --bucket "$1" --key "$2" --version-id "$3" 2>&1; echo "version_exit=$?"; '
              'aws s3api delete-object --bucket "$1" --key "$2" 2>&1; echo "object_exit=$?"')
    restricted = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                  "capabilities": {"drop": ["ALL"]}}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace, "labels": {"component": "source", "app": "t35-delete-probe"}},
            "spec": {"serviceAccountName": service_account, "restartPolicy": "Never",
                     "activeDeadlineSeconds": 120,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 65534, "runAsGroup": 65534,
                                         "fsGroup": 65534, "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "probe", "image": AWS_CLI, "command": ["sh", "-c", script, "sh"],
                                     "args": [bucket, key, version],
                                     "env": [{"name": "HOME", "value": "/tmp"}, {"name": "AWS_REGION", "value": region}],
                                     "resources": {"requests": {"cpu": "10m", "memory": "64Mi"},
                                                   "limits": {"cpu": "200m", "memory": "256Mi"}},
                                     "securityContext": restricted,
                                     "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
                     "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}]}}


def same_role(principal, role_arn):
    """An assumed-role session of exactly the registered role (same account and role name)."""
    used, expected = ASSUMED.fullmatch(principal or ""), ROLE_ARN.fullmatch(role_arn or "")
    return bool(used and expected and used.groups() == expected.groups())


def verdict(log, role_arn):
    lines = log.splitlines()
    principal = next((line.split("=", 1)[1] for line in lines if line.startswith("principal=")), "")
    exits = dict(line.split("=", 1) for line in lines if line.startswith(("version_exit=", "object_exit=")))
    if set(exits) != {"version_exit", "object_exit"}:
        return "UNKNOWN_UNCLEAR"
    if "0" in exits.values():
        return "DELETED"
    if not principal or principal == "None":
        return "UNKNOWN_NO_CREDENTIALS"
    if not same_role(principal, role_arn):
        return "UNKNOWN_ROLE_MISMATCH"
    if sum("(AccessDenied)" in line for line in lines) == 2:
        return "DENIED"
    return "UNKNOWN_UNCLEAR"


def service_account(kube, namespace, name):
    result = run(kube + ["--namespace", namespace, "get", "serviceaccount", name], check=False)
    if result.returncode == 0:
        return None
    return "UNKNOWN_SA_NOT_FOUND" if "NotFound" in result.stderr else "UNKNOWN_SA_LOOKUP_FAILED"


def probe(aws, kube, reg, subjects):
    """subjects: {label: (namespace, ServiceAccount, registered role ARN)}."""
    bucket, key = reg["bucket"], reg["destination"].split("/", 3)[3] + f"_t35-delete-probe/{uuid.uuid4()}"
    with tempfile.NamedTemporaryFile() as empty:
        version = json.loads(run(aws + ["s3api", "put-object", "--bucket", bucket, "--key", key,
                                        "--body", empty.name]).stdout)["VersionId"]
    out = {}
    try:
        for label, (namespace, sa, role_arn) in subjects.items():
            missing = service_account(kube, namespace, sa)
            if missing:
                out[label] = missing
                continue
            name = f"t35-delete-probe-{uuid.uuid4().hex[:8]}"
            spec = pod(namespace, sa, name, reg["region"], bucket, key, version)
            created = subprocess.run(kube + ["create", "-f", "-"], input=json.dumps(spec), capture_output=True,
                                     text=True, timeout=60, check=False)
            if created.returncode:
                out[label] = "UNKNOWN_POD_NOT_CREATED"
                continue
            try:
                deadline = time.monotonic() + 150
                phase = ""
                while time.monotonic() < deadline and phase not in ("Succeeded", "Failed"):
                    time.sleep(3)
                    phase = run(kube + ["--namespace", namespace, "get", "pod", name, "-o",
                                        "jsonpath={.status.phase}"], check=False).stdout
                log = run(kube + ["--namespace", namespace, "logs", name], check=False).stdout
                out[label] = verdict(log, role_arn)
            finally:
                run(kube + ["--namespace", namespace, "delete", "pod", name, "--wait=false"], check=False)
        head = run(aws + ["s3api", "head-object", "--bucket", bucket, "--key", key, "--version-id", version], check=False)
        out["probe_version_intact"] = head.returncode == 0
        listing = json.loads(run(aws + ["s3api", "list-object-versions", "--bucket", bucket, "--prefix", key]).stdout)
        out["no_delete_marker"] = not listing.get("DeleteMarkers")
    finally:
        # The custodian removes the probe's versions and any marker; nothing else under the prefix.
        listing = json.loads(run(aws + ["s3api", "list-object-versions", "--bucket", bucket, "--prefix", key]).stdout)
        removed = True
        for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            if item["Key"] == key:
                removed &= run(aws + ["s3api", "delete-object", "--bucket", bucket, "--key", key,
                                      "--version-id", item["VersionId"]], check=False).returncode == 0
        out["custodian_removed_probe"] = removed
    return out


def status(readback_result, probe_result, stage):
    """FAIL on any deletion or wrong configuration; PASS only when every required identity was verified."""
    if not all(v is True for v in readback_result.values()):
        return "FAIL"
    if probe_result is None:
        return "UNKNOWN"  # configuration alone does not show that deletion is refused
    if ("DELETED" in probe_result.values() or probe_result.get("probe_version_intact") is False
            or probe_result.get("no_delete_marker") is False):
        return "FAIL"
    if probe_result.get("custodian_removed_probe") is not True:
        return "UNKNOWN"
    identities = {k: v for k, v in probe_result.items() if k not in
                  ("probe_version_intact", "no_delete_marker", "custodian_removed_probe")}
    if any(identities.get(k) != "DENIED" for k in REQUIRED) or any(
            v != "DENIED" for k, v in identities.items() if k != "reader"):
        return "UNKNOWN"
    if identities.get("reader") == "DENIED":
        return "PASS"
    if stage == "before-restore" and identities.get("reader") == "UNKNOWN_SA_NOT_FOUND":
        return "PARTIAL"
    return "UNKNOWN"


def _identity(value):
    """NAMESPACE/SERVICEACCOUNT=ROLE_ARN"""
    try:
        target, role = value.split("=", 1)
        namespace, sa = target.split("/", 1)
    except ValueError:
        raise argparse.ArgumentTypeError("use NAMESPACE/SERVICEACCOUNT=ROLE_ARN") from None
    if not namespace or not sa or not ROLE_ARN.fullmatch(role):
        raise argparse.ArgumentTypeError("use NAMESPACE/SERVICEACCOUNT=ROLE_ARN with an exact role ARN")
    return namespace, sa, role


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True, type=argparse.FileType(),
                        help="terraform output -json backup_registration")
    parser.add_argument("--kubeconfig", help="source cluster; required with --delete-probe")
    parser.add_argument("--delete-probe", action="store_true")
    parser.add_argument("--stage", choices=["before-restore", "final"], default="final",
                        help="before-restore reports PARTIAL while the restore ServiceAccount does not exist")
    parser.add_argument("--app-role-arn", help="source-workloads app role (workload_metadata.app_role_arn)")
    parser.add_argument("--executor", type=_identity, metavar="NAMESPACE/SERVICEACCOUNT=ROLE_ARN",
                        help="the Executor identity and the role its Pods run as; without it the run stays UNKNOWN")
    parser.add_argument("--also-deny", action="append", default=[], type=_identity,
                        metavar="NAMESPACE/SERVICEACCOUNT=ROLE_ARN",
                        help="another identity that must not delete and the role its Pods run as")
    args = parser.parse_args(argv)
    reg = json.load(args.registration)
    aws = ["aws", "--region", reg["region"], "--no-cli-pager", "--output", "json"]
    result = {"readback": readback(aws, reg)}
    probe_result = None
    if args.delete_probe:
        if not args.kubeconfig or not args.app_role_arn or not ROLE_ARN.fullmatch(args.app_role_arn):
            parser.error("--delete-probe needs --kubeconfig and the exact --app-role-arn")
        subjects = {"writer": (reg["writer"]["namespace"], reg["writer"]["name"], reg["writer_role_arn"]),
                    "reader": (reg["reader"]["namespace"], reg["reader"]["name"], reg["reader_role_arn"]),
                    "app": (*APP, args.app_role_arn)}
        if args.executor:
            subjects["executor"] = args.executor
        subjects.update({f"extra:{ns}/{sa}": (ns, sa, role) for ns, sa, role in args.also_deny})
        probe_result = result["delete_probe"] = probe(aws, ["kubectl", "--kubeconfig", args.kubeconfig], reg, subjects)
        if not args.executor:
            probe_result["executor"] = "UNKNOWN_NOT_REGISTERED"
    result["status"] = status(result["readback"], probe_result, args.stage)
    json.dump(result, sys.stdout, indent=2)
    print()
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
