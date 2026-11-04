"""Apply reviewed non-secret manifests via host SSM; generate DB passwords in memory."""
import base64
import json
import secrets
import subprocess
import sys

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml"]


def kube(*args, data=None):
    result = subprocess.run(K + list(args), input=None if data is None else json.dumps(data),
                            text=True, capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise RuntimeError("kubernetes_operation_failed")
    return result.stdout


def apply(value):
    kube("apply", "--server-side", "--field-manager=t7-platform-data", "-f", "-", data=value)


def main():
    settings = json.load(sys.stdin)
    mount = "/var/lib/bravo-platform-data"
    if subprocess.run(["mountpoint", "-q", mount], check=False).returncode:
        raise RuntimeError("retained_volume_not_mounted")
    encryption = subprocess.run(["/usr/local/bin/rke2", "secrets-encrypt", "status"], capture_output=True, text=True, timeout=30, check=False)
    if encryption.returncode or "Encryption Status: Enabled" not in encryption.stdout:
        raise RuntimeError("kubernetes_secret_encryption_not_verified")
    items = settings["manifest"]["items"]
    for item in items:
        if item["kind"] == "Namespace":
            existing = json.loads(kube("get", "namespace", item["metadata"]["name"], "--ignore-not-found", "-o", "json") or "null")
            if existing and existing["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != "bravo-platform":
                raise RuntimeError("namespace_owned_elsewhere")
            apply(item)
    for user, name in [("platform_runtime", "platform-runtime-db"), ("platform_migration", "platform-migration-db")]:
        existing = json.loads(kube("get", "secret", name, "-n", "platform-state", "--ignore-not-found", "-o", "json") or "null")
        if existing:
            if (existing.get("type") != "kubernetes.io/basic-auth" or
                existing["metadata"].get("labels", {}).get("bravo.dev/owner") != "t7-platform-data" or
                base64.b64decode(existing["data"]["username"]).decode() != user):
                raise RuntimeError("secret_ownership_mismatch")
            continue
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "platform-state",
            "labels": {"bravo.dev/owner": "t7-platform-data", "component": "platform"}},
            "type": "kubernetes.io/basic-auth", "data": {
                "username": base64.b64encode(user.encode()).decode(),
                "password": base64.b64encode(secrets.token_urlsafe(48).encode()).decode()}}
        # stdin + captured output: no password in argv, SSM request/response or files.
        kube("create", "-f", "-", data=secret)
    chart = {"apiVersion": "helm.cattle.io/v1", "kind": "HelmChart",
             "metadata": {"name": "t7-cnpg", "namespace": "kube-system"}, "spec": {
                 "repo": "https://cloudnative-pg.github.io/charts", "chart": "cloudnative-pg", "version": "0.29.1",
                 "targetNamespace": "cnpg-system", "createNamespace": True, "valuesContent": settings["cnpg_values"]}}
    apply(chart)
    # Caller waits for CRD/controller readiness, then repeats with apply_database=true.
    if settings.get("apply_database") is True:
        kube("wait", "--for=condition=Established", "crd/clusters.postgresql.cnpg.io", "--timeout=90s")
        kube("wait", "--for=condition=Available", "deployment/t7-cnpg-cloudnative-pg", "-n", "cnpg-system", "--timeout=90s")
        kube("apply", "--server-side", "--dry-run=server", "--field-manager=t7-platform-data", "-f", "-", data=settings["manifest"])
        apply(settings["manifest"])
    print(json.dumps({"operator_chart_applied": True, "database_applied": settings.get("apply_database") is True,
                      "passwords_generated_or_reused_on_host": True, "secrets_encryption_verified": True}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__,
                          "error_code": str(error) if type(error) is RuntimeError else "invalid_input"}))
        raise SystemExit(1) from None
