"""ADR-0034 ``both.control_probes``: ``(kind, test_id) → probe factory``.

The server still owns every collector's wiring (clients, registered targets, receipts,
evidence sinks). The registry only decides *which* collector class may serve a test for
an environment kind, and refuses adapters of another kind (an S3 search for a gcp
environment, a SeaweedFS target for aws, ...).

* ENC-01, NET-01 and DR-01 are kind-neutral: one factory serves every kind.
* A ``(kind, test_id)`` without a factory yields ``UnsupportedProbe``: an Observation
  with verdict UNKNOWN (``unknown_reason`` NOT_RUN, evidence tool ``UNSUPPORTED``), which
  the gate blocks like any UNKNOWN. ``UnknownReason`` in the T5 schema is closed, so the
  ``UNSUPPORTED`` marker travels in the evidence and its retained payload.
* A supported test with no server wiring is simply omitted, so Runner reports NOT_RUN
  with no evidence: "not connected yet" is distinct from "no adapter for this kind".

Factories import collector modules lazily, so a gcp-only host does not need botocore.
"""

import hashlib
import importlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .runner import KIND_NEUTRAL_CONTROLS, KINDS, Environment, Observation, Probe, RunContext

SLOT = "both.control_probes"
UNSUPPORTED = "UNSUPPORTED"
CONTRACT_VERSION = "bravo.environment-kinds/v1"
PLAN_REJECTION_PREFIX = "adapter_missing:"

Factory = Callable[..., Probe]


def _control(test_id: str) -> str:
    return test_id.rsplit(".", 1)[0]


