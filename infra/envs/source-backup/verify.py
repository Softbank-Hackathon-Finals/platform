#!/usr/bin/env python3
"""Read back the T35 source backup bucket and prove that non-custodians cannot delete.

Run as a custodian. The deletion probe writes one empty object under the registered
prefix, tries to delete it from short-lived Pods that use each workload ServiceAccount,
checks it is still there, then deletes it as the custodian. Real backups are never
touched. Prints a JSON summary without credentials.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid

AWS_CLI = "docker.io/amazon/aws-cli:2.38.0@sha256:ed3233c28ad56d6a66e9e7623a495ab2d7eb08d425f8eea0fdbdfacb12de2ba8"
APP = ("source-dev", "demo-chat")


def run(command, check=True):
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{command[0]} {command[1]} failed; provider output suppressed")
    return result


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
    guarded = [s for s in policy["Statement"] if s["Sid"].endswith("ExceptCustodians")]
    out["only_custodians_delete"] = len(guarded) == 2 and all(
        s["Effect"] == "Deny" and sorted(_list(s["Condition"]["ArnNotEquals"]["aws:PrincipalArn"])) == sorted(reg["custodians"])
        for s in guarded)
    for key in ("writer", "reader"):
        role = reg[f"{key}_role_arn"].rsplit("/", 1)[1]
        trust = json.loads(run(aws + ["iam", "get-role", "--role-name", role]).stdout)["Role"]["AssumeRolePolicyDocument"]
        subjects = [v for s in trust["Statement"] for k, v in s["Condition"]["StringEquals"].items() if k.endswith(":sub")]
        sa = reg[key]
        out[f"{key}_trusts_one_service_account"] = subjects == [f"system:serviceaccount:{sa['namespace']}:{sa['name']}"]
        actions = []
        for name in json.loads(run(aws + ["iam", "list-role-policies", "--role-name", role]).stdout)["PolicyNames"]:
            doc = json.loads(run(aws + ["iam", "get-role-policy", "--role-name", role, "--policy-name", name]).stdout)
            actions += [a for s in doc["PolicyDocument"]["Statement"] for a in _list(s["Action"])]
        attached = json.loads(run(aws + ["iam", "list-attached-role-policies", "--role-name", role]).stdout)
        out[f"{key}_cannot_delete"] = not attached["AttachedPolicies"] and not any(
            "*" in a or a.startswith(("s3:Delete", "s3:PutBucket")) for a in actions)
    return out


def _list(value):
    return value if isinstance(value, list) else [value]


def pod(namespace, service_account, name, region, bucket, key, version):
    script = ('aws s3api delete-object --bucket "$1" --key "$2" --version-id "$3" 2>&1; echo "version_exit=$?"; '
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


def verdict(log):
    """Both deletes must fail with AccessDenied. A Pod without IRSA may fall back to the node role,
    which the bucket policy also denies. Any other failure is not evidence."""
    lines = log.splitlines()
    exits = dict(line.split("=", 1) for line in lines if line.startswith(("version_exit=", "object_exit=")))
    if set(exits) != {"version_exit", "object_exit"}:
        return "UNCLEAR"
    if "0" in exits.values():
        return "DELETED"
    if sum("(AccessDenied)" in line for line in lines) == 2:
        return "DENIED"
    if sum("Unable to locate credentials" in line for line in lines) == 2:
        return "NO_AWS_CREDENTIALS"
    return "UNCLEAR"


def probe(aws, kube, reg, extra):
    bucket, key = reg["bucket"], reg["destination"].split("/", 3)[3] + f"_t35-delete-probe/{uuid.uuid4()}"
    with tempfile.NamedTemporaryFile() as empty:
        version = json.loads(run(aws + ["s3api", "put-object", "--bucket", bucket, "--key", key,
                                        "--body", empty.name]).stdout)["VersionId"]
    subjects = {"writer": (reg["writer"]["namespace"], reg["writer"]["name"]),
                "reader": (reg["reader"]["namespace"], reg["reader"]["name"]), "app": APP}
    subjects.update({f"extra:{ns}/{sa}": (ns, sa) for ns, sa in extra})
    out = {}
    try:
        for label, (namespace, sa) in subjects.items():
            if run(kube + ["--namespace", namespace, "get", "serviceaccount", sa], check=False).returncode:
                out[label] = "SKIPPED_NO_SERVICE_ACCOUNT"
                continue
            name = f"t35-delete-probe-{uuid.uuid4().hex[:8]}"
            spec = pod(namespace, sa, name, reg["region"], bucket, key, version)
            subprocess.run(kube + ["apply", "-f", "-"], input=json.dumps(spec), capture_output=True,
                           text=True, timeout=60, check=True)
            try:
                deadline = time.monotonic() + 150
                phase = ""
                while time.monotonic() < deadline and phase not in ("Succeeded", "Failed"):
                    time.sleep(3)
                    phase = run(kube + ["--namespace", namespace, "get", "pod", name, "-o",
                                        "jsonpath={.status.phase}"], check=False).stdout
                log = run(kube + ["--namespace", namespace, "logs", name], check=False).stdout
                out[label] = verdict(log)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True, type=argparse.FileType(),
                        help="terraform output -json backup_registration")
    parser.add_argument("--kubeconfig", help="source cluster; required with --delete-probe")
    parser.add_argument("--delete-probe", action="store_true")
    parser.add_argument("--also-deny", action="append", default=[], metavar="NAMESPACE/SERVICEACCOUNT",
                        help="another workload identity that must not delete, e.g. an Executor")
    args = parser.parse_args(argv)
    reg = json.load(args.registration)
    aws = ["aws", "--region", reg["region"], "--no-cli-pager", "--output", "json"]
    result = {"readback": readback(aws, reg)}
    if args.delete_probe:
        if not args.kubeconfig:
            parser.error("--delete-probe needs --kubeconfig")
        extra = [tuple(item.split("/", 1)) for item in args.also_deny]
        result["delete_probe"] = probe(aws, ["kubectl", "--kubeconfig", args.kubeconfig], reg, extra)
    # A ServiceAccount that does not exist yet (the restore Cluster's) is reported, not counted.
    acceptable = (True, "DENIED", "NO_AWS_CREDENTIALS", "SKIPPED_NO_SERVICE_ACCOUNT")
    result["passed"] = all(v is True for v in result["readback"].values()) and all(
        v in acceptable for v in result.get("delete_probe", {}).values())
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
