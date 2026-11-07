"""Read-only T44 customer GitOps intake; never approves, syncs or changes resources."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

NAMESPACE = "bravo-iac-gitops"
REPOSITORY = "https://github.com/Softbank-Hackathon-Finals/platform.git"
COMPONENTS = {"application-controller", "repo-server", "redis"}


class IntakeError(Exception):
    """Only fixed, non-secret diagnostic codes leave the transport boundary."""


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        raise IntakeError("RECONCILIATION_TIME_INVALID") from None


def assess(application, workloads, *, environment, revision, now, allow_prune=False):
    """Assess observations; the caller authenticates and fixes the target cluster."""
    if environment not in {"source-dev", "target-dev"} or not re.fullmatch("[0-9a-f]{40}", revision):
        raise IntakeError("REGISTERED_TARGET_REQUIRED")
    result = {
        "environment": environment,
        "expected_revision": revision,
        "observed_at": now.astimezone(UTC).isoformat(),
        "status": "BLOCKED",
        "reasons": [],
        "authorizes_cutover": False,
        "migration_verified": False,
        "control_equivalence_verified": False,
    }
    reasons = result["reasons"]
    components = {}
    for item in workloads:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        if labels.get("app.kubernetes.io/instance") != NAMESPACE:
            continue
        component = labels.get("app.kubernetes.io/component")
        if component not in COMPONENTS:
            continue
        if component in components:
            reasons.append("CONTROLLER_IDENTITY_AMBIGUOUS")
        components[component] = item
    if set(components) != COMPONENTS:
        reasons.append("CONTROLLERS_MISSING")
    for item in components.values():
        spec, status = item.get("spec", {}), item.get("status", {})
        desired = spec.get("replicas", 1)
        if (type(desired) is not int or desired < 1
                or status.get("observedGeneration") != item.get("metadata", {}).get("generation")
                or status.get("readyReplicas", 0) != desired
                or status.get("updatedReplicas", 0) != desired):
            reasons.append("CONTROLLERS_NOT_READY")
    if not application:
        result["status"] = "BOOTSTRAP_ONLY" if not reasons else "BOOTSTRAP_PENDING"
        return result
    spec, status = application.get("spec", {}), application.get("status", {})
    metadata = application.get("metadata", {})
    source, destination = spec.get("source", {}), spec.get("destination", {})
    if (metadata.get("namespace") != NAMESPACE or metadata.get("name") != f"bravo-iac-{environment}"
            or not metadata.get("uid") or not metadata.get("resourceVersion") or metadata.get("deletionTimestamp")
            or spec.get("project") != "bravo-iac" or spec.get("sources")
            or source.get("repoURL") != REPOSITORY
            or source.get("path") != f"infra/iac/charts/{environment}"
            or source.get("targetRevision") != revision
            or destination.get("server") != "https://kubernetes.default.svc"
            or destination.get("name") or destination.get("namespace")):
        reasons.append("APPLICATION_TARGET_MISMATCH")
    policy = spec.get("syncPolicy", {}).get("automated", {})
    if policy.get("prune") is True and not allow_prune:
        reasons.append("PRUNE_NOT_APPROVED")
    if reasons:
        return result
    operation = status.get("operationState", {})
    retry_count = operation.get("retryCount", 0)
    if type(retry_count) is not int or retry_count < 0:
        reasons.append("OPERATION_RETRY_COUNT_INVALID")
        return result
    if application.get("operation") or operation.get("phase") in {"Running", "Terminating"}:
        if retry_count:
            result.update(status="SYNC_RETRYING", reasons=["OPERATION_RETRYING"], retry_count=retry_count)
        else:
            result.update(status="SYNC_IN_PROGRESS", reasons=["OPERATION_IN_PROGRESS"])
        return result
    # Explicit false is required. Missing enabled has Argo's implicit-enable semantics.
    if policy.get("enabled") is False:
        result["status"] = "SYNC_PAUSED"
        return result
    if policy.get("enabled") is not True:
        reasons.append("SYNC_MODE_UNCONFIRMED")
        return result
    result["status"] = "SYNC_PENDING"
    if (status.get("sync", {}).get("status") != "Synced"
            or status.get("sync", {}).get("revision") != revision
            or status.get("sync", {}).get("comparedTo", {}).get("source") != source
            or status.get("sync", {}).get("comparedTo", {}).get("destination") != destination
            or operation.get("phase") != "Succeeded"
            or operation.get("syncResult", {}).get("revision") != revision
            or operation.get("syncResult", {}).get("source") != source
            or application.get("operation")
            or status.get("conditions")
            or status.get("health", {}).get("status") != "Healthy"):
        reasons.append("REVISION_OR_SYNC_UNCONFIRMED")
        return result
    try:
        reconciled = _time(status.get("reconciledAt"))
    except IntakeError as error:
        reasons.append(str(error))
        return result
    if not timedelta(0) <= now - reconciled <= timedelta(minutes=5):
        reasons.append("RECONCILIATION_STALE")
        return result
    resources = status.get("resources", [])
    if not resources or any(r.get("status") != "Synced" for r in resources):
        reasons.append("RESOURCE_SYNC_UNCONFIRMED")
        return result
    result["status"] = "SYNC_VERIFIED"
    result["synced_resource_count"] = len(resources)
    return result


def inspect(get, *, expected_cluster_uid, environment, revision, allow_prune=False, now=None):
    """GET only; require the registered cluster before reading its customer objects."""
    if not expected_cluster_uid or not isinstance(expected_cluster_uid, str):
        raise IntakeError("REGISTERED_CLUSTER_REQUIRED")
    before = get("namespace", "kube-system")
    if before.get("metadata", {}).get("uid") != expected_cluster_uid:
        raise IntakeError("CLUSTER_IDENTITY_MISMATCH")
    crd = get("crd", "applications.argoproj.io", optional=True)
    if not crd:
        result = assess(None, [], environment=environment, revision=revision,
                        now=now or datetime.now(UTC))
        result.update(status="NOT_INSTALLED", reasons=["APPLICATION_CRD_MISSING"])
    else:
        application = get("application", f"bravo-iac-{environment}", namespace=NAMESPACE, optional=True)
        workloads = get("deployments,statefulsets", namespace=NAMESPACE).get("items", [])
        result = assess(application, workloads, environment=environment, revision=revision,
                        allow_prune=allow_prune, now=now or datetime.now(UTC))
        if application:
            after = get("application", f"bravo-iac-{environment}", namespace=NAMESPACE)
            if (application.get("metadata", {}).get("uid") != after.get("metadata", {}).get("uid")
                    or application.get("metadata", {}).get("resourceVersion") != after.get("metadata", {}).get("resourceVersion")):
                result.update(status="BLOCKED", reasons=["APPLICATION_CHANGED_DURING_READ"])
    if get("namespace", "kube-system").get("metadata", {}).get("uid") != expected_cluster_uid:
        raise IntakeError("CLUSTER_IDENTITY_CHANGED")
    result["cluster_identity_matches"] = True
    return result


def reader(kubeconfig, context=None):
    if not kubeconfig or not Path(kubeconfig).is_file():
        raise IntakeError("EXPLICIT_KUBECONFIG_REQUIRED")
    argv = ["kubectl", "--kubeconfig", kubeconfig, "--request-timeout=10s"]
    if context:
        argv += ["--context", context]

    def run(arguments):
        try:
            output = subprocess.run([*argv, *arguments], capture_output=True, text=True,
                                    check=True, timeout=20)
            return json.loads(output.stdout) if output.stdout.strip() else {}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            raise IntakeError("KUBERNETES_READ_UNCONFIRMED") from None

    # Never accept insecure TLS or an HTTP API, even for a status observation.
    config = run(["config", "view", "--minify", "-o", "json"])
    clusters = config.get("clusters", [])
    if (len(clusters) != 1 or clusters[0].get("cluster", {}).get("insecure-skip-tls-verify")
            or not clusters[0].get("cluster", {}).get("server", "").startswith("https://")):
        raise IntakeError("VERIFIED_API_TLS_REQUIRED")

    def get(kind, name=None, *, namespace=None, optional=False):
        arguments = ["get", kind]
        if name:
            arguments.append(name)
        if namespace:
            arguments += ["--namespace", namespace]
        if optional:
            arguments.append("--ignore-not-found")
        return run([*arguments, "-o", "json"])
    return get


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    parser.add_argument("--context")
    parser.add_argument("--expected-cluster-uid", required=True)
    parser.add_argument("--environment", choices=("source-dev", "target-dev"), required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--allow-prune", action="store_true", help="acknowledge separately reviewed deletion scope")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch("[0-9a-f]{40}", args.revision):
            raise IntakeError("REVIEWED_REVISION_REQUIRED")
        result = inspect(reader(args.kubeconfig, args.context), expected_cluster_uid=args.expected_cluster_uid,
                         environment=args.environment, revision=args.revision, allow_prune=args.allow_prune)
    except IntakeError as error:
        result = {"status": "BLOCKED", "reasons": [str(error)], "authorizes_cutover": False,
                  "migration_verified": False, "control_equivalence_verified": False}
    except (TypeError, ValueError, KeyError, AttributeError):
        result = {"status": "BLOCKED", "reasons": ["MALFORMED_OBSERVATION"], "authorizes_cutover": False,
                  "migration_verified": False, "control_equivalence_verified": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "SYNC_VERIFIED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
