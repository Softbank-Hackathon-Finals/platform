"""Verify an applied grant with short-lived tokens, without opening app writes.

An operator provides the kubeconfig. Tokens remain in memory/stdin. The only
mutation is a unique temporary file in the selected pooler's config directory.
ConfigMap patch permission is checked by SelfSubjectAccessReview, not by
overwriting the deployment's durable cutover destination.
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone

APP_READ = (
    "import json,os,urllib.request; "
    "r=urllib.request.Request('http://127.0.0.1:8080/admin/write-block',"
    "headers={'Authorization':'Bearer '+os.environ['ADMIN_TOKEN']}); "
    "v=json.load(urllib.request.urlopen(r,timeout=5)); "
    "assert v.get('write_blocked') is True; print('blocked')"
)
POOLER_WRITE = (
    'set -eu; test -w /etc/pgbouncer/pgbouncer.ini; '
    'probe=$(mktemp /etc/pgbouncer/.executor-probe.XXXXXX); '
    'trap \'rm -f "$probe" "$probe.moved"\' EXIT; '
    'printf probe > "$probe"; mv "$probe" "$probe.moved"; '
    'test "$(cat "$probe.moved")" = probe; printf writable'
)


def run(args, payload=None):
    return subprocess.run(args, input=payload, text=True, capture_output=True, check=False, timeout=30)


def denied(result):
    return result.returncode != 0 and "forbidden" in result.stderr.lower()


def token_config(kubeconfig, namespace, identity):
    token = run(["kubectl", "--kubeconfig", kubeconfig, "-n", namespace,
                 "create", "token", identity, "--duration=10m"])
    view = run(["kubectl", "--kubeconfig", kubeconfig, "config", "view", "--raw", "--minify", "-o", "json"])
    if token.returncode or view.returncode:
        raise RuntimeError("operator_token_request_failed")
    config = json.loads(view.stdout)
    config["users"] = [{"name": "probe", "user": {"token": token.stdout.strip()}}]
    config["contexts"][0]["context"]["user"] = "probe"
    config["contexts"][0]["context"]["namespace"] = namespace
    return json.dumps(config)


def verify(kubeconfig, namespace, configmap, app_pods, pooler_pod, db_pod,
           pooler_container, app_container="app", identity_namespace=None, check_app=True):
    if check_app != bool(app_pods):
        raise ValueError("explicit_app_or_pooler_only_scope_required")
    checks = {}
    for identity in ("bravo-executor", "bravo-collector"):
        config = token_config(kubeconfig, identity_namespace or namespace, identity)

        def call(*args, token_config_json=config):
            return run(["kubectl", "--kubeconfig", "/dev/stdin", "--namespace", namespace, *args], token_config_json)

        def can(verb, resource, subresource=None):
            args = ("auth", "can-i", verb, resource)
            if subresource:
                args += ("--subresource=" + subresource,)
            result = call(*args)
            if result.returncode == 0 and result.stdout.strip() == "yes":
                return True
            if result.returncode == 1 and result.stdout.strip() == "no":
                return False
            raise RuntimeError("authorization_readback_failed")

        prefix = identity.removeprefix("bravo-")
        checks[prefix + "_pods_list"] = call("get", "pods", "-o", "name").returncode == 0
        for verb in ("get", "create"):
            # --subresource is required: 'pods/exec' alone has no target name.
            result = call("auth", "can-i", verb, "pods/" + pooler_pod, "--subresource=exec")
            if (result.returncode, result.stdout.strip()) not in ((0, "yes"), (1, "no")):
                raise RuntimeError("exec_authorization_readback_failed")
            allowed = result.returncode == 0 and result.stdout.strip() == "yes"
            checks[f"{prefix}_named_exec_{verb}"] = allowed == (identity == "bravo-executor")
        checks[prefix + "_db_exec_denied"] = denied(call("exec", db_pod, "-c", "postgres", "--", "true"))
        checks[prefix + "_secret_list_denied"] = denied(call("get", "secrets"))
        checks[prefix + "_other_namespace_denied"] = denied(call("-n", "kube-system", "get", "pods"))
        checks[prefix + "_pod_create_denied"] = not can("create", "pods")
        checks[prefix + "_rbac_patch_denied"] = not can("patch", "roles.rbac.authorization.k8s.io")
        checks[prefix + "_token_issue_denied"] = not can(
            "create", "serviceaccounts/" + identity, subresource="token")
        checks[prefix + "_other_config_patch_denied"] = not can("patch", "configmaps/cutover-unrelated")
        if identity == "bravo-executor":
            checks["executor_exact_config_get"] = call("get", "configmap", configmap, "-o", "name").returncode == 0
            checks["executor_exact_config_patch_authorized"] = can("patch", "configmaps/" + configmap)
            writable = call("exec", pooler_pod, "-c", pooler_container, "--", "sh", "-ec", POOLER_WRITE)
            checks["executor_config_directory_writable"] = writable.returncode == 0 and writable.stdout == "writable"
            if check_app:
                for index, pod in enumerate(app_pods):
                    response = call("exec", pod, "-c", app_container, "--", "python", "-c", APP_READ)
                    checks[f"executor_app_{index}_authenticated_block_read"] = response.returncode == 0 and response.stdout.strip() == "blocked"
        else:
            checks["collector_config_get_denied"] = denied(call("get", "configmap", configmap))
            checks["collector_config_patch_denied"] = not can("patch", "configmaps/" + configmap)
            checks["collector_pooler_exec_denied"] = denied(call("exec", pooler_pod, "-c", pooler_container, "--", "true"))
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
            "passed": all(checks.values()), "app_block_read_requested": check_app,
            "app_access_verified": check_app and all(
                checks[f"executor_app_{index}_authenticated_block_read"] for index in range(len(app_pods))),
            "cutover_ready": False,
            "configmap_data_changed": False, "business_writes_opened": False,
            "secret_values_exported": False,
            "scope": "Kubernetes access and local config filesystem; not network or cutover verification"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--identity-namespace")
    parser.add_argument("--pooler-config", required=True)
    parser.add_argument("--pooler-pod", required=True)
    parser.add_argument("--pooler-container", required=True)
    workloads = parser.add_mutually_exclusive_group(required=True)
    workloads.add_argument("--app-pod", action="append")
    workloads.add_argument("--pooler-only", action="store_true",
                           help="Verify only pooler/RBAC; app access remains unverified")
    parser.add_argument("--app-container", default="app")
    parser.add_argument("--db-pod", required=True)
    args = parser.parse_args()
    try:
        report = verify(args.kubeconfig, args.namespace, args.pooler_config, args.app_pod or [],
                        args.pooler_pod, args.db_pod, args.pooler_container,
                        args.app_container, args.identity_namespace, check_app=not args.pooler_only)
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired):
        report = {"passed": False, "error": "verification_failed", "raw_provider_output_suppressed": True}
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
