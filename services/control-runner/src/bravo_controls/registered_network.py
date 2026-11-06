"""Opt-in network method 2: registered DB and pooler listeners, not fixed ports.

The existing T5 method/catalog remains unchanged. A trusted host must select the
candidate catalog and authenticate the registry before this collector is used.
This module does not register a plan, alter a policy or authorize a cutover.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .aws_probes import _canonical, _digest
from .network_probes import NetworkTarget, _NetworkPortProbe
from .runner import Observation, RunContext, TestSpec, _utc


@dataclass(frozen=True)
class RegisteredListener:
    role: str
    target: NetworkTarget
    # Host-pinned service/listener/backend identity and approved registration.
    registration_sha256: str

    def __post_init__(self):
        if (
            self.role not in {"database", "pooler"}
            or not isinstance(self.target, NetworkTarget)
            or not isinstance(self.registration_sha256, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.registration_sha256)
        ):
            raise ValueError("registered DB or pooler listener required")

    def binding(self):
        return {
            "role": self.role,
            "target_binding_sha256": self.target.binding_sha256,
            "registration_sha256": self.registration_sha256,
        }


@dataclass(frozen=True)
class ListenerSnapshot:
    context: RunContext
    binding_sha256: str
    collected_at: datetime


class RegisteredNetworkAccessProbe:
    """Both roles are mandatory even when their listening port is the same.

    Callbacks run under the host's bounded, authenticated execution channel. Registry
    and policy readers must obtain live data, not echo request JSON. Frozen Python
    objects and digests bind observations; they are not signatures or authorization.
    """

    def __init__(
        self,
        *,
        listeners,
        read_registration,
        run_tcp,
        read_policy,
        evidence_sink,
        run_dns=None,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(listeners, tuple)
            or len(listeners) != 2
            or not all(isinstance(item, RegisteredListener) for item in listeners)
            or {item.role for item in listeners} != {"database", "pooler"}
            or not all(
                callable(f) for f in (read_registration, run_tcp, read_policy, evidence_sink, clock)
            )
        ):
            raise ValueError("two registered roles and trusted readers required")
        self.listeners = tuple(sorted(listeners, key=lambda item: item.role))
        first, second = (item.target for item in self.listeners)
        if first.target_uid == second.target_uid or first.endpoint == second.endpoint:
            raise ValueError("distinct DB and pooler services required")

        def scope(t):
            return (
                t.app,
                t.environment,
                t.allowed_agent,
                t.denied_agent,
                t.health_endpoint,
                t.dns_name,
            )

        if scope(first) != scope(second):
            raise ValueError("same app, environment and probe agents required")
        self.test_id = "NET-01.3" if first.dns_name else "NET-01.2"
        self.spec = TestSpec(self.test_id, "NET-01", "2", "deny", "required")
        self.binding_sha256 = _digest(_canonical([item.binding() for item in self.listeners]))
        self.read_registration, self.sink, self.clock = read_registration, evidence_sink, clock
        self.ports = tuple(
            _NetworkPortProbe(
                target=item.target,
                run_tcp=run_tcp,
                run_dns=run_dns,
                read_policy=lambda context, target=item.target: read_policy(context, target),
                evidence_sink=evidence_sink,
                clock=clock,
                method_version="2",
            )
            for item in self.listeners
        )

    def __call__(self, context, spec):
        first = self.listeners[0].target
        if (
            not isinstance(context, RunContext)
            or spec != self.spec
            or context.app != first.app
            or context.environment != first.environment
        ):
            raise ValueError("registered network method or scope mismatch")
        started = _utc(self.clock())
        deadline = started + timedelta(seconds=60)

        def registration():
            before = _utc(self.clock())
            if not started <= before <= deadline:
                raise TimeoutError("registration budget expired")
            result = self.read_registration(context)
            now = _utc(self.clock())
            if (
                not isinstance(result, ListenerSnapshot)
                or result.context != context
                or result.binding_sha256 != self.binding_sha256
                or not before <= _utc(result.collected_at) <= now <= deadline
            ):
                raise ValueError("fresh authenticated listener registration required")

        registration()
        results = []
        for port in self.ports:
            # Do not spend a second full budget after the first endpoint.
            if not started <= _utc(self.clock()) <= deadline:
                raise TimeoutError("combined listener budget expired")
            results.append(port(context, spec, deadline=deadline))
            registration()
        finished = _utc(self.clock())
        if not started <= finished <= deadline:
            raise TimeoutError("combined listener budget expired")
        positive = all(row.positive_control == "pass" for row in results)
        observed, reason = None, None
        if not positive:
            reason = "POSITIVE_CONTROL_FAILED"
        elif any(row.observed == "allow" for row in results):
            # One proven connection is a failure even if the other role was inconclusive.
            observed = "allow"
        elif any(row.observed is None for row in results):
            reason = next(row.unknown_reason for row in results if row.observed is None)
        else:
            observed = "deny"
        payload = _canonical(
            {
                "format_version": "2",
                "method_version": "2",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "registration_binding_sha256": self.binding_sha256,
                "required_roles": ["database", "pooler"],
                "results": [
                    {
                        **listener.binding(),
                        "port": listener.target.endpoint.port,
                        "output_sha256": row.output_sha256,
                        "observed": row.observed,
                        "positive_control": row.positive_control,
                        "unknown_reason": row.unknown_reason,
                    }
                    for listener, row in zip(self.listeners, results, strict=True)
                ],
                "observed": observed,
                "unknown_reason": reason,
                "collected_at": finished.isoformat(),
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            "2",
            observed,
            "pass" if positive else "fail",
            finished,
            "bravo-registered-network-probe",
            "2",
            _digest(payload),
            reason,
        )
