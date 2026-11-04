"""Verify the permanent DB/DNS path after temporary probe egress is absent."""
import json
import subprocess

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml"]
NS = "platform-state"
NAME = "t7-db-permanent-path"
QUERY = """SELECT json_build_object(
 'status','PASS',
 'runtime_login',current_user = 'platform_runtime',
 'tls',(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()),
 'schema_version',(SELECT version=1 FROM bravo_platform.schema_version),
 'state_readable',EXISTS(SELECT 1 FROM bravo_platform.plans),
 'events_readable',EXISTS(SELECT 1 FROM bravo_platform.events),
 'receipts_readable',EXISTS(SELECT 1 FROM bravo_platform.plan_commands));"""
COMMAND = """export PGUSER="$(cat /credential/username)" PGPASSWORD="$(cat /credential/password)"
exec psql -X --no-password --tuples-only --no-align --set=ON_ERROR_STOP=1 --command="$VERIFY_SQL"
"""


def kube(*args, data=None):
    return subprocess.check_output(K + list(args), input=None if data is None else json.dumps(data), text=True, timeout=120)


def main():
    if kube("get", "networkpolicy", "t7-verification", "-n", NS, "--ignore-not-found", "-o", "name").strip():
        raise RuntimeError("temporary_policy_still_present")
    kube("wait", "--for=condition=Ready", "cluster/platform-db", "-n", NS, "--timeout=90s")
    cluster = json.loads(kube("get", "cluster", "platform-db", "-n", NS, "-o", "json"))
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": NAME, "namespace": NS}, "spec": {
        "backoffLimit": 0, "activeDeadlineSeconds": 90, "template": {"metadata": {"labels": {
            "bravo.dev/db-client": "runtime", "bravo.dev/probe": "t7-database"}}, "spec": {
            "restartPolicy": "Never", "serviceAccountName": "platform-runtime", "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "verify", "image": cluster["spec"]["imageName"], "command": ["sh", "-ec", COMMAND],
                "env": [{"name": k, "value": v} for k, v in {
                    "PGHOST": "platform-db-rw.platform-state.svc", "PGDATABASE": "platform", "PGSSLMODE": "verify-full",
                    "PGSSLROOTCERT": "/ca/ca.crt", "PGCONNECT_TIMEOUT": "10", "VERIFY_SQL": QUERY}.items()],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
                "volumeMounts": [{"name": "credential", "mountPath": "/credential", "readOnly": True},
                                 {"name": "ca", "mountPath": "/ca", "readOnly": True}]}],
            "volumes": [{"name": "credential", "secret": {"secretName": "platform-runtime-db", "defaultMode": 288}},
                        {"name": "ca", "secret": {"secretName": "platform-db-ca", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]}}}}
    kube("create", "-f", "-", data=job)
    try:
        kube("wait", "--for=condition=Complete", "job/" + NAME, "-n", NS, "--timeout=90s")
        result = json.loads(kube("logs", "job/" + NAME, "-n", NS))
        checks = {k: v for k, v in result.items() if k != "status"}
        if len(checks) != 6 or not all(v is True for v in checks.values()):
            raise RuntimeError("permanent_path_check_failed")
        checks["temporary_policy_absent"] = not kube("get", "networkpolicy", "t7-verification", "-n", NS, "--ignore-not-found", "-o", "name").strip()
        checks["persistent_volume_bound"] = json.loads(kube("get", "pvc", "platform-db-1", "-n", NS, "-o", "json"))["status"]["phase"] == "Bound"
        if not all(checks.values()):
            raise RuntimeError("final_boundary_check_failed")
        print(json.dumps({"status": "PASS", "checks": checks, "check_count": len(checks),
                          "scope": "fresh_psql_process_TLS_and_T10_tables_on_permanent_policy_not_full_consumer_suite"}))
    finally:
        kube("delete", "job", NAME, "-n", NS, "--wait=true", "--timeout=60s")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
