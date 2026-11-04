"""Authenticated CNPG status readback for T35; never a backup executor.

Only the trusted effect owner may register a request. An API document, uploaded
receipt, or an LLM assertion must not become a RequestBinding. The injected GET
client must authenticate the registered cluster, enforce deadlines/size limits,
and expose only the three named resources below. This module reads no Secrets.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .backup_restore import CompletedBackup, _hash, _json, _time
from .cnpg_backup import PLUGIN, _names
from .runner import RunContext

RESOURCES = {
    "Cluster": ("postgresql.cnpg.io/v1", "clusters.postgresql.cnpg.io"),
    "Backup": ("postgresql.cnpg.io/v1", "backups.postgresql.cnpg.io"),
    "ObjectStore": ("barmancloud.cnpg.io/v1", "objectstores.barmancloud.cnpg.io"),
}
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _uid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("canonical Kubernetes UID required")
    return value


def _timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC API timestamp required")
    return _time(datetime.fromisoformat(value.replace("Z", "+00:00")))


def resource_fingerprint(document, *, kind, namespace, name, uid):
    """Retain this digest before issuing the backup request.

    Generation catches change-and-restore of specs on the same UID. Status and
    resourceVersion change during normal reconciliation and are not pinned.
    Configuration includes Secret references, never Secret values.
    """
    if not isinstance(document, dict) or len(_json(document)) > 262144:
        raise ValueError("bounded Kubernetes object required")
    metadata = document.get("metadata", {})
    generation = metadata.get("generation")
    if (
        document.get("apiVersion") != RESOURCES[kind][0]
        or document.get("kind") != kind
        or metadata.get("namespace") != namespace
        or metadata.get("name") != name
        or metadata.get("uid") != _uid(uid)
        or metadata.get("deletionTimestamp") is not None
        or type(generation) is not int
        or generation < 1
        or not isinstance(document.get("spec"), dict)
    ):
        raise ValueError("registered resource identity/spec mismatch")
    return _hash(
        _json({"kind": kind, "uid": uid, "generation": generation, "spec": document["spec"]})
    )


@dataclass(frozen=True)
class RequestBinding:
    """Server-side effect receipt; creation does not authorize an effect.

    Source/store fingerprints MUST be retained before request submission and
    Backup UID from the authenticated create response (or reconciled readback).
    Never reconstruct these fields after completion to claim historical binding.
    """

    context: RunContext
    namespace: str
    source_name: str
    source_uid: str
    source_sha256: str
    store_name: str
    store_uid: str
    store_sha256: str
    backup_name: str
    backup_uid: str
    requested_at: datetime

    def __post_init__(self):
        _names(self.namespace, self.source_name, self.store_name, self.backup_name)
        for uid in (self.source_uid, self.store_uid, self.backup_uid):
            _uid(uid)
        if not isinstance(self.context, RunContext) or any(
            not isinstance(v, str) or not SHA.fullmatch(v)
            for v in (self.source_sha256, self.store_sha256)
        ):
            raise ValueError("registered context and pre-request fingerprints required")
        _time(self.requested_at)

    @property
    def archive_binding_sha256(self):
        return _hash(_json({"source": self.source_sha256, "store": self.store_sha256}))


class CNPGBackupReader:
    def __init__(self, *, binding, get_resource, evidence_sink, clock=lambda: datetime.now(UTC)):
        if not isinstance(binding, RequestBinding) or not all(
            callable(f) for f in (get_resource, evidence_sink, clock)
        ):
            raise ValueError("trusted binding, authenticated GET and evidence storage required")
        self.binding, self.get, self.sink, self.clock = binding, get_resource, evidence_sink, clock

    def _read(self, kind, name, uid):
        b = self.binding
        document = self.get(RESOURCES[kind][1], b.namespace, name)
        # Detach mutable client objects. Reject non-JSON and oversized responses.
        encoded = _json(document)
        if len(encoded) > 262144:
            raise ValueError("oversized Kubernetes response")
        document = json.loads(encoded)
        fingerprint = resource_fingerprint(
            document, kind=kind, namespace=b.namespace, name=name, uid=uid
        )
        return document, fingerprint

    def __call__(self, context):
        b = self.binding
        now = _time(self.clock())
        if context != b.context or not timedelta(0) <= now - b.requested_at <= timedelta(hours=24):
            raise ValueError("wrong invocation or stale request")
        status_snapshot = None
        receipt = None
        # Before/after checks reject replacement or edits while collecting.
        for _ in range(2):
            source, source_sha = self._read("Cluster", b.source_name, b.source_uid)
            store, store_sha = self._read("ObjectStore", b.store_name, b.store_uid)
            backup, _ = self._read("Backup", b.backup_name, b.backup_uid)
            if source_sha != b.source_sha256 or store_sha != b.store_sha256:
                raise ValueError("source/archive configuration changed since request")
            plugins = [p for p in source["spec"].get("plugins", []) if p.get("name") == PLUGIN]
            if (
                len(plugins) != 1
                or plugins[0].get("enabled", True) is not True
                or plugins[0].get("isWALArchiver") is not True
                or plugins[0].get("parameters") != {"barmanObjectName": b.store_name}
                or not isinstance(store["spec"].get("configuration"), dict)
            ):
                raise ValueError("registered archive plugin binding mismatch")
            if backup["metadata"]["generation"] != 1:
                raise ValueError("backup request spec changed after creation")
            if backup["spec"] != {
                "cluster": {"name": b.source_name},
                "method": "plugin",
                "pluginConfiguration": {"name": PLUGIN},
            }:
                raise ValueError("unexpected backup request spec")
            status = backup.get("status", {})
            plugin = status.get("pluginMetadata", {})
            backup_id = status.get("backupId")
            if (
                status.get("phase") != "completed"
                or status.get("method") != "plugin"
                or status.get("error") not in (None, "")
                or plugin.get("clusterUID") != b.source_uid
                or plugin.get("name") != PLUGIN
                or plugin.get("pluginName") != PLUGIN
                or not isinstance(backup_id, str)
                or not re.fullmatch(r"[0-9]{8}T[0-9]{6}", backup_id)
            ):
                raise ValueError("completed backup provenance not established")
            datetime.strptime(backup_id, "%Y%m%dT%H%M%S")
            created = _timestamp(backup["metadata"].get("creationTimestamp"))
            started = _timestamp(status.get("startedAt"))
            completed = _timestamp(status.get("stoppedAt"))
            if not b.requested_at.replace(microsecond=0) <= created <= started <= completed <= now:
                raise ValueError("backup timestamps inconsistent with request")
            current = _hash(_json({"status": status, "created": created.isoformat()}))
            if status_snapshot is not None and current != status_snapshot:
                raise ValueError("backup status changed during readback")
            status_snapshot = current
            receipt = CompletedBackup(
                context,
                b.source_uid,
                b.backup_uid,
                backup_id,
                b.archive_binding_sha256,
                b.requested_at,
                started,
                completed,
            )
        finished = _time(self.clock())
        if not now <= finished or finished - now > timedelta(seconds=30):
            raise ValueError("readback exceeded freshness window")
        self.sink(
            _json(
                {
                    "format_version": "1",
                    "run_id": context.run_id,
                    "app": context.app,
                    "environment": context.environment.ref,
                    "source_uid_sha256": _hash(b.source_uid.encode()),
                    "backup_uid_sha256": _hash(b.backup_uid.encode()),
                    "archive_binding_sha256": b.archive_binding_sha256,
                    "status_sha256": status_snapshot,
                    "collected_at": finished.isoformat(),
                    "qualification": "controller-completed-backup-not-restored-data",
                }
            )
        )
        return receipt
