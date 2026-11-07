#!/usr/bin/env python3
"""Install an environment's node files on a running RKE2 server node (T44 cluster-audit and later modules).

    sudo python3 apply.py <bundle dir: infra/iac/nodes/<environment>> --cluster-uid <kube-system UID>
                          [--state-digest <reviewed state_digest>]
                          [--sha256 <node path>=<reviewed sha256> ...] [--apply]

Without --apply it only reports what would change. With --apply, for each file whose bytes differ:
back up the live file, install the reviewed bytes atomically with the declared mode, then restart
what reads it (the API server reads its audit policy only at start). If anything fails after the
first install, the previous bytes and modes are put back and the API server is waited for again.
Prints one JSON line. Standard library only.

The bundle is not trusted. Where it writes: only paths, modes and restarts in ALLOWED are accepted
(a manifest naming /etc/sudoers.d/x is refused before anything is written). What it writes: the
manifest's sha256 only says the folder agrees with itself, so --apply requires every file's reviewed
sha256 (`files[].sha256` of GET /v1/iac/environments/{id}/node-files) and refuses other bytes, such as a
policy edited to `level: None` with the state_digest copied. --state-digest only checks which reviewed
state the manifest claims; it does not bind bytes.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

RKE2 = Path("/var/lib/rancher/rke2")
KUBECTL = [str(RKE2 / "bin/kubectl"), "--kubeconfig=/etc/rancher/rke2/rke2.yaml", "--request-timeout=20s"]
CRICTL = [str(RKE2 / "bin/crictl"), "--runtime-endpoint", "unix:///run/k3s/containerd/containerd.sock"]
APISERVER_MANIFEST = RKE2 / "agent/pod-manifests/kube-apiserver.yaml"
API_SERVER = "StaticPod/kube-system/kube-apiserver"
AUDIT_POLICY = "/etc/rancher/rke2/audit-policy.yaml"
# Node files this applier may write, with their only allowed mode and restart. Same set as the
# services/iac node modules (a test pins it); infra/envs/onprem/render-user-data.sh embeds it too.
ALLOWED = {AUDIT_POLICY: {"mode": "0600", "restart": [API_SERVER]}}
ROOT = Path("/")  # tests install under a temporary root
READY_TIMEOUT = 240


class Refused(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle(directory: Path, state_digest: str | None = None, reviewed: dict | None = None) -> list[dict]:
    """The manifest's files with their bytes. Refuses paths, modes or restarts outside ALLOWED, a
    manifest of another reviewed state, bytes that differ from the manifest, and, when `reviewed`
    ({path: sha256}) is given, any file set or bytes other than the reviewed ones."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "iac-node-files.v1" or not isinstance(manifest.get("files"), list):
        raise Refused("bundle_manifest_invalid")
    if state_digest is not None and manifest.get("state_digest") != state_digest:
        raise Refused("bundle_state_digest_mismatch")
    rows = []
    for row in manifest["files"]:
        allowed = ALLOWED.get(row.get("path"))
        if allowed is None:
            raise Refused("bundle_path_not_allowed")
        if row.get("mode") != allowed["mode"] or row.get("restart") != allowed["restart"]:
            raise Refused("bundle_mode_or_restart_not_allowed")
        data = (directory / row["path"].lstrip("/")).read_bytes()
        if sha(data) != row.get("sha256"):
            raise Refused("bundle_sha_mismatch")
        rows.append({**row, "data": data})
    if reviewed is not None and reviewed != {row["path"]: row["sha256"] for row in rows}:
        raise Refused("bundle_not_reviewed_bytes")
    return rows


def kubectl(*args: str) -> str:
    return subprocess.run(KUBECTL + list(args), capture_output=True, text=True, check=True).stdout


def running_apiserver() -> str | None:
    out = subprocess.run(CRICTL + ["ps", "--name", "kube-apiserver", "--state", "running", "-q"],
                         capture_output=True, text=True, check=True).stdout.split()
    if len(out) > 1:
        raise Refused("api_server_container_ambiguous")
    return out[0] if out else None


def crashed_since(since_ns: int, previous: str | None) -> bool:
    """A kube-apiserver container created after `since_ns` has already exited: it crashed (a healthy
    API server never exits by itself). Measured on a real node: waiting the full timeout instead let
    kubelet's crash backoff grow and stretched a failed install to ~6.5 min of API outage."""
    out = subprocess.run(CRICTL + ["ps", "-a", "--name", "kube-apiserver", "-o", "json"],
                         capture_output=True, text=True, check=True).stdout
    return any(c.get("id") != previous and c.get("state") == "CONTAINER_EXITED" and int(c.get("createdAt", 0)) > since_ns
               for c in json.loads(out or "{}").get("containers", []))


def wait_ready(previous: str | None, since_ns: int) -> str:
    until = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < until:
        time.sleep(3)
        try:
            if crashed_since(since_ns, previous):
                raise Refused("api_server_crashed")
            current = running_apiserver()
            if current and current != previous and kubectl("get", "--raw", "/readyz").strip() == "ok":
                return current
        except subprocess.CalledProcessError:
            continue
    raise Refused("api_server_not_ready")


