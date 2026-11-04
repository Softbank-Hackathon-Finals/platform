"""Read an exact, registered CNPG restore; never create or repair resources.

The effect owner records admitted Cluster/ObjectStore fingerprints at creation.
The connection factory is trusted: it pins the restored cluster CA and routes to
the named primary. A caller-supplied URL or uploaded receipt is not registration.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .backup_restore import CompletedBackup, MarkerBoundary, RestoredDatabase, _hash, _json, _time
from .cnpg_backup import PLUGIN, _names
from .cnpg_readback import RESOURCES, SHA, _timestamp, _uid, resource_fingerprint
from .postgres_restore import read_demo_chat_marker
from .runner import RunContext


@dataclass(frozen=True)
class RestoreRequestBinding:
    """Immutable server execution receipt, not a requester's authorization."""

    context: RunContext
    backup: CompletedBackup
    namespace: str
    source_name: str
    target_name: str
    target_uid: str
    target_sha256: str
    reader_store_name: str
    reader_store_uid: str
    reader_store_sha256: str
    marker_uuid: str
    requested_at: datetime

    def __post_init__(self):
        _names(self.namespace, self.source_name, self.target_name, self.reader_store_name)
        _uid(self.target_uid)
        _uid(self.reader_store_uid)
        if (
            not isinstance(self.context, RunContext)
            or not isinstance(self.backup, CompletedBackup)
            or self.backup.context != self.context
            or self.source_name == self.target_name
            or self.target_uid == self.backup.source_uid
            or any(
                not isinstance(v, str) or not SHA.fullmatch(v)
                for v in (self.target_sha256, self.reader_store_sha256)
            )
            or not isinstance(self.marker_uuid, str)
            or str(UUID(self.marker_uuid)) != self.marker_uuid
            or _time(self.requested_at) < self.backup.completed_at
        ):
            raise ValueError("registered new restore and completed backup required")


