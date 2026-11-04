"""Read-only resource measurement for one stable container per Deployment.

Usage: stats.py NODE SECONDS INTERVAL NAMESPACE/DEPLOYMENT/CONTAINER ...
Missing, replaced, ambiguous or stale samples make the whole interval UNKNOWN
(exit 2). Such intervals must not be used as resource sizing evidence.
"""

import argparse
import json
import re
import subprocess
import time
from dataclasses import dataclass


class Unobservable(ValueError):
    """A bounded reason code, never a subprocess error or cluster response."""


@dataclass(frozen=True)
class Target:
    namespace: str
    deployment: str
    container: str

    @classmethod
    def parse(cls, value):
        parts = value.split("/")
        if len(parts) != 3 or any(not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", p) for p in parts):
            raise argparse.ArgumentTypeError("expected namespace/deployment/container")
        return cls(*parts)

    def __str__(self):
        return f"{self.namespace}/{self.deployment}/{self.container}"


def node_exec(node, *cmd):
    try:
        return subprocess.run(
            ["docker", "exec", node, *cmd], capture_output=True, text=True, check=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise Unobservable("NODE_READ_FAILED") from None


def controller(metadata, kind, uid):
    return any(o.get("kind") == kind and o.get("uid") == uid and o.get("controller") is True
               for o in metadata.get("ownerReferences", []))


def resolve(target, items):
    """Resolve the owner UID chain, not a name prefix or untrusted app label."""
    deployments = [i for i in items if i["kind"] == "Deployment"
                   and i["metadata"]["name"] == target.deployment]
    if len(deployments) != 1:
        raise Unobservable("DEPLOYMENT_MISSING_OR_AMBIGUOUS")
    deployment = deployments[0]
    if deployment["spec"].get("replicas", 1) != 1 or deployment["metadata"].get("deletionTimestamp"):
        raise Unobservable("DEPLOYMENT_NOT_SINGLE_REPLICA")
    dep_uid = deployment["metadata"]["uid"]
    rs_uids = {i["metadata"]["uid"] for i in items if i["kind"] == "ReplicaSet"
               and controller(i["metadata"], "Deployment", dep_uid)}
    pods = [i for i in items if i["kind"] == "Pod"
            and any(controller(i["metadata"], "ReplicaSet", uid) for uid in rs_uids)
            and i.get("status", {}).get("phase") not in {"Succeeded", "Failed"}]
    if len(pods) != 1 or pods[0]["metadata"].get("deletionTimestamp"):
        raise Unobservable("POD_MISSING_OR_AMBIGUOUS")
    pod = pods[0]
    statuses = [s for s in pod.get("status", {}).get("containerStatuses", [])
                if s["name"] == target.container and s.get("ready") is True
                and "running" in s.get("state", {})]
    if pod.get("status", {}).get("phase") != "Running" or len(statuses) != 1:
        raise Unobservable("CONTAINER_NOT_READY")
    container_id = statuses[0].get("containerID", "").removeprefix("containerd://")
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise Unobservable("CONTAINER_ID_INVALID")
    return dep_uid, pod["metadata"]["uid"], pod["metadata"]["name"], container_id


def integer(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise Unobservable("INVALID_COUNTER")
    return int(value)


def anon_bytes(node, container_id):
    pid = node_exec(node, "crictl", "inspect", "-o", "go-template", "--template", "{{.info.pid}}", container_id).strip()
    if not re.fullmatch(r"[1-9][0-9]*", pid):
        raise Unobservable("PROCESS_ID_INVALID")
    cgroup = node_exec(node, "cat", f"/proc/{pid}/cgroup").strip()
    if not re.fullmatch(r"0::/[a-zA-Z0-9_./:-]+", cgroup) or ".." in cgroup:
        raise Unobservable("CGROUP_PATH_INVALID")
    stat = node_exec(node, "cat", f"/sys/fs/cgroup{cgroup[3:]}/memory.stat")
    values = [integer(line.split()[1]) for line in stat.splitlines() if line.startswith("anon ")]
    if len(values) != 1:
        raise Unobservable("ANON_COUNTER_MISSING")
    return values[0]


def inventory(node, targets):
    return {ns: json.loads(node_exec(node, "kubectl", "get", "deployments,replicasets,pods", "-n", ns, "-o", "json"))["items"]
            for ns in sorted({t.namespace for t in targets})}


def sample(node, targets):
    before = inventory(node, targets)
    rows = json.loads(node_exec(node, "crictl", "stats", "-o", "json"))["stats"]
    result = {}
    for target in targets:
        identity = resolve(target, before[target.namespace])
        _, pod_uid, pod_name, container_id = identity
        matched = []
        for row in rows:
            attrs = row["attributes"]
            labels = attrs.get("labels", {})
            if (attrs["id"] == container_id
                    and labels.get("io.kubernetes.container.name") == target.container
                    and labels.get("io.kubernetes.pod.namespace") == target.namespace
                    and labels.get("io.kubernetes.pod.name") == pod_name
                    and labels.get("io.kubernetes.pod.uid") == pod_uid):
                matched.append(row)
        if len(matched) != 1:
            raise Unobservable("CRI_SAMPLE_MISSING_OR_AMBIGUOUS")
        row = matched[0]
        result[target] = (identity, integer(row["memory"]["workingSetBytes"]["value"]),
                          integer(row["cpu"]["usageCoreNanoSeconds"]["value"]),
                          integer(row["cpu"]["timestamp"]), anon_bytes(node, container_id))
    after = inventory(node, targets)
    if any(resolve(t, after[t.namespace]) != result[t][0] for t in targets):
        raise Unobservable("IDENTITY_CHANGED_DURING_SAMPLE")
    return result


def summarize(samples, targets):
    if len(samples) < 2:
        raise Unobservable("INSUFFICIENT_SAMPLES")
    output = []
    for target in targets:
        rows = [s[target] for s in samples]
        cpu_rates = []
        for previous, current in zip(rows, rows[1:], strict=False):
            if previous[0] != current[0]:
                raise Unobservable("IDENTITY_CHANGED")
            if current[2] < previous[2] or current[3] <= previous[3]:
                raise Unobservable("COUNTER_RESET_OR_STALE")
            cpu_rates.append((current[2] - previous[2]) / (current[3] - previous[3]))
        average = (rows[-1][2] - rows[0][2]) / (rows[-1][3] - rows[0][3])
        output.append(f"  VALID {target} samples={len(rows)}"
                      f" 최대 메모리 {max(r[1] for r in rows) / 2**20:.1f}MiB"
                      f" (anon {max(r[4] for r in rows) / 2**20:.1f}MiB)"
                      f" 평균 CPU {average * 1000:.1f}m 최대 CPU(간격) {max(cpu_rates) * 1000:.1f}m")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node")
    parser.add_argument("seconds", type=float)
    parser.add_argument("interval", type=float)
    parser.add_argument("targets", type=Target.parse, nargs="+")
    args = parser.parse_args(argv)
    if (not 0 < args.interval <= args.seconds <= 3600 or len(args.targets) != len(set(args.targets))):
        parser.error("unique targets and 0 < interval <= seconds <= 3600 required")
    try:
        samples = [sample(args.node, args.targets)]
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            time.sleep(min(args.interval, max(0, end - time.monotonic())))
            samples.append(sample(args.node, args.targets))
            # Validate each adjacent pair immediately; do not average across a restart.
            summarize(samples[-2:], args.targets)
        for line in summarize(samples, args.targets):
            print(line)
        return 0
    except Unobservable as error:
        print(f"UNKNOWN resource interval: {error}")
    except (KeyError, ValueError, TypeError, IndexError):
        print("UNKNOWN resource interval: MALFORMED_SAMPLE")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
