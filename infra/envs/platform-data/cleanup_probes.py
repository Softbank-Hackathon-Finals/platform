"""Remove explicitly owned completed T7 probe Jobs; preserve bundles and database data."""
import json
import re
import subprocess
import sys

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml", "-n", "platform-state"]


def kube(*args):
    return subprocess.check_output(K + list(args), text=True)


def terminal(job):
    return any(c["type"] in ("Complete", "Failed") and c["status"] == "True"
               for c in job.get("status", {}).get("conditions", []))


def main():
    settings = json.load(sys.stdin)
    names = settings["jobs"]
    if not 1 <= len(names) <= 8 or any(not re.fullmatch(r"t7-db-[a-z0-9-]{1,32}", n) for n in names):
        raise ValueError("invalid_probe_names")
    removed = 0
    for name in names:
        text = kube("get", "job", name, "--ignore-not-found", "-o", "json")
        if not text:
            continue
        job = json.loads(text)
        if job["spec"]["template"]["metadata"].get("labels", {}).get("bravo.dev/probe") != "t7-database":
            raise ValueError("job_owned_elsewhere")
        if not terminal(job):
            raise ValueError("probe_still_running")
        kube("delete", "job", name, "--wait=true", "--timeout=60s")
        removed += 1
    remaining = json.loads(kube("get", "jobs", "-o", "json"))["items"]
    active = any(job["spec"]["template"].get("metadata", {}).get("labels", {}).get("bravo.dev/probe") == "t7-database"
                 and not terminal(job) for job in remaining)
    if not active:
        kube("delete", "networkpolicy", "t7-verification", "--ignore-not-found")
    print(json.dumps({"removed_probe_jobs": removed, "immutable_bundles_preserved": True,
                      "temporary_download_policy_removed": not active, "database_resources_preserved": True}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
