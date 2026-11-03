"""Run a fixed, read-only T7 host probe through authenticated SSM."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


class Failure(RuntimeError):
    pass


def aws(service, operation, parameters, region):
    args = ["aws", service, operation, "--region", region, "--output", "json", "--no-cli-pager",
            "--cli-connect-timeout", "10", "--cli-read-timeout", "30", "--cli-input-json", json.dumps(parameters)]
    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "AWS_MAX_ATTEMPTS": "1"})
    except (OSError, subprocess.TimeoutExpired):
        raise Failure("aws_unavailable") from None
    if result.returncode:
        match = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
        raise Failure(match[1] if match else "aws_failed")
    return json.loads(result.stdout)


def validate_instance(instance, config):
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    if instance.get("InstanceId") != config["instance_id"] or instance.get("VpcId") != config["vpc_id"]:
        raise Failure("instance_or_vpc_mismatch")
    if any(tags.get(k) != v for k, v in {"project": "bravo-platform", "component": "platform", "managed-by": "terraform"}.items()):
        raise Failure("owner_tags_mismatch")
    if not instance.get("IamInstanceProfile", {}).get("Arn", "").endswith(":instance-profile/bravo-platform-runtime"):
        raise Failure("instance_profile_mismatch")
    if instance.get("PublicIpAddress") or instance.get("State", {}).get("Name") != "running":
        raise Failure("instance_state_or_exposure_mismatch")


def validate_host(body):
    boolean_fields = {"bootstrap_marker_exists", "rke2_service_active", "cis_profile_configured", "kubeconfig_owner_only", "api_ready", "nodes_query_succeeded", "system_pods_query_succeeded", "human_kubernetes_login_verified", "ghcr_image_pull_verified", "cis_benchmark_verified"}
    count_fields = {"node_count", "ready_node_count", "system_pod_count", "system_pods_pending_or_failed"}
    rbac_fields = {"smoke_can_get_secret", "smoke_can_create_configmap", "smoke_can_write_other_namespace"}
    expected = boolean_fields | count_fields | rbac_fields | {"schema_version", "cloud_init_status", "rke2_version", "ghcr_http_status"}
    if set(body) != expected or type(body.get("schema_version")) is not int or body["schema_version"] != 1:
        raise Failure("host_receipt_schema_mismatch")
    if any(type(body[k]) is not bool for k in boolean_fields):
        raise Failure("host_receipt_boolean_invalid")
    if any(type(body[k]) is not int or not 0 <= body[k] <= 100000 for k in count_fields):
        raise Failure("host_receipt_count_invalid")
    if any(body[k] not in ("yes", "no", "UNKNOWN") for k in rbac_fields):
        raise Failure("host_receipt_rbac_invalid")
    if body["cloud_init_status"] not in ("done", "running", "error", "disabled", "not run", "UNKNOWN"):
        raise Failure("host_receipt_cloud_init_invalid")
    if body["rke2_version"] is not None and not re.fullmatch(r"v\d+\.\d+\.\d+\+rke2r\d+", body["rke2_version"]):
        raise Failure("host_receipt_version_invalid")
    if body["ghcr_http_status"] is not None and not re.fullmatch(r"\d{3}", body["ghcr_http_status"]):
        raise Failure("host_receipt_registry_invalid")
    if any(body[k] for k in ("human_kubernetes_login_verified", "ghcr_image_pull_verified", "cis_benchmark_verified")):
        raise Failure("host_receipt_overclaims_verification")


def operator_path_ready(body):
    required = ("bootstrap_marker_exists", "rke2_service_active", "cis_profile_configured", "kubeconfig_owner_only", "api_ready", "nodes_query_succeeded", "system_pods_query_succeeded")
    return (all(body[k] for k in required) and body["cloud_init_status"] == "done" and body["node_count"] > 0
            and body["ready_node_count"] == body["node_count"] and body["system_pods_pending_or_failed"] == 0
            and body["smoke_can_get_secret"] == "no" and body["smoke_can_create_configmap"] == "yes"
            and body["smoke_can_write_other_namespace"] == "no")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="private account/instance/VPC mapping")
    parser.add_argument("--private-receipt", type=Path, required=True, help="SSM receipt outside the repository")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = json.loads(args.config.read_text())
        if Path(__file__).resolve().parents[1] in args.private_receipt.resolve().parents:
            raise Failure("private_receipt_must_be_outside_repository")
        if args.private_receipt.exists():
            raise Failure("private_receipt_exists_choose_new_path")
        if not re.fullmatch(r"\d{12}", config["expected_account"]):
            raise Failure("expected_account_invalid")
        region = config.get("region", "ap-northeast-2")
        identity = aws("sts", "get-caller-identity", {}, region)
        if identity.get("Account") != config["expected_account"]:
            raise Failure("account_boundary_mismatch")
        response = aws("ec2", "describe-instances", {"InstanceIds": [config["instance_id"]]}, region)
        instances = [i for r in response["Reservations"] for i in r["Instances"]]
        if len(instances) != 1:
            raise Failure("instance_inventory_incomplete")
        validate_instance(instances[0], config)
        source = Path(__file__).with_name("t7_runtime_checks.py").read_text()
        sent = aws("ssm", "send-command", {"DocumentName": "AWS-RunShellScript", "InstanceIds": [config["instance_id"]],
                   "Comment": "T7 read-only readiness verification; no credentials or mutation", "TimeoutSeconds": 120,
                   "Parameters": {"commands": ["python3 - <<'BRAVO_READ_ONLY_PROBE'\n" + source + "\nBRAVO_READ_ONLY_PROBE"],
                                  "executionTimeout": ["120"]}}, region)
        command_id = sent["Command"]["CommandId"]
        args.private_receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.private_receipt.write_text(json.dumps({"command_id": command_id, "instance_id": config["instance_id"], "region": region}))
        os.chmod(args.private_receipt, 0o600)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            try:
                result = aws("ssm", "get-command-invocation", {"CommandId": command_id, "InstanceId": config["instance_id"]}, region)
            except Failure as error:
                if str(error) != "InvocationDoesNotExist":
                    raise
                time.sleep(2)
                continue
            if result["Status"] in ("Pending", "InProgress", "Delayed"):
                time.sleep(2)
                continue
            if result["Status"] != "Success" or result.get("ResponseCode") != 0:
                raise Failure("ssm_probe_failed")
            body = json.loads(result["StandardOutputContent"])
            # Validate every scalar before emitting it; raw SSM output is private.
            validate_host(body)
            public = {"observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "account_boundary_matches": True,
                      "target_owner_boundary_matches": True, "ssm_status": "Success", "ssm_response_code": 0,
                      "probe_sha256": hashlib.sha256(source.encode()).hexdigest(), "host": body,
                      "operator_readiness": "PASS" if operator_path_ready(body) else "NOT_READY"}
            print(json.dumps(public, indent=2))
            return 0 if operator_path_ready(body) else 2
        raise Failure("ssm_wait_timeout_requery_private_receipt")
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        code = str(error) if isinstance(error, Failure) else "local_or_receipt_invalid"
        print(json.dumps({"error_code": code, "readiness": "NOT_VERIFIED"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
