#!/usr/bin/env python3
"""Request fixed object operations with personal auth; storage keys stay in the worker."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from object_operations.client import APIError, Client
from object_operations.install import NAME, NAMESPACE, ROOT, manifests


class Failure(Exception):
    pass


def kubectl(*args, value=None):
    result = subprocess.run(["kubectl", *args], input=None if value is None else json.dumps(value),
                            capture_output=True, text=True, check=False, timeout=30)
    if result.returncode:
        raise Failure("kubernetes_command_failed")
    return result.stdout


def state():
    doc = json.loads(kubectl("get", "configmap", NAME, "-n", NAMESPACE, "-o", "json"))
    return doc, json.loads(doc["data"]["state.json"])


def save(doc, value):
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": doc["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": doc["metadata"]["resourceVersion"]},
        {"op": "replace", "path": "/data/state.json", "value": json.dumps(value)},
    ]
    kubectl("patch", "configmap", NAME, "-n", NAMESPACE, "--type=json", "--patch", json.dumps(patch))


def execute(args):
    subject = json.loads(kubectl("auth", "whoami", "-o", "json"))["status"]["userInfo"]["username"]
    if not re.fullmatch(r"system:serviceaccount:bravo-team-access:member-0[1-5]", subject):
        raise Failure("personal_member_required")
    cluster_uid = kubectl("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}")
    if args.command == "install":
        if cluster_uid != args.cluster_uid:
            raise Failure("cluster_uid_mismatch")
        # kubectl receives the Secret response in memory; only its UID is emitted.
        # This installer-only read is never exported as a Secret file or run input.
        secret_uid = kubectl("get", "secret", "target-seaweedfs-auth", "-n", NAMESPACE, "-o", "jsonpath={.metadata.uid}")
        deployment_uid = kubectl("get", "deploy", "target-seaweedfs", "-n", NAMESPACE, "-o", "jsonpath={.metadata.uid}")
        objects, initial = manifests(cluster_uid, secret_uid, deployment_uid)
        existing = kubectl("get", "configmap", NAME, "-n", NAMESPACE, "--ignore-not-found", "-o", "json")
        if existing:
            value = json.loads(json.loads(existing)["data"]["state.json"])
            if value["phase"] in ("PENDING", "RUNNING", "CLOSING", "RECOVERY_REQUIRED"):
                raise Failure("active_operation_blocks_install")
            if any(value["config"][key] != initial_value for key, initial_value in json.loads(initial["data"]["state.json"])["config"].items() if key != "source_sha256"):
                raise Failure("installed_target_binding_changed")
        else:
            kubectl("create", "-f", "-", value=initial)
        kubectl("apply", "-f", "-", value={"apiVersion": "v1", "kind": "List", "items": objects})
        doc, value = state()
        value["config"]["source_sha256"] = json.loads(initial["data"]["state.json"])["config"]["source_sha256"]
        save(doc, value)
        print(json.dumps({"installed": True, "cluster_uid": cluster_uid, "credentials_distributed": False}))
        return 0
    settings = json.loads(args.access.read_text())
    if cluster_uid != settings["cluster_uid"]:
        raise Failure("cluster_uid_mismatch")
    api = Client(settings)
    if args.command == "state":
        print(json.dumps(api.call("GET", route="/v1/object-exposure/status"), ensure_ascii=False))
        return 0
    if args.command in ("status", "cleanup"):
        if not args.run_id:
            entries = api.call("GET")["runs"]
            if not entries:
                raise Failure("own_run_not_found")
            args.run_id = entries[0]["run_id"]
        result = api.call("POST", "/" + args.run_id + "/cancel", {}) if args.command == "cleanup" else api.call("GET", "/" + args.run_id)
    else:
        key = args.request_key or api.new_key()
        args.run_id = api.run_id(subject, key)
        print(json.dumps({"run_id": args.run_id, "request_key": key, "submission": "SENDING"}), flush=True)
        result = api.call("POST", body={"action": args.command, "hold_seconds": args.hold_seconds,
                                      "acknowledge_anonymous_read": args.acknowledge_anonymous_read}, key=key)
    deadline = time.monotonic() + args.wait_seconds
    while result["phase"] != "FINISHED" and time.monotonic() < deadline:
        time.sleep(3)
        result = api.call("GET", "/" + args.run_id)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.command == "status":
        return 3 if args.wait_seconds and result["phase"] != "FINISHED" else 0
    if result["phase"] != "FINISHED":
        return 3 if args.wait_seconds or result["phase"] == "RECOVERY_REQUIRED" else 0
    if args.command == "cleanup" and result["phase"] == "FINISHED":
        return 0 if result.get("cleanup") in ("CLEAN", "NOT_APPLICABLE") else 1
    return 0 if result.get("verdict") == "PASS" and result.get("cleanup") in ("CLEAN", "NOT_APPLICABLE") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "check", "cycle", "open", "close", "state", "status", "cleanup"))
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--cluster-uid")
    parser.add_argument("--run-id")
    parser.add_argument("--request-key", help="Reuse only for the exact same HTTP submission; printed before sending")
    parser.add_argument("--hold-seconds", type=int, default=15)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--acknowledge-anonymous-read", action="store_true")
    parser.add_argument("--authenticated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.authenticated:
        if not os.environ.get("BRAVO_TEAM_DIRECT_TOKEN"):
            raise Failure("personal_wrapper_required")
        return execute(args)
    return subprocess.run([sys.executable, str(ROOT / "infra/envs/rehearsal-access/team_test.py"),
                           "--profile", str(args.access.resolve()), "--", sys.executable,
                           str(Path(__file__).resolve()), *sys.argv[1:], "--authenticated"], check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Failure, APIError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"error": str(error) if isinstance(error, (Failure, APIError)) else type(error).__name__, "replayed": False}), file=sys.stderr)
        raise SystemExit(2) from None
