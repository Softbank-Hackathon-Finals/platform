#!/usr/bin/env python3
"""Install an environment's node files on a running RKE2 server node (T44 cluster-audit and later modules).

    sudo python3 apply.py <bundle dir: infra/iac/nodes/<environment>> --cluster-uid <kube-system UID> [--apply]

Without --apply it only reports what would change. With --apply, for each file whose bytes differ:
back up the live file, install the reviewed bytes atomically with the declared mode, then restart
what reads it (the API server reads its audit policy only at start). If the API server does not come
back ready, the backup is restored and the API server restarted again. Prints one JSON line.
Standard library only; the node is never changed before the bundle and the cluster identity check.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RKE2 = Path("/var/lib/rancher/rke2")
KUBECTL = [str(RKE2 / "bin/kubectl"), "--kubeconfig=/etc/rancher/rke2/rke2.yaml", "--request-timeout=20s"]
CRICTL = [str(RKE2 / "bin/crictl"), "--runtime-endpoint", "unix:///run/k3s/containerd/containerd.sock"]
APISERVER_MANIFEST = RKE2 / "agent/pod-manifests/kube-apiserver.yaml"
API_SERVER = "StaticPod/kube-system/kube-apiserver"
READY_TIMEOUT = 240


class Refused(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle(directory: Path) -> list[dict]:
    """The manifest's files with their bytes; refuses a bundle whose bytes differ from the manifest."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "iac-node-files.v1" or not isinstance(manifest.get("files"), list):
        raise Refused("bundle_manifest_invalid")
    rows = []
    for row in manifest["files"]:
        path = row.get("path", "")
        if not path.startswith("/") or ".." in Path(path).parts or row.get("mode") not in ("0600", "0644"):
            raise Refused("bundle_path_invalid")
        data = (directory / path.lstrip("/")).read_bytes()
        if sha(data) != row.get("sha256"):
            raise Refused("bundle_sha_mismatch")
        rows.append({**row, "data": data})
    return rows


def kubectl(*args: str) -> str:
    return subprocess.run(KUBECTL + list(args), capture_output=True, text=True, check=True).stdout


def apiserver_container() -> str:
    out = subprocess.run(CRICTL + ["ps", "--name", "kube-apiserver", "--state", "running", "-q"],
                         capture_output=True, text=True, check=True).stdout.split()
    if len(out) != 1:
        raise Refused("api_server_container_unknown")
    return out[0]


def wait_ready(previous: str) -> str:
    until = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < until:
        time.sleep(3)
        try:
            current = apiserver_container()
            if current != previous and kubectl("get", "--raw", "/readyz").strip() == "ok":
                return current
        except (Refused, subprocess.CalledProcessError):
            continue
    raise Refused("api_server_not_ready")


def restart_apiserver() -> str:
    before = apiserver_container()
    subprocess.run(CRICTL + ["stop", before], capture_output=True, check=True)  # kubelet restarts the static pod
    return wait_ready(before)


def install(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.bravo-iac-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def audit_log_path() -> Path | None:
    for line in APISERVER_MANIFEST.read_text().splitlines():
        if "--audit-log-path=" in line:
            return Path(line.split("--audit-log-path=", 1)[1].strip().strip('"'))
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bundle")
    p.add_argument("--cluster-uid", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    report = {"status": "FAIL", "applied": args.apply, "files": [], "restarted": []}
    try:
        rows = bundle(Path(args.bundle))
        uid = json.loads(kubectl("get", "ns", "kube-system", "-o", "json"))["metadata"]["uid"]
        report["uid"] = uid
        if uid != args.cluster_uid:
            raise Refused("cluster_identity_mismatch")
        manifest_text = APISERVER_MANIFEST.read_text()
        changed = []
        for row in rows:
            path = Path(row["path"])
            if API_SERVER in row["restart"] and f"--audit-policy-file={path}" not in manifest_text:
                raise Refused("api_server_does_not_read_this_file")
            live = path.read_bytes() if path.exists() else None
            entry = {"path": row["path"], "before_sha256": sha(live) if live is not None else None,
                     "after_sha256": row["sha256"], "action": "unchanged" if live == row["data"] else "install"}
            report["files"].append(entry)
            if entry["action"] == "install":
                changed.append((row, path, live))
        if not args.apply or not changed:
            report["status"] = "PASS"
            print(json.dumps(report))
            return 0
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        log = audit_log_path()
        size_before = log.stat().st_size if log and log.exists() else None
        for row, path, live in changed:
            if live is not None:
                backup = path.with_name(f"{path.name}.bravo-backup-{stamp}")
                install(backup, live, 0o600)
                next(e for e in report["files"] if e["path"] == row["path"])["backup"] = str(backup)
            install(path, row["data"], int(row["mode"], 8))
        restarts = sorted({r for row, _, _ in changed for r in row["restart"]})
        try:
            for target in restarts:
                if target != API_SERVER:
                    raise Refused("restart_target_unsupported")
                report["restarted"].append({"target": target, "container": restart_apiserver()})
        except Refused as error:
            for _row, path, live in changed:  # put the previous bytes back and restart once more
                if live is not None:
                    install(path, live, 0o600)
            if API_SERVER in restarts:
                try:
                    restart_apiserver()
                except Refused:
                    pass
            report["rolled_back"] = True
            raise error
        kubectl("get", "ns", "kube-system", "-o", "name")
        time.sleep(5)
        size_after = log.stat().st_size if log and log.exists() else None
        report["audit_log"] = {"path": str(log) if log else None, "bytes_before": size_before, "bytes_after": size_after,
                               "growing": None if size_after is None else size_after > (size_before or 0)}
        report["status"] = "PASS"
    except Refused as error:
        report["reason"] = str(error)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        report["reason"] = type(error).__name__
    print(json.dumps(report))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