class UnsupportedProbe:
    """Explicit "no adapter for this kind" collector. Always UNKNOWN, never PASS."""

    reason = UNSUPPORTED

    def __init__(
        self,
        kind: str,
        test_id: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        evidence_sink: Callable[[bytes], None] | None = None,
    ):
        if kind not in KINDS or not isinstance(test_id, str):
            raise ValueError("registered kind and test required")
        self.kind, self.test_id, self.clock, self.sink = kind, test_id, clock, evidence_sink

    def __call__(self, context: RunContext, spec) -> Observation:
        if (
            not isinstance(context, RunContext)
            or spec.test_id != self.test_id
            or context.environment.kind != self.kind
        ):
            raise ValueError("unsupported-probe scope mismatch")
        collected = self.clock().astimezone(UTC)
        payload = json.dumps(
            {
                "reason": UNSUPPORTED,
                "slot": SLOT,
                "contract": CONTRACT_VERSION,
                "kind": self.kind,
                "test_id": spec.test_id,
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "collected_at": collected.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if self.sink is not None:
            self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            spec.method_version,
            None,
            "fail" if spec.positive_control == "required" else "not_applicable",
            collected,
            UNSUPPORTED,
            CONTRACT_VERSION,
            "sha256:" + hashlib.sha256(payload).hexdigest(),
            "NOT_RUN",
        )


class ProbeRegistry:
    def __init__(self):
        self._neutral: dict[str, Factory] = {}
        self._kinds: dict[tuple[str, str], Factory] = {}

    def register(self, kind: str, test_id: str, factory: Factory) -> None:
        if kind not in KINDS or not callable(factory):
            raise ValueError("registered kind and callable factory required")
        if _control(test_id) in KIND_NEUTRAL_CONTROLS:
            raise ValueError("kind-neutral control must use register_neutral")
        if (kind, test_id) in self._kinds:
            raise ValueError("duplicate probe registration")
        self._kinds[(kind, test_id)] = factory

    def register_neutral(self, test_id: str, factory: Factory) -> None:
        if _control(test_id) not in KIND_NEUTRAL_CONTROLS or not callable(factory):
            raise ValueError("only ENC-01, NET-01 and DR-01 are kind-neutral")
        if test_id in self._neutral:
            raise ValueError("duplicate probe registration")
        self._neutral[test_id] = factory

    def factory(self, kind: str, test_id: str) -> Factory | None:
        if kind not in KINDS:
            raise ValueError("invalid environment kind")
        if _control(test_id) in KIND_NEUTRAL_CONTROLS:
            return self._neutral.get(test_id)
        return self._kinds.get((kind, test_id))

    def supports(self, kind: str, test_id: str) -> bool:
        return self.factory(kind, test_id) is not None

    def matrix(self, test_ids: Iterable[str]) -> dict[str, dict[str, str]]:
        """kind → test_id → collector name, or ``UNSUPPORTED``."""
        ids = tuple(test_ids)
        return {
            kind: {
                test_id: getattr(self.factory(kind, test_id), "probe_name", "probe")
                if self.supports(kind, test_id)
                else UNSUPPORTED
                for test_id in ids
            }
            for kind in KINDS
        }

    def missing(self, kind: str, test_ids: Iterable[str]) -> tuple[str, ...]:
        """Plan-readiness reasons, e.g. ``adapter_missing:both.control_probes:IAM-01.2``."""
        return tuple(
            f"{PLAN_REJECTION_PREFIX}{SLOT}:{test_id}"
            for test_id in test_ids
            if not self.supports(kind, test_id)
        )

    def probes(
        self,
        environment: Environment,
        test_ids: Iterable[str],
        wiring: Mapping[str, Mapping[str, Any]],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        evidence_sink: Callable[[bytes], None] | None = None,
    ) -> dict[str, Probe]:
        """Build ``Runner`` probes for one registered environment.

        ``wiring`` maps test_id → keyword arguments of that kind's collector. Pass the
        Runner's clock so UNSUPPORTED observations fall inside its invocation window.
        """
        if not isinstance(environment, Environment) or environment.kind is None:
            raise ValueError("registered environment kind required")
        ids = tuple(test_ids)
        if not set(wiring) <= set(ids):
            raise ValueError("wiring for a test outside the requested set")
        probes: dict[str, Probe] = {}
        for test_id in ids:
            factory = self.factory(environment.kind, test_id)
            if factory is None:
                probes[test_id] = UnsupportedProbe(
                    environment.kind, test_id, clock=clock, evidence_sink=evidence_sink
                )
            elif test_id in wiring:
                probes[test_id] = factory(**dict(wiring[test_id]))
        return probes


def _lazy(module: str, name: str, check: Callable[[dict], bool] | None = None) -> Factory:
    def factory(**wiring):
        if check is not None and not check(wiring):
            raise ValueError("collector adapter does not match environment kind")
        cls = getattr(importlib.import_module(f"{__package__}.{module}"), name)
        return cls(**wiring)

    adapter = getattr(check, "adapter_name", None)
    factory.probe_name = f"{name}+{adapter}" if adapter else name
    return factory


def _instance(key: str, module: str, name: str, exact: bool = False) -> Callable[[dict], bool]:
    def check(wiring):
        cls = getattr(importlib.import_module(f"{__package__}.{module}"), name)
        value = wiring.get(key)
        return type(value) is cls if exact else isinstance(value, cls)

    check.adapter_name = name
    return check


def _provider(key: str, provider: str, attribute: str | None = None) -> Callable[[dict], bool]:
    def check(wiring):
        value = wiring.get(key)
        target = getattr(value, attribute, None) if attribute else value
        return getattr(target, "provider", None) == provider

    check.adapter_name = provider
    return check


def _any(*checks):
    def check(wiring):
        return any(item(wiring) for item in checks)

    check.adapter_name = "/".join(item.adapter_name for item in checks)
    return check


def _all(*checks):
    def check(wiring):
        return all(item(wiring) for item in checks)

    check.adapter_name = "+".join(item.adapter_name for item in checks)
    return check


def default_registry() -> ProbeRegistry:
    """Collectors implemented in this package (see README "환경 종류별 통제 시험")."""
    registry = ProbeRegistry()
    # Kind-neutral: the common base (Kubernetes + CNPG + PgBouncer) is the same everywhere.
    for test_id in ("ENC-01.1", "ENC-01.2", "ENC-01.3"):
        registry.register_neutral(test_id, _lazy("postgres_tls", "PoolerTLSProbe"))
    registry.register_neutral("ENC-01.4", _lazy("replication_tls", "ReplicationTLSProbe"))
    registry.register_neutral("ENC-01.5", _lazy("pooler_backend_tls", "PoolerBackendTLSProbe"))
    registry.register_neutral("NET-01.1", _lazy("public_ports", "PublicPortsProbe"))
    for test_id in ("NET-01.2", "NET-01.3"):
        registry.register_neutral(test_id, _lazy("network_probes", "NetworkAccessProbe"))
    registry.register_neutral("DR-01.1", _lazy("backup_restore", "RestoreProbe"))

    for kind in KINDS:
        # Repository/rendered-config scan and rotation qualifier do not touch the provider.
        registry.register(kind, "SEC-01.2", _lazy("secret_scan", "GitleaksProbe"))
        registry.register(kind, "SEC-01.4", _lazy("secret_rotation", "SecretRotationProbe"))

    # aws
    registry.register("aws", "SEC-01.1", _lazy("aws_probes", "SecretAccessProbe"))
    registry.register(
        "aws",
        "SEC-01.3",
        _lazy(
            "secret_logs",
            "SecretLogProbe",
            _instance("search", "secret_logs", "CloudWatchSecretLogSearch"),
        ),
    )
    registry.register("aws", "ENC-02.1", _lazy("secret_encryption", "SecretEncryptionProbe"))
    registry.register(
        "aws",
        "IAM-01.1",
        _lazy("aws_probes", "S3AnonymousReadProbe", _provider("target", "s3")),
    )
    registry.register("aws", "IAM-01.2", _lazy("iam_probes", "ReportProbe"))
    registry.register(
        "aws",
        "ISO-01.1",
        _lazy(
            "app_isolation",
            "AppIsolationProbe",
            _instance("object_reader", "app_isolation", "S3IsolationReader"),
        ),
    )

    # onprem
    registry.register("onprem", "SEC-01.1", _lazy("openbao", "OpenBaoSecretAccessProbe"))
    registry.register(
        "onprem",
        "SEC-01.3",
        _lazy(
            "secret_logs",
            "SecretLogProbe",
            _instance("search", "audit_store", "ObjectStoreSecretLogSearch"),
        ),
    )
    registry.register(
        "onprem", "ENC-02.1", _lazy("target_secret_encryption", "TargetSecretEncryptionProbe")
    )
    registry.register(
        "onprem",
        "IAM-01.1",
        _lazy("aws_probes", "S3AnonymousReadProbe", _provider("target", "s3-compatible")),
    )
    registry.register("onprem", "IAM-01.2", _lazy("iam_probes", "KubeBenchIamProbe"))
    registry.register(
        "onprem",
        "ISO-01.1",
        _lazy(
            "app_isolation",
            "AppIsolationProbe",
            _all(
                _instance("object_reader", "app_isolation", "ClientObjectIsolationReader"),
                _provider("object_reader", "s3-compatible", "owner"),
            ),
        ),
    )

    # gcp
    registry.register("gcp", "SEC-01.1", _lazy("gcp_probes", "GcpSecretAccessProbe"))
    registry.register(
        "gcp",
        "SEC-01.3",
        _lazy(
            "secret_logs",
            "SecretLogProbe",
            _instance("search", "gcp_probes", "CloudLoggingSecretLogSearch"),
        ),
    )
    registry.register("gcp", "ENC-02.1", _lazy("gcp_probes", "GcpSecretEncryptionProbe"))
    registry.register(
        "gcp",
        "IAM-01.1",
        _lazy("gcp_probes", "GcsAnonymousReadProbe", _provider("target", "gcs")),
    )
    registry.register(
        "gcp",
        "ISO-01.1",
        _lazy(
            "app_isolation",
            "AppIsolationProbe",
            _all(
                _instance("object_reader", "app_isolation", "ClientObjectIsolationReader"),
                _provider("object_reader", "gcs", "owner"),
            ),
        ),
    )
    # gcp IAM-01.2 (IAM baseline / GKE benchmark mapping) is deliberately UNSUPPORTED.

    audit = {
        "aws": ("audit_probes", "CloudWatchAuditSearch", False),
        "onprem": ("audit_store", "ObjectStoreAuditSearch", True),
        "gcp": ("gcp_probes", "CloudLoggingAuditSearch", False),
    }
    for kind, (module, name, exact) in audit.items():
        for test_id in ("AUD-01.1", "AUD-01.2", "AUD-01.3"):
            native = _instance("search", module, name, exact)
            if test_id == "AUD-01.1":
                # T34 / ADR-0037: the DB login canary may also be found in the CNPG primary's own
                # pod log through the Kubernetes API. CNPG is the common base, so every kind may use it
                # (operations/controls/run.py `pod-log` on both sides of aws->onprem).
                native = _any(native, _instance("search", "pod_log_audit", "PodLogAuditSearch", True))
            registry.register(kind, test_id, _lazy("audit_probes", "AuditSearchProbe", native))
    return registry
