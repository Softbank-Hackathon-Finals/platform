# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.43.0,<2"]
# ///
"""Access the private RKE2 API with a personal AWS SSO profile and regional STS."""
import argparse
import base64
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from rke2_oidc_config import AUDIENCE, USERNAME_PREFIX


def aws_session(config, profile):
    import boto3

    session = boto3.Session(profile_name=profile, region_name=config["region"])
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "sso":
        raise ValueError("Select a personal IAM Identity Center SSO profile.")
    return session


def kubeconfig(config, settings_path, profile, server):
    cluster = {
        "server": server,
        "tls-server-name": config["tls_name"],
        "certificate-authority-data": base64.b64encode(config["server_ca_pem"].encode()).decode(),
    }
    return {"apiVersion": "v1", "kind": "Config", "current-context": "personal",
            "clusters": [{"name": "rke2", "cluster": cluster}],
            "contexts": [{"name": "personal", "context": {"cluster": "rke2", "user": "sso"}}],
            "users": [{"name": "sso", "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1", "command": sys.executable,
                "args": [str(Path(__file__).resolve()), str(settings_path), "--profile", profile, "--credential"],
                "interactiveMode": "Never", "provideClusterInfo": True,
            }}}]}


def emit_credential(config, settings_path, profile):
    info = json.loads(os.environ["KUBERNETES_EXEC_INFO"])
    cluster = info["spec"]["cluster"]
    if not re.fullmatch(r"https://127\.0\.0\.1:[0-9]{1,5}", cluster["server"]):
        raise ValueError("Expected a local SSM tunnel")
    expected = kubeconfig(config, settings_path, profile, cluster["server"])["clusters"][0]["cluster"]
    if (any(cluster.get(k) != v for k, v in expected.items())
            or cluster.get("insecure-skip-tls-verify") or cluster.get("proxy-url")):
        raise ValueError("Unexpected cluster TLS configuration")
    sts = aws_session(config, profile).client("sts")
    result = sts.get_web_identity_token(Audience=[AUDIENCE], DurationSeconds=300, SigningAlgorithm="RS256")
    print(json.dumps({"apiVersion": "client.authentication.k8s.io/v1", "kind": "ExecCredential",
                      "status": {"token": result["WebIdentityToken"],
                                 "expirationTimestamp": result["Expiration"].isoformat()}}))


@contextlib.contextmanager
def tunnel(session, config, profile, directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = directory / "tunnel.log"
    command = ["aws", "--profile", profile, "--region", config["region"], "ssm", "start-session",
               "--target", config["instance_id"], "--document-name", "AWS-StartPortForwardingSession",
               "--parameters", json.dumps({"portNumber": ["6443"], "localPortNumber": [str(port)]})]
    with log_path.open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    session_id = None
    try:
        for _ in range(150):
            output = log_path.read_text()
            match = re.search(r"Starting session with SessionId:\s*([\w@.+:-]+)", output)
            if match:
                session_id = match[1]
            if process.poll() is not None:
                raise RuntimeError("SSM tunnel stopped before it was ready. Check the profile and Session Manager plugin.")
            if session_id and f"Port {port} opened" in output:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("SSM tunnel did not become ready in 30 seconds")
        yield f"https://127.0.0.1:{port}"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if not session_id:
            match = re.search(r"Starting session with SessionId:\s*([\w@.+:-]+)", log_path.read_text())
            session_id = match[1] if match else None
        if session_id:
            ssm = session.client("ssm")
            try:
                ssm.terminate_session(SessionId=session_id)
            except ssm.exceptions.DoesNotExistException:
                pass
            for _ in range(6):
                sessions = ssm.describe_sessions(State="Active", Filters=[{"key": "SessionId", "value": session_id}])
                if not sessions["Sessions"]:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("SSM still lists this session as active. Check it in Session Manager.")


def access(config, path, profile, command):
    from botocore.exceptions import SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError

    session = aws_session(config, profile)
    try:
        identity = session.client("sts").get_caller_identity()
    except (SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError):
        subprocess.run(["aws", "--profile", profile, "sso", "login"], check=True)
        session = aws_session(config, profile)
        identity = session.client("sts").get_caller_identity()
    role = re.fullmatch(r"arn:aws:sts::([0-9]{12}):assumed-role/(AWSReservedSSO_BravoDevelopmentAdministrator_[0-9a-fA-F]{16})/([^/]+)", identity["Arn"])
    if not role or role[1] != config["account"] or role[2] != config["role_name"]:
        raise ValueError("This is not the approved team development role and account")
    user = session.client("identitystore").get_user_id(
        IdentityStoreId=config["store_id"], AlternateIdentifier={"UniqueAttribute": {
            "AttributePath": "userName", "AttributeValue": role[3]}})["UserId"]
    with tempfile.TemporaryDirectory(prefix="bravo-rke2-") as tmp:
        directory = Path(tmp)
        with tunnel(session, config, profile, directory) as server:
            file = directory / "kubeconfig"
            file.write_text(json.dumps(kubeconfig(config, path, profile, server)))
            cli = ["kubectl", "--kubeconfig", str(file)]
            def read(*args):
                return subprocess.check_output([*cli, "--request-timeout=15s", *args], text=True).strip()
            if read("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}") != USERNAME_PREFIX + user:
                raise RuntimeError("Personal directory identity did not match")
            if read("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}") != config["cluster_uid"]:
                raise RuntimeError("Unexpected cluster")
            if not read("get", "nodes", "-o", "name") or read("auth", "can-i", "create", "configmaps", "-n", "default") != "yes":
                raise RuntimeError("Node read or required permission failed")
            print("Personal SSO identity, cluster, node read and ConfigMap permission verified.", flush=True)
            if command:
                subprocess.run([*cli, *command], check=True)
    print("Local tunnel closed; SSM session has no active entry.")


def main():
    argument_parts = sys.argv[1:]
    command = []
    if "--" in argument_parts:
        sep = argument_parts.index("--")
        argument_parts, command = argument_parts[:sep], argument_parts[sep + 1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path, help="Trusted private connection settings published by the operator")
    parser.add_argument("--profile", default="bravo-personal")
    parser.add_argument("--credential", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argument_parts)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN", "AWS_ROLE_ARN",
                "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                "AWS_CA_BUNDLE", "KUBERNETES_MASTER", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ.update(AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true", AWS_EC2_METADATA_DISABLED="true",
                      AWS_STS_REGIONAL_ENDPOINTS="regional", AWS_PAGER="")
    path = args.settings.resolve(strict=True)
    config = json.loads(path.read_text())
    if args.credential:
        emit_credential(config, path, args.profile)
    else:
        access(config, path, args.profile, command)


if __name__ == "__main__":
    main()
