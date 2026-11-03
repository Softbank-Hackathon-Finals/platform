"""Actions OIDC -> fixed SSM broker -> bounded inference. No persisted credentials."""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def prove_denied(call):
    try:
        call()
    except ClientError as error:
        if error.response["Error"]["Code"] in {"AccessDenied", "AccessDeniedException"}:
            return True
        raise
    raise RuntimeError("permission boundary was not enforced")


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
            url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com"
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
            with urllib.request.urlopen(request, timeout=20) as response:
                token = json.load(response)["value"]
            body = json.dumps({"web_identity_token": token}).encode()
            request = urllib.request.Request("http://127.0.0.1:18762/run", data=body,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=260) as response:
                result = json.load(response)
            results.append(result)
            if (result.get("state") != "PROPOSAL_READY" or not result.get("active_after")
                    or result.get("runtime_source_sha") != os.environ["BRAVO_RUNTIME_SOURCE_SHA"]):
                raise RuntimeError("broker did not produce a proposal")
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