def restart_apiserver() -> str:
    """Stop the running container (kubelet restarts the static pod) and wait for a new ready one.
    With no running container (a crashloop, say), only wait: kubelet keeps restarting it. A new
    container that exits is a crash and fails at once, so a rollback starts before backoff grows."""
    since_ns = time.time_ns()
    before = running_apiserver()
    if before:
        subprocess.run(CRICTL + ["stop", before], capture_output=True, check=True)
    return wait_ready(before, since_ns)


def install(path: Path, data: bytes, mode: int) -> None:
    """Write next to the target without following links, fsync, rename, then fsync the directory."""
    tmp = path.with_name(f".{path.name}.bravo-iac-tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def audit_log_path() -> Path | None:
    for line in APISERVER_MANIFEST.read_text().splitlines():
        if "--audit-log-path=" in line:
            return Path(line.split("--audit-log-path=", 1)[1].strip().strip('"'))
    return None


def node_path(path: str) -> Path:
    return ROOT / path.lstrip("/")


def _rollback(report, changed, installed) -> None:
    failed = []
    for path, live, live_mode in installed:  # the previous bytes and modes go back
        try:
            if live is not None:
                install(path, live, live_mode if live_mode is not None else 0o600)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))  # keep going, and say which file still holds the new bytes
    report["rolled_back"] = not failed
    if failed:
        report["rollback_failed"] = failed
    if any(API_SERVER in row["restart"] for row, *_ in changed):
        try:
            restart_apiserver()
            report["rollback_ready"] = True
        except (Refused, subprocess.CalledProcessError):
            report["rollback_ready"] = False  # the API server is not back: an operator must look now


def _reviewed(pairs: list[str] | None) -> dict | None:
    if pairs is None:
        return None
    reviewed = dict(pair.split("=", 1) for pair in pairs if "=" in pair)
    if len(reviewed) != len(pairs):
        raise Refused("reviewed_sha256_invalid")
    return reviewed


def run(args) -> dict:
    report = {"status": "FAIL", "applied": args.apply, "files": [], "restarted": []}
    if args.apply and not args.sha256:
        raise Refused("reviewed_sha256_required")  # writing as root needs bytes someone reviewed
    rows = bundle(Path(args.bundle), args.state_digest, _reviewed(args.sha256))
    uid = json.loads(kubectl("get", "ns", "kube-system", "-o", "json"))["metadata"]["uid"]
    report["uid"] = uid
    if uid != args.cluster_uid:
        raise Refused("cluster_identity_mismatch")
    manifest_text = APISERVER_MANIFEST.read_text()
    changed = []
    for row in rows:
        path = node_path(row["path"])
        if API_SERVER in row["restart"] and f"--audit-policy-file={row['path']}" not in manifest_text:
            raise Refused("api_server_does_not_read_this_file")
        live = path.read_bytes() if path.exists() else None
        live_mode = stat.S_IMODE(path.stat().st_mode) if live is not None else None
        entry = {"path": row["path"], "before_sha256": sha(live) if live is not None else None,
                 "after_sha256": row["sha256"], "action": "unchanged" if live == row["data"] else "install"}
        report["files"].append(entry)
        if entry["action"] == "install":
            changed.append((row, path, live, live_mode, entry))
    if not args.apply or not changed:
        report["status"] = "PASS"
        return report

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    log = audit_log_path()
    before = log.stat() if log and log.exists() else None
    size_before = before.st_size if before else None
    installed = []
    try:
        for row, path, live, live_mode, entry in changed:
            if live is not None:
                backup = path.with_name(f"{path.name}.bravo-backup-{stamp}")
                install(backup, live, 0o600)
                entry["backup"] = str(backup)
            install(path, row["data"], int(row["mode"], 8))
            installed.append((path, live, live_mode))
        for target in sorted({r for row, *_ in changed for r in row["restart"]}):
            report["restarted"].append({"target": target, "container": restart_apiserver()})
        if any(row["path"] == AUDIT_POLICY for row, *_ in changed):
            kubectl("get", "ns", "kube-system", "-o", "name")
            time.sleep(5)
            after = log.stat() if log and log.exists() else None
            size_after = after.st_size if after else None
            # A rotation in between starts a new file (another inode): any bytes in it are new events.
            rotated = bool(before and after and after.st_ino != before.st_ino)
            growing = size_after is not None and size_after > (0 if rotated else size_before or 0)
            report["audit_log"] = {"path": str(log) if log else None, "bytes_before": size_before,
                                   "bytes_after": size_after, "growing": growing}
            if not growing:  # the reason this module exists: an audit log that never fills
                raise Refused("audit_log_not_growing")
    except (Refused, OSError, ValueError, subprocess.CalledProcessError) as error:
        report["reason"] = str(error) if isinstance(error, Refused) else type(error).__name__
        _rollback(report, changed, installed)
        return report
    report["status"] = "PASS"
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bundle")
    p.add_argument("--cluster-uid", required=True)
    p.add_argument("--state-digest")
    p.add_argument("--sha256", action="append", metavar="PATH=SHA256",
                   help="reviewed bytes of one node file (repeat per file); required with --apply")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    try:
        report = run(args)
    except Refused as error:
        report = {"status": "FAIL", "applied": args.apply, "reason": str(error)}
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        report = {"status": "FAIL", "applied": args.apply, "reason": type(error).__name__}
    print(json.dumps(report))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
