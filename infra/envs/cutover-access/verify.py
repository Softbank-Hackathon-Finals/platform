"""Verify an applied grant with short-lived tokens, without opening app writes.

An operator provides the kubeconfig. Tokens remain in memory/stdin. The only
mutation is a unique temporary file in the selected pooler's config directory.
ConfigMap patch permission is checked by SelfSubjectAccessReview, not by
overwriting the deployment's durable cutover destination.
"""

import argparse
import json
import subprocess
import tempfile
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


def secret_read_audit(call, can, namespace):
    """Audit effective rules; use SSAR for live authorization, never fetch Secrets.

    RulesReview discovers named grants, including names of nonexistent Secrets.
    It is not an authorization decision. An incomplete inventory or any reported
    read grant prevents a clean audit, even if the subsequent SSAR says no.
    """
    summary = {"namespace": namespace, "rules_complete": False,
               "read_rule_count": None, "named_targets_checked": 0,
               "allowed_read_queries": 0}
    verbs = ("get", "list", "watch")
    summary["allowed_read_queries"] = sum(can(verb, "secrets", target_namespace=namespace) for verb in verbs)
    request = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview",
               "spec": {"namespace": namespace}}
    # stdin already carries the token kubeconfig. This temporary file contains
    # only the namespace and review kind; no token, rule output or Secret data.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as body:
        json.dump(request, body)
        body.flush()
        response = call("create", "--raw", "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews",
                        "-f", body.name, target_namespace=namespace)
    if response.returncode:
        return False, summary
    try:
        document = json.loads(response.stdout)
        if document.get("kind") != "SelfSubjectRulesReview" or document.get("apiVersion") != "authorization.k8s.io/v1":
            return False, summary
        status = document["status"]
        rules = status["resourceRules"]
        if not isinstance(rules, list):
            return False, summary
        names = set()
        read_rules = 0
        for rule in rules:
            fields = [rule[key] for key in ("apiGroups", "resources", "verbs")]
            resource_names = rule.get("resourceNames", [])
            if any(not isinstance(values, list) or not all(isinstance(v, str) for v in values)
                   for values in [*fields, resource_names]):
                return False, summary
            groups, resources, actions = fields
            if not groups or not resources or not actions:
                return False, summary
            if (set(groups) & {"", "*"} and set(resources) & {"secrets", "*"}
                    and set(actions) & {*verbs, "*"}):
                read_rules += 1
                names.update(resource_names)
        summary["rules_complete"] = status.get("incomplete") is False and not status.get("evaluationError")
    except (ValueError, KeyError, TypeError, AttributeError):
        return False, summary
    summary["read_rule_count"] = read_rules
    for name in sorted(names):
        summary["allowed_read_queries"] += sum(
            can(verb, "secrets/" + name, target_namespace=namespace) for verb in verbs)
    summary["named_targets_checked"] = len(names)
    return (summary["rules_complete"] and read_rules == 0 and summary["allowed_read_queries"] == 0), summary


def verify(kubeconfig, namespace, configmap, app_pods, pooler_pod, db_pod,
           pooler_container, app_container="app", identity_namespace=None, check_app=True):
    if check_app != bool(app_pods):
        raise ValueError("explicit_app_or_pooler_only_scope_required")
    checks = {}
    secret_audits = {}
    secret_namespaces = {"workload": namespace}
    if identity_namespace and identity_namespace != namespace:
        secret_namespaces["identity"] = identity_namespace
    for identity in ("bravo-executor", "bravo-collector"):
        config = token_config(kubeconfig, identity_namespace or namespace, identity)

        def call(*args, token_config_json=config, target_namespace=namespace):
            return run(["kubectl", "--kubeconfig", "/dev/stdin", "--namespace", target_namespace, *args], token_config_json)

        def can(verb, resource, subresource=None, target_namespace=namespace):
            args = ("auth", "can-i", verb, resource)
            if subresource:
                args += ("--subresource=" + subresource,)
            result = call(*args, target_namespace=target_namespace)
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
        checks[prefix + "_secret_list_denied"] = not can("list", "secrets")
        secret_audits[prefix] = []
        for scope, secret_namespace in secret_namespaces.items():
            clean, audit = secret_read_audit(call, can, secret_namespace)
            checks[f"{prefix}_{scope}_secret_read_denied"] = clean
            secret_audits[prefix].append(audit)
        checks[prefix + "_other_namespace_denied"] = denied(call("-n", "kube-system", "get", "pods"))
        checks[prefix + "_pod_create_denied"] = not can("create", "pods")
        checks[prefix + "_rbac_patch_denied"] = not can("patch", "roles.rbac.authorization.k8s.io")
        checks[prefix + "_token_issue_denied"] = not can(
            "create", "serviceaccounts/" + identity, subresource="token",
            target_namespace=identity_namespace or namespace)
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
            "secret_read_audit": {"identities": secret_audits,
                                  "scope": "workload and identity namespaces at observation; not cluster-wide or future grants",
                                  "incomplete_rules_require_operator_review": True},
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
