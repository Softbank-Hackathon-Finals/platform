"""Human operator SSM entrypoint. Uses existing AWS authentication; no new keys or IAM grants."""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

SCRIPTS = {"mount": "mount_volume.py", "database": "apply_host.py",
           "probe": "run_probe_host.py", "identities": "verify_platform_host.py",
           "cleanup": "cleanup_probes.py", "runtime-path": "verify_runtime_path.py"}


def aws(region, service, operation, data):
    result = subprocess.run(["aws", service, operation, "--region", region, "--output", "json",
        "--no-cli-pager", "--cli-connect-timeout", "10", "--cli-read-timeout", "30",
        "--cli-input-json", json.dumps(data)], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("aws_operation_failed")
    return json.loads(result.stdout)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["submit", "poll"])
    p.add_argument("--phase", choices=SCRIPTS)
    p.add_argument("--name", required=True)
    p.add_argument("--config", type=Path, required=True, help="private expected_account/region/instance_id/vpc_id mapping")
    p.add_argument("--payload", type=Path, help="non-secret reviewed deployment input")
    p.add_argument("--receipts", type=Path, required=True, help="private directory outside this checkout")
    args = p.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,64}", args.name):
        raise ValueError("invalid_operation_name")
    repo = Path(__file__).resolve().parents[3]
    if args.receipts.resolve() == repo or repo in args.receipts.resolve().parents:
        raise ValueError("private_receipts_must_be_outside_checkout")
    os.umask(0o077)
    args.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = json.loads(args.config.read_text())
    region = config["region"]
    identity = aws(region, "sts", "get-caller-identity", {})
    if identity["Account"] != config["expected_account"]:
        raise RuntimeError("account_mismatch")
    path = args.receipts / (args.name + "-command.private.json")
    if args.action == "poll":
        receipt = json.loads(path.read_text())
        if receipt["instance_id"] != config["instance_id"]:
            raise RuntimeError("receipt_target_mismatch")
        result = aws(region, "ssm", "get-command-invocation", {"CommandId": receipt["command_id"], "InstanceId": config["instance_id"]})
        (args.receipts / (args.name + "-invocation.private.json")).write_text(json.dumps(result))
        print(json.dumps({"status": result["Status"], "response_code": result["ResponseCode"], "source_sha256": receipt["source_sha256"]}))
        return
    if not args.phase or path.exists():
        raise RuntimeError("phase_missing_or_receipt_exists_poll_instead")
    inventory = aws(region, "ec2", "describe-instances", {"InstanceIds": [config["instance_id"]]})
    instances = [i for reservation in inventory["Reservations"] for i in reservation["Instances"]]
    if len(instances) != 1:
        raise RuntimeError("instance_ambiguous")
    instance = instances[0]
    tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
    if (instance["VpcId"] != config["vpc_id"] or instance["State"]["Name"] != "running" or instance.get("PublicIpAddress") or
        any(tags.get(k) != v for k, v in {"project": "bravo-platform", "component": "platform", "managed-by": "terraform"}.items())):
        raise RuntimeError("target_ownership_or_exposure_mismatch")
    source = Path(__file__).with_name(SCRIPTS[args.phase]).read_text()
    payload = args.payload.read_text() if args.payload else "{}"
    json.loads(payload)
    # JSON is stdin, never interpolated into Python/shell source. No literal heredoc delimiter can occur in compact JSON.
    payload = json.dumps(json.loads(payload))
    command = "python3 -c " + shlex.quote(source) + " <<'BRAVO_OPERATOR_INPUT'\n" + payload + "\nBRAVO_OPERATOR_INPUT"
    if len(command.encode()) > 64000:
        raise ValueError("operation_payload_too_large")
    result = aws(region, "ssm", "send-command", {"DocumentName": "AWS-RunShellScript", "InstanceIds": [config["instance_id"]],
        "Comment": "T7 reviewed platform data operation " + args.phase, "TimeoutSeconds": 120,
        "Parameters": {"commands": [command], "executionTimeout": ["1800"]}})
    receipt = {"command_id": result["Command"]["CommandId"], "instance_id": config["instance_id"],
               "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "payload_sha256": hashlib.sha256(payload.encode()).hexdigest()}
    path.write_text(json.dumps(receipt))
    print(json.dumps({"submitted": True, "source_sha256": receipt["source_sha256"], "target_boundary_checked": True}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "details_suppressed": True}))
        raise SystemExit(1) from None
