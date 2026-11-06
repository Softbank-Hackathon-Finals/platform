"""Persist create intents before Kubernetes calls, and UID receipts after them."""

import fcntl
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

LABEL = "bravo.dev/team-run"
KINDS = {
    "Namespace": "namespaces",
    "Job": "jobs",
    "Secret": "secrets",
    "ConfigMap": "configmaps",
}


class Failure(RuntimeError):
    pass


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def locked(root):
    with (root / "journal.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "journal.json"
        value = json.loads(path.read_text())
        yield value
        save(path, value)


def kubectl(args, data=None, timeout=60):
    return subprocess.run(
        [
            os.environ.get("BRAVO_REAL_KUBECTL", "kubectl"),
            "--request-timeout=30s",
            *args,
        ],
        input=data,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def read(ref):
    args = ["get", KINDS[ref["kind"]], ref["name"], "--ignore-not-found", "-o", "json"]
    if ref.get("namespace"):
        args += ["-n", ref["namespace"]]
    response = kubectl(args)
    if response.returncode:
        raise Failure("resource_read_unavailable")
    return json.loads(response.stdout) if response.stdout.strip() else None


def match(obj, ref, run_id):
    meta = obj["metadata"]
    if (
        meta.get("name") != ref["name"]
        or meta.get("labels", {}).get(LABEL) != run_id
        or meta.get("namespace", "") != ref.get("namespace", "")
        or (ref.get("uid") and meta.get("uid") != ref["uid"])
    ):
        raise Failure("resource_identity_changed")
    if not meta.get("uid"):
        raise Failure("resource_uid_missing")
    return meta["uid"]


def deletion(ref):
    prefix = "/apis/batch/v1" if ref["kind"] == "Job" else "/api/v1"
    ns = "/namespaces/" + ref["namespace"] if ref.get("namespace") else ""
    return prefix + ns + "/" + KINDS[ref["kind"]] + "/" + ref["name"]


def shim(args):
    """Called only by our subprocess PATH. Never persist stdin, tokens, or Secret data."""
    import yaml

    root = Path(os.environ["BRAVO_RUN_ROOT"])
    data = None
    ref = None
    if "-f" in args and args[args.index("-f") + 1] == "-":
        import sys

        data = sys.stdin.read()
    if args[:1] == ["create"] and data:
        obj = yaml.safe_load(data)
        kind = obj.get("kind")
        meta = obj.get("metadata", {})
        if kind in KINDS and (
            kind == "Namespace" or meta.get("namespace") == "bravo-rehearsal-control"
        ):
            name = meta.get("name", "")
            if not re.fullmatch(r"[a-z0-9][-a-z0-9]{0,62}", name):
                raise Failure("create_name_invalid")
            with locked(root) as record:
                run_id = record["run_id"]
                if kind == "Namespace":
                    if not name.startswith(("rehearsal-", "isolation-victim-")):
                        raise Failure("namespace_outside_test_boundary")
                else:
                    owned = [
                        x["name"]
                        for x in record["resources"]
                        if x["kind"] == "Namespace"
                    ]
                    if not any(name.startswith("t15-cred-" + ns) for ns in owned):
                        raise Failure("control_resource_outside_run")
                ref = {
                    "kind": kind,
                    "name": name,
                    "namespace": meta.get("namespace", ""),
                }
                if any(all(x[k] == ref[k] for k in ref) for x in record["resources"]):
                    raise Failure("create_intent_already_exists_inspect_same_run")
                meta.setdefault("labels", {})[LABEL] = run_id
                record["resources"].append(ref.copy())
            data = json.dumps(obj)
    response = kubectl(args, data, timeout=300)
    if ref:
        try:
            obj = json.loads(response.stdout) if response.returncode == 0 else read(ref)
            if obj:
                with locked(root) as record:
                    uid = match(obj, ref, record["run_id"])
                    for item in record["resources"]:
                        if all(item[k] == ref[k] for k in ref):
                            item["uid"] = uid
        except (ValueError, KeyError, Failure):
            # Intent remains available for same-name readback, never blind replay.
            pass
    if args[:1] == ["get"] and response.returncode == 0:
        try:
            obj = json.loads(response.stdout)
            objects = (
                obj.get("items", []) if obj.get("kind", "").endswith("List") else [obj]
            )
            if obj.get("kind") == "Job" and any(
                c.get("type") == "Failed" and c.get("status") == "True"
                for c in obj.get("status", {}).get("conditions", [])
            ):
                meta = obj["metadata"]
                current = json.loads((root / "journal.json").read_text())
                owned = any(
                    r["kind"] == "Namespace" and r["name"] == meta.get("namespace")
                    for r in current["resources"]
                )
                if owned:
                    pods = kubectl(
                        [
                            "get",
                            "pods",
                            "-n",
                            meta["namespace"],
                            "-l",
                            "batch.kubernetes.io/controller-uid=" + meta["uid"],
                            "-o",
                            "json",
                        ]
                    )
                    if pods.returncode == 0:
                        objects += json.loads(pods.stdout)["items"]
                        for pod in objects[1:]:
                            if meta["name"] == "cred-app-db":
                                log = kubectl(
                                    [
                                        "logs",
                                        pod["metadata"]["name"],
                                        "-n",
                                        meta["namespace"],
                                        "--limit-bytes=1024",
                                    ]
                                )
                                try:
                                    observed = json.loads(log.stdout)
                                    if set(observed) == {"observation"} and observed[
                                        "observation"
                                    ] in ("yes", "no", "unknown"):
                                        with locked(root) as current:
                                            current.setdefault(
                                                "probe_observations", {}
                                            )[pod["metadata"]["name"]] = observed
                                except (ValueError, TypeError):
                                    pass
            with locked(root) as record:
                namespaces = {
                    r["name"] for r in record["resources"] if r["kind"] == "Namespace"
                }
                names = {r["name"] for r in record["resources"]}
                for item in objects:
                    meta = item.get("metadata", {})
                    if item.get("kind") != "Pod" or not (
                        meta.get("namespace") in namespaces
                        or any(
                            meta.get("name", "").startswith(n + "-")
                            for n in names
                            if n.startswith("t15-cred-")
                        )
                    ):
                        continue
                    statuses = item.get("status", {})
                    snapshot = {
                        "uid": meta.get("uid"),
                        "namespace": meta.get("namespace"),
                        "phase": statuses.get("phase"),
                        "containers": [],
                    }
                    for container in statuses.get(
                        "initContainerStatuses", []
                    ) + statuses.get("containerStatuses", []):
                        state = container.get("state", {})
                        snapshot["containers"].append(
                            {
                                "name": container.get("name"),
                                "image": container.get("image"),
                                "image_id": container.get("imageID"),
                                "state": {
                                    k: {
                                        field: val
                                        for field, val in v.items()
                                        if field in ("reason", "exitCode")
                                    }
                                    for k, v in state.items()
                                },
                            }
                        )
                    record.setdefault("pod_observations", {})[meta["name"]] = snapshot
        except (ValueError, TypeError, KeyError):
            pass
    if args[:1] == ["logs"] and response.returncode == 0:
        try:
            observation = json.loads(response.stdout)
            keys = {"login", "role", "value", "observation", "reach"}
            values = {
                "allow",
                "allowed",
                "denied",
                "deny",
                "unknown",
                "ok",
                "failed",
                "deleted",
                "yes",
                "no",
            }
            if (
                observation
                and set(observation) <= keys
                and all(v in values for v in observation.values())
            ):
                with locked(root) as record:
                    record.setdefault("probe_observations", {})[args[1]] = observation
        except (ValueError, TypeError):
            pass
    import sys

    sys.stdout.write(response.stdout)
    sys.stderr.write(response.stderr)
    return response.returncode
