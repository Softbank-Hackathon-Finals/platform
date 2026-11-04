"""Read-only, operator-run qualification of deliberately broken CNPG restores.

This is not a DR-01 PASS producer. Preparation/authorization are separate: use
only disposable restore clusters and operator-retained creation receipts. Never
feed application/model-provided UIDs or manifests into this qualification tool.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

CASES = {"missing-backup", "wrong-ca", "wrong-credentials", "missing-wal"}
DNS = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def spec_digest(resource):
    return digest(json.dumps(resource["spec"], sort_keys=True, separators=(",", ":")).encode())


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def failure_reason(case, backup_id, raw, *, wal_segment=None):
    """Only structured error fields from the bound recovery Pod are inspected.

    Generic 'restore failed', pending Pods, timeouts, successful exit and text in
    an informational message are not evidence of the injected failure.
    """
    if case not in CASES:
        raise ValueError("unsupported qualification case")
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9]{8}T[0-9]{6}", backup_id):
        raise ValueError("exact backup ID required")
    if case == "missing-wal" and (
        not isinstance(wal_segment, str) or not re.fullmatch(r"[0-9A-F]{24}", wal_segment)
    ):
        raise ValueError("operator-registered first required WAL segment required")
    if case != "missing-wal" and wal_segment is not None:
        raise ValueError("WAL scope is only accepted for missing-wal")
    if not isinstance(raw, bytes) or len(raw) > 256 * 1024:
        raise ValueError("bounded log bytes required")
    for line in raw.splitlines():
        try:
            event = decode(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or event.get("level") not in {"error", "fatal"}:
            continue
        error = event.get("error", "")
        if case == "missing-backup" and isinstance(error, str):
            if re.search(
                r"no backup found with ID " + re.escape(backup_id) + r"(?![A-Za-z0-9])", error
            ):
                return "requested_backup_missing"
        if case == "missing-wal" and isinstance(error, str):
            # Bind the first WAL to reviewed backup metadata, not an arbitrary
            # missing archive object, network timeout, or absent future WAL.
            expected = (
                "encountered an error while checking the presence of first needed WAL "
                "in the archive: object storage or file not found "
                + wal_segment
                + ": WAL not found"
            )
            if event.get("msg") in {"Error while restoring a backup", "restore error"} and (
                error == "rpc error: code = Unknown desc = " + expected
                or error == "while restoring cluster: rpc error: code = Unknown desc = " + expected
            ):
                return "required_initial_wal_missing"
        # The Barman plugin records command stderr separately from its generic
        # exit-status error. Do not match command options or arbitrary messages.
        if event.get("logger") != "barman" or event.get("command") != "barman-cloud-backup-list":
            continue
        stderr = event.get("stderr")
        if not isinstance(stderr, str):
            continue
        if case == "wrong-ca" and "CERTIFICATE_VERIFY_FAILED" in stderr:
            return "certificate_verification_failed"
        if case == "wrong-credentials" and re.search(
            r"\((?:InvalidAccessKeyId|AccessDenied|SignatureDoesNotMatch)\)", stderr
        ):
            return "storage_access_rejected"
    return None


def owner(resource, kind, name, uid):
    refs = resource.get("metadata", {}).get("ownerReferences", [])
    return any(
        r.get("kind") == kind
        and r.get("name") == name
        and r.get("uid") == uid
        and r.get("controller") is True
        for r in refs
    )


def assert_cluster(resource, *, name, uid, expected_spec):
    if (
        resource.get("kind") != "Cluster"
        or resource.get("apiVersion") != "postgresql.cnpg.io/v1"
        or resource.get("metadata", {}).get("name") != name
        or resource.get("metadata", {}).get("uid") != uid
        or resource.get("metadata", {}).get("deletionTimestamp")
        or spec_digest(resource) != expected_spec
        or any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in resource.get("status", {}).get("conditions", [])
        )
    ):
        raise ValueError("restore identity/spec changed or restore is Ready")


def failed_recovery(pod):
    # An unrelated init-container or OOM is not proof the restore command failed.
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return any(
        s.get("name") == "full-recovery"
        and s.get("state", {}).get("terminated", {}).get("reason") == "Error"
        and type(s.get("state", {}).get("terminated", {}).get("exitCode")) is int
        and s["state"]["terminated"]["exitCode"] != 0
        for s in statuses
    )


class Kubectl:
    def __init__(self, executable, kubeconfig, namespace):
        if not DNS.fullmatch(namespace):
            raise ValueError("namespace DNS label required")
        self.deadline = time.monotonic() + 120
        self.command = [
            str(executable),
            "--kubeconfig",
            str(kubeconfig),
            "-n",
            namespace,
            "--request-timeout=10s",
        ]

    def raw(self, *args, limit=1024 * 1024):
        # Files bound memory while communicate waits; only a bounded suffix of
        # each log is requested. No raw stderr/credentials are emitted.
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("qualification read deadline exceeded")
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            result = subprocess.run(
                self.command + list(args), stdout=out, stderr=err, timeout=min(15, remaining)
            )
            if result.returncode or out.tell() > limit:
                raise ValueError("Kubernetes read unavailable or oversized")
            out.seek(0)
            return out.read(limit + 1)

    def get(self, kind, name):
        if not DNS.fullmatch(name):
            raise ValueError("resource DNS label required")
        return decode(self.raw("get", kind, name, "-o", "json"))


def inspect(client, *, case, name, uid, expected_spec, backup_id, wal_segment=None):
    if case not in CASES or not DNS.fullmatch(name) or not SHA.fullmatch(expected_spec):
        raise ValueError("bounded case, name and spec digest required")
    if str(UUID(uid)) != uid:
        raise ValueError("canonical creation UID required")
    before = client.get("cluster", name)
    assert_cluster(before, name=name, uid=uid, expected_spec=expected_spec)
    if (
        before["spec"]
        .get("bootstrap", {})
        .get("recovery", {})
        .get("recoveryTarget", {})
        .get("backupID")
        != backup_id
    ):
        raise ValueError("requested backup ID differs from restore spec")
    pods = decode(client.raw("get", "pods", "-l", "cnpg.io/cluster=" + name, "-o", "json"))
    items = pods.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ValueError("bounded recovery Pods required")
    selected = None
    for pod in items:
        if not failed_recovery(pod):
            continue
        refs = [
            r
            for r in pod.get("metadata", {}).get("ownerReferences", [])
            if r.get("kind") == "Job" and r.get("controller") is True
        ]
        if len(refs) != 1:
            continue
        job = client.get("job", refs[0]["name"])
        if job.get("metadata", {}).get("uid") != refs[0].get("uid") or not owner(
            job, "Cluster", name, uid
        ):
            continue
        pod_name = pod["metadata"]["name"]
        pod_uid = pod["metadata"]["uid"]
        for container in ("full-recovery", "plugin-barman-cloud"):
            raw = client.raw(
                "logs",
                pod_name,
                "-c",
                container,
                "--tail=150",
                "--limit-bytes=262144",
                limit=262144,
            )
            reason = failure_reason(case, backup_id, raw, wal_segment=wal_segment)
            if reason is not None:
                after_pod = client.get("pod", pod_name)
                if after_pod["metadata"]["uid"] != pod_uid or not failed_recovery(after_pod):
                    raise ValueError("recovery Pod changed during readback")
                selected = {
                    "reason": reason,
                    "log_sha256": digest(raw),
                    "pod_identity_sha256": digest(pod_uid.encode()),
                    "restore_process_failed": True,
                }
                break
        if selected:
            break
    after = client.get("cluster", name)
    assert_cluster(after, name=name, uid=uid, expected_spec=expected_spec)
    if selected is None:
        raise ValueError("injected failure not established; retain UNKNOWN")
    return {
        "case": case,
        "qualified_failure": True,
        "cluster_ready": False,
        "restore_spec_sha256": expected_spec,
        "evidence": selected,
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "operator-prepared-disposable-restore; not DR-01 PASS",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubectl", required=True, type=Path)
    p.add_argument("--kubeconfig", required=True, type=Path)
    p.add_argument("--namespace", required=True)
    p.add_argument("--case", required=True, choices=sorted(CASES))
    p.add_argument("--cluster", required=True)
    p.add_argument("--cluster-uid", required=True)
    p.add_argument("--expected-spec-sha256", required=True)
    p.add_argument("--backup-id", required=True)
    p.add_argument(
        "--wal-segment", help="first required WAL from trusted backup metadata; missing-wal only"
    )
    args = p.parse_args()
    try:
        if (
            not args.kubectl.is_absolute()
            or not args.kubectl.is_file()
            or not args.kubeconfig.is_file()
        ):
            raise ValueError("trusted local executable and kubeconfig required")
        result = inspect(
            Kubectl(args.kubectl, args.kubeconfig, args.namespace),
            case=args.case,
            name=args.cluster,
            uid=args.cluster_uid,
            expected_spec=args.expected_spec_sha256,
            backup_id=args.backup_id,
            wal_segment=args.wal_segment,
        )
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "qualified_failure": False,
                    "status": "UNKNOWN",
                    "reason": "bound restore failure could not be established",
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
