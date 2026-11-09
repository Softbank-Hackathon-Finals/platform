#!/usr/bin/env python3
"""Render demo-chat for target-dev; never contact a cluster or carry a credential value.

The app reaches the database only through target-pgbouncer, which points at the
source DB before cutover, and starts write-blocked. The Secrets Manager sidecar is
for development measurement and is not exit evidence (see secrets_manager_compat.py).
"""
import argparse
import json
import re
from pathlib import Path

from render import LABELS, NAMESPACE, resource
from secrets_manager_compat import CREDENTIALS_FILE
from secrets_manager_compat import PORT as COMPAT_PORT

NAME = "demo-chat"
# X-Bravo-Env carries APP_ENV. T18 and T37 register the target environment as onprem.
APP_ENV = "onprem"
# Same label as the OpenBao path, so the two names of one credential stay recognisable.
DB_SECRET_ID = "bravo/target/precutover/database"
COMPAT_SCRIPT = "secrets_manager_compat.py"
COMPAT_CONFIG = NAME + "-secrets-manager-compat"
PYTHON = "/app/.venv/bin/python"
# T37: HTTP metrics to the in-cluster Collector over mTLS. The client certificate Secret is made by the
# observability owner; demo-chat refuses to start if the files are missing or OTEL_EXPORTER_OTLP* is set.
METRICS_SECRET = "bravo-app-metrics-client"
METRICS_DIR = "/etc/bravo-metrics"
METRICS_ENDPOINT = "https://bravo-collector.bravo-observability.svc:4318/v1/metrics"
# The one place that names the image to deploy. A new publication changes that file alone.
PIN = json.loads(Path(__file__).with_name("app-image.json").read_text())
REPOSITORY = "ghcr.io/softbank-hackathon-finals/demo-chat"
IMAGE = re.compile(r"(?:ghcr\.io/softbank-hackathon-finals/demo-chat"
                   r"|[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/bravo/demo-chat)"
                   r"@sha256:[0-9a-f]{64}")
# Kubernetes node architecture names. amd64 runs the publication above. arm64 runs a local build
# already imported into the target node; app-image.json records how its source was checked.
ARCHITECTURES = ("amd64", "arm64")
LOCAL_IMAGE = re.compile(r"localhost/bravo-demo-chat@sha256:[0-9a-f]{64}")
SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def pinned_image(arch="amd64"):
    """The one image registered for a node architecture."""
    if arch == "amd64":
        return f"{REPOSITORY}@{PIN['index_digest']}"
    entry = PIN.get("arm64") if arch == "arm64" else None
    image = entry.get("image") if isinstance(entry, dict) else None
    if not isinstance(image, str) or not LOCAL_IMAGE.fullmatch(image):
        raise ValueError("no demo-chat image is registered for this node architecture")
    return image


def secret_env(name, secret, key):
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}


