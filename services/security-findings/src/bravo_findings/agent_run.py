"""On-prem agent run: read Kyverno PolicyReports and the Falco DaemonSet, push to the platform outbound only.

    python -m bravo_findings.agent_run            # inside the cluster (CronJob, its ServiceAccount)
    python -m bravo_findings.agent_run --kubectl-context <ctx>   # operator check from outside

Environment: BRAVO_FINDINGS_URL (https only), BRAVO_FINDINGS_ENVIRONMENT_ID, BRAVO_FINDINGS_TOKEN_FILE (ingest
credential for kyverno · falco of that environment), optional BRAVO_FINDINGS_CA_FILE, BRAVO_FALCO_NAMESPACE.
Exit 0 when every batch was accepted, 1 otherwise. Never prints the token, response bodies or raw events.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime

from bravo_findings.agent import collect

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def in_cluster_getter(sa_dir=SA_DIR, environ=os.environ):
    base = f"https://{environ['KUBERNETES_SERVICE_HOST']}:{environ.get('KUBERNETES_SERVICE_PORT', '443')}"
    context = ssl.create_default_context(cafile=f"{sa_dir}/ca.crt")

    def get(path):
        with open(f"{sa_dir}/token") as f:  # projected token rotates; read it per request
            token = f.read().strip()
        request = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + token,
                                                               "Accept": "application/json"})
        with urllib.request.urlopen(request, context=context, timeout=10) as response:
            return json.load(response)

    return get


def kubectl_getter(context):
    def get(path):
        out = subprocess.run(["kubectl", "--context", context, "get", "--raw", path, "--request-timeout=10s"],
                             check=True, capture_output=True, text=True, timeout=20).stdout
        return json.loads(out)

    return get


def push(url, token, environment_id, batch, context, opener=urllib.request.urlopen):
    body = {"environment_id": environment_id, "events": batch["events"]}
    if batch.get("agent_error"):
        body["agent_error"] = batch["agent_error"]
    request = urllib.request.Request(f"{url}/v1/security/ingest/{batch['source']}", data=json.dumps(body).encode(),
                                     method="POST", headers={"Authorization": "Bearer " + token,
                                                             "Content-Type": "application/json"})
    with opener(request, context=context, timeout=15) as response:
        return response.status


def run(get, *, url, token, environment_id, context, falco_namespace="falco", now=None, opener=urllib.request.urlopen):
    if not url.startswith("https://"):
        raise ValueError("BRAVO_FINDINGS_URL must be https")
    failed = 0
    for batch in collect(get, now or datetime.now(UTC), falco_namespace=falco_namespace):
        line = {"source": batch["source"], "events": len(batch["events"]), "agent_error": batch.get("agent_error")}
        try:
            line["status"] = push(url.rstrip("/"), token, environment_id, batch, context, opener)
        except Exception as error:  # noqa: BLE001 — report the kind only, never the body or credential
            failed += 1
            line["error"] = type(error).__name__
        print(json.dumps(line))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubectl-context", help="read through this kubectl context instead of the Pod's account")
    args = parser.parse_args(argv)
    env = os.environ
    with open(env["BRAVO_FINDINGS_TOKEN_FILE"]) as f:
        token = f.read().strip()
    ca = env.get("BRAVO_FINDINGS_CA_FILE")
    context = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    get = kubectl_getter(args.kubectl_context) if args.kubectl_context else in_cluster_getter()
    return run(get, url=env["BRAVO_FINDINGS_URL"], token=token, environment_id=env["BRAVO_FINDINGS_ENVIRONMENT_ID"],
               context=context, falco_namespace=env.get("BRAVO_FALCO_NAMESPACE", "falco"))


if __name__ == "__main__":
    sys.exit(main())
