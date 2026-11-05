"""Use a short-lived reader for private exports; scan only on explicit producer action."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


class AccessDenied(RuntimeError):
    """Provider-confirmed denial, distinguished from transport or expiry failures."""


def now():
    return datetime.now(UTC).isoformat()


def write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def environment():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AWS_", "KUBERNETES_", "PYTHON", "BOTO"))
           and k not in {"KUBECONFIG", "SSL_CERT_FILE", "SSL_CERT_DIR",
                         "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
    env.update(AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_EC2_METADATA_DISABLED="true",
               AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true", AWS_PAGER="",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def reader_environment(credentials):
    env = environment()
    env.update(AWS_CONFIG_FILE="/dev/null", AWS_ACCESS_KEY_ID=credentials["AccessKeyId"],
               AWS_SECRET_ACCESS_KEY=credentials["SecretAccessKey"],
               AWS_SESSION_TOKEN=credentials["SessionToken"])
    return env


def aws(config, env, service, operation, data, profile=None):
    argv = ["aws", "--region", config["region"], "--output", "json", "--no-cli-pager",
            "--cli-connect-timeout", "10", "--cli-read-timeout", "25"]
    if profile:
        argv += ["--profile", profile]
    if (service, operation) == ("eks", "get-token"):
        argv += [service, operation, "--cluster-name", data["clusterName"]]
    else:
        argv += [service, operation, "--cli-input-json", json.dumps(data)]
    result = subprocess.run(argv, check=False, env=env, capture_output=True, timeout=45)
    if result.returncode and re.search(rb"\((AccessDenied|AccessDeniedException)\)", result.stderr):
        raise AccessDenied(f"{service}_{operation}_access_denied")
    require(result.returncode == 0, f"{service}_{operation}_failed_details_suppressed")
    return json.loads(result.stdout)


class LoopbackTLS(http.client.HTTPSConnection):
    """Keep original hostname and CA while using a caller-owned loopback tunnel."""

    def __init__(self, host, ca, port):
        context = ssl.create_default_context(cadata=base64.b64decode(ca).decode())
        super().__init__(host, context=context, timeout=20)
        self.local_port = port

    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.local_port), timeout=20)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def api(config, port, token, path, body=None):
    conn = LoopbackTLS(urlsplit(config["endpoint"]).hostname, config["ca"], port)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        conn.request("POST" if body is not None else "GET", path,
                     body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def check_scope(exports, config):
    ns = config["namespace"]
    for items in exports.values():
        require(all(i["metadata"].get("namespace") == ns for i in items["items"]),
                "unexpected_export_namespace")
    for deployment in exports["deployments.json"]["items"]:
        pod_spec = deployment["spec"]["template"]["spec"]
        for container in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
            for variable in container.get("env", []):
                sensitive = re.search(r"(?:TOKEN|PASSWORD|SECRET|ACCESS_KEY|PRIVATE_KEY)", variable["name"], re.IGNORECASE)
                reference = (variable["name"].endswith(("_ID", "_ARN", "_NAME", "_REF"))
                             and variable.get("value") in config.get("allowed_credential_references", []))
                require(not (sensitive and variable.get("value") and not reference), "literal_credential_export_refused")
    apps = [x for x in exports["deployments.json"]["items"]
            if x["metadata"]["name"] == config["deployment"]]
    require(len(apps) == 1 and apps[0]["metadata"]["uid"] == config["deployment_uid"],
            "registered_deployment_changed")
    pod = apps[0]["spec"]["template"]["spec"]
    require(pod.get("serviceAccountName", "default") == config["service_account"],
            "registered_service_account_changed")
    containers = [x for x in pod["containers"] if x["name"] == config["container"]]
    require(len(containers) == 1 and containers[0]["image"] == config["image"],
            "registered_image_changed_reconcile_before_scan")
    role_arns = {x["metadata"].get("annotations", {}).get("eks.amazonaws.com/role-arn")
                 for x in exports["serviceaccounts.json"]["items"]}
    role_arns.discard(None)
    require(role_arns == set(config["source_role_arns"]), "source_iam_scope_changed")
    dbs = [x for x in exports["clusters.json"]["items"]
           if x["metadata"]["name"] == config["database"]]
    require(len(dbs) == 1 and dbs[0]["metadata"]["uid"] == config["database_uid"],
            "registered_database_changed")


def source_revision(path):
    p = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=False,
                       capture_output=True, text=True, timeout=15)
    require(p.returncode == 0, "checkout_unavailable")
    dirty = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], check=False,
                           capture_output=True, text=True, timeout=15)
    require(dirty.returncode == 0 and not dirty.stdout, "checkout_must_be_clean")
    return p.stdout.strip()


def collect(config, profile, port, output, mode, repo=None, platform=None):
    require(datetime.now(UTC) < datetime.fromisoformat(config["access_until"]),
            "access_window_expired")
    require(not output.exists(), "output_exists_do_not_repeat")
    registration_matches = (config.get("source_sha") == config.get("registered_source_sha")
                            and config.get("image") == config.get("registered_image"))
    if mode == "scan":
        require(registration_matches, "registration_binding_changed_reconcile_before_scan")
        require(source_revision(repo) == config["source_sha"], "source_revision_changed")
        require(source_revision(platform) == config["inventory_revision"], "inventory_revision_changed")
    os.umask(0o077)
    output.mkdir(mode=0o700, parents=False)
    receipt = {"started_at": now(), "mode": mode, "scan_started": False,
               "status": "STARTED", "source_sha": config["source_sha"],
               "source_environment": config["source_environment"], "checks": [],
               "registration_matches": registration_matches,
               "registered_source_sha": config.get("registered_source_sha")}
    write(output / "started.json", receipt)
    try:
        personal = environment()
        personal["AWS_CONFIG_FILE"] = str(Path.home() / ".aws/config")
        caller = aws(config, personal, "sts", "get-caller-identity", {}, profile)
        require(caller["UserId"] in config["allowed_sso_user_ids"]
                and caller["Account"] == config["account"], "personal_sso_identity_mismatch")
        session_name = "t16-" + re.sub(r"[^a-zA-Z0-9+=,.@_-]", "-", caller["UserId"].split(":", 1)[1])
        assumed = aws(config, personal, "sts", "assume-role", {
            "RoleArn": config["reader_role_arn"], "RoleSessionName": session_name,
            "DurationSeconds": 900}, profile)
        credentials = assumed.pop("Credentials")
        env = reader_environment(credentials)
        env.update(AWS_REGION=config["region"], AWS_DEFAULT_REGION=config["region"])
        identity = aws(config, env, "sts", "get-caller-identity", {})
        require(identity["Account"] == config["account"] and
                identity["UserId"].split(":")[0] == config["reader_role_id"], "reader_identity_mismatch")
        receipt.update(personal_identity=caller, reader_identity=identity,
                       expires_at=credentials["Expiration"], credentials_saved=False)
        del credentials, assumed
        auth = aws(config, env, "eks", "get-token", {"clusterName": config["cluster_name"]})
        token = auth["status"]["token"]
        ns = config["namespace"]
        resources = [("deployments.json", f"/apis/apps/v1/namespaces/{ns}/deployments", "DeploymentList"),
                     ("serviceaccounts.json", f"/api/v1/namespaces/{ns}/serviceaccounts", "ServiceAccountList"),
                     ("clusters.json", f"/apis/postgresql.cnpg.io/v1/namespaces/{ns}/clusters", "ClusterList")]
        exports = {}
        for name, path, kind in resources:
            status, body = api(config, port, token, path)
            require(status == 200 and body["kind"] == kind and not body.get("metadata", {}).get("continue"),
                    "export_failed_or_incomplete")
            exports[name] = body
            receipt["checks"].append({"resource": name, "http": status, "items": len(body["items"])})
        check_scope(exports, config)
        export_dir = output / "exports"
        export_dir.mkdir(mode=0o700)
        for name, body in exports.items():
            write(export_dir / name, body)
        receipt["export_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in export_dir.iterdir()}
        negatives = [("", "secrets", "get", ns), ("", "pods", "create", ns),
                     ("", "pods/exec", "create", ns), ("apps", "deployments", "patch", ns),
                     ("postgresql.cnpg.io", "clusters", "patch", ns),
                     ("apps", "deployments", "list", "kube-system")]
        for group, resource, verb, namespace in negatives:
            status, body = api(config, port, token, "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                               {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                                "spec": {"resourceAttributes": {"group": group, "resource": resource.split("/")[0],
                                          **({"subresource": resource.split("/")[1]} if "/" in resource else {}),
                                          "verb": verb, "namespace": namespace}}})
            require(status == 201 and body["status"].get("allowed") is False
                    and not body["status"].get("evaluationError"), "negative_authorization_failed")
            receipt["checks"].append({"resource": resource, "namespace": namespace,
                                       "verb": verb, "grade": "SSAR", "allowed": False})
        status, _ = api(config, port, None, resources[0][1])
        require(status == 401, "unauthenticated_access_not_denied")
        receipt["checks"].append({"grade": "actual_http", "unauthenticated_http": status})
        receipt["aws_preflight"] = []
        for arn in config["source_role_arns"]:
            role = arn.rsplit("/", 1)[1]
            attached = aws(config, env, "iam", "list-attached-role-policies", {"RoleName": role})
            require(attached["AttachedPolicies"] == config["attached_policies"], "attached_policy_scope_changed")
            policies = aws(config, env, "iam", "list-role-policies", {"RoleName": role})
            for policy in policies["PolicyNames"]:
                aws(config, env, "iam", "get-role-policy", {"RoleName": role, "PolicyName": policy})
            receipt["aws_preflight"].append({"role": arn, "inline_policies_read": policies["PolicyNames"],
                                              "attached_policies": attached["AttachedPolicies"]})
        for bucket in config["buckets"]:
            value = aws(config, env, "s3api", "get-bucket-policy-status", {"Bucket": bucket})
            loc = aws(config, env, "s3api", "get-bucket-location", {"Bucket": bucket})
            receipt["aws_preflight"].append({"bucket": bucket, "policy_status": value["PolicyStatus"],
                                              "location": loc["LocationConstraint"]})
        for secret in config["secret_arns"]:
            value = aws(config, env, "secretsmanager", "describe-secret", {"SecretId": secret})
            receipt["aws_preflight"].append({"secret_arn": value["ARN"], "metadata_only": True,
                                              "rotation_enabled": value.get("RotationEnabled", False)})
        for key in config["key_arns"]:
            value = aws(config, env, "kms", "describe-key", {"KeyId": key})["KeyMetadata"]
            receipt["aws_preflight"].append({"key_arn": value["Arn"], "metadata_only": True,
                                              "key_state": value["KeyState"]})
        try:
            aws(config, env, "iam", "list-role-policies", {"RoleName": config["denied_role_name"]})
        except AccessDenied:
            receipt["aws_preflight"].append({"grade": "actual_aws_denial", "action": "iam:ListRolePolicies",
                                              "role": config["denied_role_name"], "allowed": False})
        else:
            raise RuntimeError("out_of_scope_iam_read_was_allowed")
        receipt["access_verified_at"] = now()
        write(output / "access.json", receipt)
        if mode == "scan":
            receipt["scan_started"] = True
            write(output / "scan-started.json", {"started_at": now(), "inventory_revision": config["inventory_revision"]})
            argv = ["uv", "run", "--frozen", "--extra", "aws", "bravo-inventory", "scan",
                    "--app-id", config["app_id"], "--repo", str(repo), "--app-path", config["app_path"],
                    "--repo-ref", config["repo_ref"],
                    "--source-env", config["source_environment"], "--source-kind", "aws",
                    "--namespace", ns, "--exports", str(export_dir), "--aws-live",
                    "--source-sha", config["source_sha"], "--output", str(output / "inventory.json")]
            result = subprocess.run(argv, check=False, cwd=platform / "services/inventory", env=env,
                                    capture_output=True, timeout=600)
            receipt["scan_exit_code"] = result.returncode
            require(result.returncode == 0, "scan_failed_reconcile_before_retry_details_suppressed")
            receipt["inventory_sha256"] = hashlib.sha256((output / "inventory.json").read_bytes()).hexdigest()
        receipt.update(status="PASS", ended_at=now())
    except BaseException as exc:
        receipt.update(status="UNKNOWN" if receipt["scan_started"] else "FAILED", ended_at=now(),
                       failure=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        raise
    finally:
        write(output / "result.json", receipt)
    return {"status": receipt["status"], "mode": mode, "scan_started": receipt["scan_started"],
            "receipt": str(output / "result.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["verify", "scan"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--platform", type=Path)
    args = parser.parse_args()
    require(args.config.stat().st_mode & 0o077 == 0, "config_must_be_private")
    require(1024 <= args.port <= 65535, "invalid_loopback_port")
    require(args.mode != "scan" or (args.repo and args.platform), "scan_checkout_paths_required")
    result = collect(json.loads(args.config.read_text()), args.profile, args.port,
                     args.output_dir.resolve(), args.mode,
                     args.repo.resolve() if args.repo else None,
                     args.platform.resolve() if args.platform else None)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - never print raw credential-provider exceptions
        print(json.dumps({"status": "FAILED", "reason": str(error) if isinstance(error, RuntimeError)
                          else type(error).__name__, "details_suppressed": True}))
        raise SystemExit(1) from None
