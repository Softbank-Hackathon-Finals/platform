"""Actions OIDC -> fixed SSM broker -> bounded inference. No persisted credentials."""

import json
import os
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from bravo_agent.contracts import VerificationReceipt, digest
from bravo_agent.verification import validate_result


def accept_verification(result, verification):
    bundle, proposal = validate_result(result["result"])
    receipt = VerificationReceipt.model_validate(verification["receipt"])
    evidence = verification["evidence"]
    checks = evidence.get("checks", {})
    if (verification.get("state") != "AWAITING_APPROVAL"
            or receipt.run_id != bundle.run_id or receipt.source_sha != bundle.source_sha
            or receipt.proposal_digest != digest(proposal) or receipt.attempt != result["result"]["attempt"]
            or receipt.verdict != "PASS" or not receipt.effects_known
            or receipt.evidence_digest != digest(evidence)
            or evidence.get("namespace_deleted") is not True or evidence.get("job_complete") is not True
            or evidence.get("failure_code") != "NONE"
            or set(checks) != {"injected_value", "missing_value_closed"}
            or not all(value is True for value in checks.values())):
        raise ValueError("verification does not prove this proposal")


def prove_denied(call):
    try:
        call()
    except ClientError as error:
        if error.response["Error"]["Code"] in {"AccessDenied", "AccessDeniedException"}:
            return True
        raise
    raise RuntimeError("permission boundary was not enforced")


def direct_broker_denied(sts, role_arn, token):
    # Use the same token for the subsequent VM call. Scope any unexpectedly
    # issued probe session down to zero permissions.
    return prove_denied(lambda: sts.assume_role_with_web_identity(
        RoleArn=role_arn, RoleSessionName="BravoBoundaryProbe", WebIdentityToken=token,
        DurationSeconds=900,
        Policy=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Deny", "Action": "*", "Resource": "*"}]})))


def prove_session_denied(ssm, **arguments):
    try:
        created = ssm.start_session(**arguments)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"AccessDenied", "AccessDeniedException"}:
            return True
        raise
    # Do not open a channel or run a command if a negative probe is accepted.
    # A denied cleanup request is not evidence that StartSession was denied.
    ssm.terminate_session(SessionId=created["SessionId"])
    raise RuntimeError("session permission boundary was not enforced")


def main():
    region = "ap-northeast-2"
    instance = os.environ["BRAVO_INSTANCE_ID"]
    ssm = boto3.client("ssm", region_name=region)
    session = None
    process = None
    try:
        denied = {
            "arbitrary_shell": prove_denied(lambda: ssm.send_command(
                InstanceIds=[instance], DocumentName="AWS-RunShellScript",
                Parameters={"commands": ["true"]})),
            "secret_inventory": prove_denied(lambda: boto3.client("secretsmanager", region_name=region).list_secrets()),
            "default_shell_session": prove_session_denied(ssm, Target=instance),
            "arbitrary_port_session": prove_session_denied(
                ssm, Target=instance, DocumentName="AWS-StartPortForwardingSession",
                Parameters={"portNumber": ["22"], "localPortNumber": ["18763"]}),
        }
        command = ssm.send_command(InstanceIds=[instance], DocumentName="bravo-subscription-service",
                                   Parameters={"operation": ["start"]})["Command"]["CommandId"]
        ssm.get_waiter("command_executed").wait(CommandId=command, InstanceId=instance,
                                              WaiterConfig={"Delay": 2, "MaxAttempts": 30})
        session = ssm.start_session(Target=instance, DocumentName="bravo-subscription-tunnel",
                                    Parameters={"localPortNumber": ["18762"]})
        process = subprocess.Popen(["session-manager-plugin", json.dumps(session), region,
                                    "StartSession", "", json.dumps({"Target": instance}),
                                    f"https://ssm.{region}.amazonaws.com"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import socket
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", 18762), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("tunnel unavailable")
        results = []
        for _ in range(2):
            run_id = "subscription-" + uuid.uuid4().hex
            url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com"
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
            with urllib.request.urlopen(request, timeout=20) as response:
                token = json.load(response)["value"]
            if not results:
                denied["direct_broker_role"] = direct_broker_denied(
                    boto3.client("sts", region_name=region), os.environ["BRAVO_BROKER_ROLE_ARN"], token)
                print("Direct broker role request from runner: AccessDenied", flush=True)
            body = json.dumps({"web_identity_token": token, "run_id": run_id}).encode()
            request = urllib.request.Request("http://127.0.0.1:18762/validation/fixture", data=body,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=260) as response:
                result = json.load(response)
            results.append(result)
            if (result.get("state") != "PROPOSAL_READY" or result.get("active_after") is not True
                    or result.get("exit_code") != 0
                    or result.get("runtime_source_sha") != os.environ["BRAVO_RUNTIME_SOURCE_SHA"]):
                raise RuntimeError("broker did not produce a proposal")
            if result["result"].get("input_kind") != "synthetic_db_host_fixture":
                raise ValueError("broker did not identify the validation fixture")
            bundle, proposal = validate_result(result["result"])
            if bundle.run_id != run_id:
                raise ValueError("broker returned a different execution")
            command = ssm.send_command(InstanceIds=[instance], DocumentName="bravo-subscription-service",
                                       Parameters={"operation": ["verify"], "runId": [run_id],
                                                   "proposalDigest": [digest(proposal)]})["Command"]["CommandId"]
            ssm.get_waiter("command_executed").wait(CommandId=command, InstanceId=instance,
                                                  WaiterConfig={"Delay": 2, "MaxAttempts": 180})
            verification = json.loads(ssm.get_command_invocation(CommandId=command, InstanceId=instance)["StandardOutputContent"])
            result["verification"] = verification
            try:
                accept_verification(result, verification)
            except (KeyError, TypeError, ValueError):
                Path("runtime-result.json").write_text(json.dumps({"denied": denied, "runs": results}, indent=2) + "\n")
                raise RuntimeError("independent verification did not pass") from None
        Path("runtime-result.json").write_text(json.dumps({"denied": denied, "runs": results}, indent=2) + "\n")
        print(json.dumps({"runs": len(results), "states": [r["state"] for r in results],
                          "credential_rotated": any(r["credential_rotated"] for r in results),
                          "registration_reused": all(r["registration_reused"] for r in results)}))
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if session:
            ssm.terminate_session(SessionId=session["SessionId"])
        command = ssm.send_command(InstanceIds=[instance], DocumentName="bravo-subscription-service",
                                   Parameters={"operation": ["stop"]})["Command"]["CommandId"]
        ssm.get_waiter("command_executed").wait(CommandId=command, InstanceId=instance,
                                              WaiterConfig={"Delay": 2, "MaxAttempts": 30})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Subscription runtime verification failed; no credential-bearing diagnostics emitted.")
        raise SystemExit(1) from None