def application(image, pull_secret=None, arch="amd64"):
    registered = pinned_image(arch)
    # This Pod receives database, object store and admin credentials, so only the registered bytes may run.
    # An unregistered build is another digest; testing/app_lab.py injects one into its own manifest, never here.
    if arch == "amd64":
        if not isinstance(image, str) or not IMAGE.fullmatch(image):
            raise ValueError("demo-chat must use the exact registered repository pinned by digest")
        if image.split("@")[1] != PIN["index_digest"]:
            raise ValueError("demo-chat must use the digest registered in app-image.json")
    elif image != registered:
        raise ValueError("demo-chat must use the image app-image.json registers for this node architecture")
    elif pull_secret is not None:
        # The arm64 image was imported into the node. A pull would ask whatever answers at "localhost".
        raise ValueError("the arm64 image exists only on the node and is never pulled")
    if pull_secret is not None and not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", pull_secret):
        raise ValueError("a Kubernetes Secret name is required")
    # An imported image must not silently fall back to an unauthenticated registry request.
    pull_policy = "IfNotPresent" if pull_secret else "Never"
    selector = {"app": NAME}
    env = {
        "APP_ENV": APP_ENV,
        "DB_HOST": f"target-pgbouncer.{NAMESPACE}.svc.cluster.local", "DB_PORT": "5432", "DB_NAME": "chat",
        "DB_SECRET_ID": DB_SECRET_ID, "AWS_ENDPOINT_URL_SECRETS_MANAGER": f"http://127.0.0.1:{COMPAT_PORT}",
        # Before cutover an upload's object would stay here while its row went to the source DB.
        "ATTACHMENT_UPLOAD_ENABLED": "false",
        "S3_BUCKET": "demo-chat-attachments", "S3_FORCE_PATH_STYLE": "true",
        "S3_ENDPOINT_URL": f"http://seaweedfs.{NAMESPACE}.svc.cluster.local:8333",
        "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true",
        "PGSSLMODE": "verify-full", "PGSSLROOTCERT": "/etc/cnpg-ca/ca.crt", "WRITE_BLOCKED": "true",
        "BRAVO_METRICS_ENDPOINT": METRICS_ENDPOINT,
        "BRAVO_METRICS_CA_FILE": f"{METRICS_DIR}/ca.crt",
        "BRAVO_METRICS_CLIENT_CERT": f"{METRICS_DIR}/tls.crt",
        "BRAVO_METRICS_CLIENT_KEY": f"{METRICS_DIR}/tls.key",
    }
    sidecar = {
        "name": "secrets-manager-compat", "image": image, "imagePullPolicy": pull_policy,
        # A restartable init container is running and started before the app makes its first call.
        "restartPolicy": "Always",
        "command": [PYTHON, f"/compat/{COMPAT_SCRIPT}"],
        "env": [{"name": "DB_SECRET_ID", "value": DB_SECRET_ID}],
        # The server binds loopback, so a tcpSocket probe from the kubelet could not reach it.
        # Bash, not Python: an interpreter start overran the timeout under this CPU limit.
        "startupProbe": {"exec": {"command": ["/bin/bash", "-c", f"exec 3<>/dev/tcp/127.0.0.1/{COMPAT_PORT}"]},
                         "timeoutSeconds": 5, "periodSeconds": 2, "failureThreshold": 60},
        "securityContext": SECURITY,
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                      "limits": {"cpu": "250m", "memory": "64Mi"}},
        "volumeMounts": [
            {"name": "compat", "mountPath": "/compat", "readOnly": True},
            {"name": "database-credentials", "mountPath": str(Path(CREDENTIALS_FILE).parent), "readOnly": True},
        ],
    }
    app = {
        "name": "app", "image": image, "imagePullPolicy": pull_policy,
        "env": [{"name": k, "value": v} for k, v in env.items()] + [
            secret_env("AWS_ACCESS_KEY_ID", "target-app-objects", "access_key"),
            secret_env("AWS_SECRET_ACCESS_KEY", "target-app-objects", "secret_key"),
            secret_env("ADMIN_TOKEN", "target-app-admin", "token"),
        ],
        "ports": [{"containerPort": 8080}],
        # The development VM emulates amd64. Liveness must not kill an app that is still importing,
        # and a slow answer must not pull a Pod out of the Service or reset its write switch.
        "startupProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "timeoutSeconds": 5,
                         "periodSeconds": 5, "failureThreshold": 60},
        "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}, "timeoutSeconds": 5},
        "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "timeoutSeconds": 5,
                          "initialDelaySeconds": 10},
        # Keep answering until the Service has dropped this Pod; otherwise requests fail during a rollout.
        "lifecycle": {"preStop": {"sleep": {"seconds": 5}}},
        "securityContext": SECURITY,
        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                      "limits": {"cpu": "500m", "memory": "512Mi"}},
        "volumeMounts": [{"name": "ca", "mountPath": "/etc/cnpg-ca", "readOnly": True},
                         {"name": "app-metrics", "mountPath": METRICS_DIR, "readOnly": True},
                         {"name": "tmp", "mountPath": "/tmp"}],
    }
    pod = {
        "automountServiceAccountToken": False,
        "serviceAccountName": NAME,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
        "initContainers": [sidecar], "containers": [app],
        # The image runs on one architecture. A render for the other never schedules, and during a
        # rolling update the old Pod keeps serving instead of being replaced by one that cannot start.
        "nodeSelector": {"kubernetes.io/arch": arch},
        "volumes": [
            {"name": "compat", "configMap": {"name": COMPAT_CONFIG}},
            {"name": "database-credentials", "secret": {
                "secretName": "target-app-credentials", "defaultMode": 0o440,
                "items": [{"key": "credentials.json", "path": "credentials.json"}]}},
            {"name": "ca", "secret": {"secretName": "target-db-ca",
                                      "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
            {"name": "app-metrics", "secret": {
                "secretName": METRICS_SECRET, "defaultMode": 0o440,
                "items": [{"key": k, "path": k} for k in ("ca.crt", "tls.crt", "tls.key")]}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if pull_secret:
        pod["imagePullSecrets"] = [{"name": pull_secret}]
    config = resource("v1", "ConfigMap", COMPAT_CONFIG, data={
        COMPAT_SCRIPT: Path(__file__).with_name(COMPAT_SCRIPT).read_text()})
    deployment = resource("apps/v1", "Deployment", NAME, spec={
        "replicas": 1, "selector": {"matchLabels": selector},
        "template": {
            "metadata": {
                # The target SeaweedFS NetworkPolicy admits only Pods with this label.
                "labels": {**LABELS, **selector, "bravo-object-client": "true"},
                "annotations": {"bravo.t30/not-exit-evidence": "secrets-manager-compat-sidecar"},
            },
            "spec": pod,
        },
    })
    service = resource("v1", "Service", NAME, spec={
        "type": "ClusterIP", "selector": selector,
        "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
    })
    return [config, deployment, service]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=ARCHITECTURES, default="amd64",
                        help="architecture of the target nodes, as kubectl get nodes reports it; default: amd64")
    parser.add_argument("--image", help="default: the image app-image.json registers for the architecture")
    parser.add_argument("--pull-secret", help="only with an agreed registry credential; default is an imported image")
    parser.add_argument("--print-image", action="store_true", help="print the pinned image reference and exit")
    args = parser.parse_args()
    try:
        pinned = pinned_image(args.arch)
        if args.print_image:
            print(pinned)
            return
        items = application(pinned if args.image is None else args.image, args.pull_secret, args.arch)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