class CNPGRestoreReader:
    """Kubernetes before/after readback around a certificate-verified SQL query.

    GET and connection factory must enforce transport deadlines, registration,
    network ownership and certificate trust. This reader requires only GET of two
    named resources and SELECT on public.messages. It never reads a Secret itself.
    """

    def __init__(
        self,
        *,
        binding,
        get_resource,
        connection_factory,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(binding, RestoreRequestBinding) or not all(
            callable(f) for f in (get_resource, connection_factory, evidence_sink, clock)
        ):
            raise ValueError("registered restore and bounded read clients required")
        self.binding, self.get, self.connect = binding, get_resource, connection_factory
        self.sink, self.clock = evidence_sink, clock

    def _read(self, kind, name, uid, expected):
        b = self.binding
        raw = _json(self.get(RESOURCES[kind][1], b.namespace, name))
        if len(raw) > 262144:
            raise ValueError("oversized Kubernetes response")
        doc = json.loads(raw)
        digest = resource_fingerprint(doc, kind=kind, namespace=b.namespace, name=name, uid=uid)
        if digest != expected:
            raise ValueError("restore configuration changed since registration")
        return doc

    def _state(self, now):
        b = self.binding
        target = self._read("Cluster", b.target_name, b.target_uid, b.target_sha256)
        self._read("ObjectStore", b.reader_store_name, b.reader_store_uid, b.reader_store_sha256)
        spec, status = target["spec"], target.get("status", {})
        recovery = spec.get("bootstrap", {}).get("recovery", {})
        external = spec.get("externalClusters", [])
        if (
            target["metadata"]["generation"] != 1
            or type(spec.get("instances")) is not int
            or spec["instances"] != 1
            or spec.get("enableSuperuserAccess") is not False
            or spec.get("plugins")
            or recovery.get("source") != "source"
            or recovery.get("recoveryTarget")
            != {"backupID": b.backup.backup_id, "targetImmediate": True}
            or len(external) != 1
            or external[0].get("name") != "source"
        ):
            raise ValueError("unqualified restore topology or backup selection")
        plugin = external[0].get("plugin", {})
        if (
            plugin.get("name") != PLUGIN
            or plugin.get("enabled", True) is not True
            or plugin.get("isWALArchiver", False) is not False
            or plugin.get("parameters")
            != {"barmanObjectName": b.reader_store_name, "serverName": b.source_name}
        ):
            raise ValueError("restore archive source mismatch")
        conditions = status.get("conditions", [])
        ready = [c for c in conditions if c.get("type") == "Ready"]
        initialized = [c for c in conditions if c.get("type") == "Initialized"]
        primary = status.get("currentPrimary")
        if (
            len(ready) != 1
            or ready[0].get("status") != "True"
            or len(initialized) != 1
            or initialized[0].get("status") != "True"
            or status.get("phase") != "Cluster in healthy state"
            or type(status.get("readyInstances")) is not int
            or status["readyInstances"] != 1
            or type(status.get("instances")) is not int
            or status["instances"] != 1
            or not isinstance(primary, str)
            or status.get("targetPrimary") != primary
            or status.get("instanceNames") != [primary]
        ):
            raise ValueError("restore is not ready or primary is ambiguous")
        _names(primary)
        created = _timestamp(target["metadata"].get("creationTimestamp"))
        ready_at = _timestamp(ready[0].get("lastTransitionTime"))
        if not b.requested_at.replace(microsecond=0) <= created <= ready_at <= now:
            raise ValueError("restore timestamps inconsistent with request")
        return created, ready_at, primary

    def __call__(self, context, backup, boundary):
        b = self.binding
        started = _time(self.clock())
        if (
            context != b.context
            or backup != b.backup
            or not isinstance(boundary, MarkerBoundary)
            or boundary.context != context
            or boundary.source_uid != backup.source_uid
            or not timedelta(0) <= started - b.requested_at <= timedelta(hours=24)
            or not boundary.recorded_at <= backup.requested_at <= backup.completed_at
            or started - boundary.recorded_at > timedelta(hours=24)
        ):
            raise ValueError("restore context, backup or source boundary mismatch")
        before = self._state(started)
        host = f"{b.target_name}-rw.{b.namespace}.svc"
        # Dedicated context-managed connection; no application connection pool.
        with self.connect(context, b.target_uid, before[2], host, boundary.database) as connection:
            if connection.info.host != host or connection.info.dbname != boundary.database:
                raise ValueError("SQL connection is not the registered restore endpoint")
            data = read_demo_chat_marker(
                connection,
                marker_id=boundary.marker_id,
                idempotency_key=b.marker_uuid,
                clock=self.clock,
            )
        finished = _time(self.clock())
        after = self._state(finished)
        ended = _time(self.clock())
        if (
            before != after
            or not started <= data.observed_at <= finished <= ended
            or ended - started > timedelta(seconds=30)
        ):
            raise ValueError(
                "restore identity/readiness changed or query exceeded freshness window"
            )
        result = RestoredDatabase(
            context,
            backup.source_uid,
            b.target_uid,
            backup.backup_uid,
            backup.backup_id,
            backup.archive_binding_sha256,
            boundary.database,
            before[0],
            before[1],
            data.observed_at,
            data.marker_id,
            data.marker_sha256,
            data.row_count,
        )
        self.sink(
            _json(
                {
                    "format_version": "1",
                    "run_id": context.run_id,
                    "target_uid_sha256": _hash(b.target_uid.encode()),
                    "target_configuration_sha256": b.target_sha256,
                    "reader_store_configuration_sha256": b.reader_store_sha256,
                    "backup_uid_sha256": _hash(backup.backup_uid.encode()),
                    "archive_binding_sha256": backup.archive_binding_sha256,
                    "observed_at": data.observed_at.isoformat(),
                    "qualification": "registered-restore-readback-not-whole-database-integrity",
                }
            )
        )
        return result
