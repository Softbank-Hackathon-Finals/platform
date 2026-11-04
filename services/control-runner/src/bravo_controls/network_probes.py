"""T14 NET-01.2/3 paired connectivity observations, not a policy executor.

The orchestrator registers agents, target resource identities and authenticated
policy readers. User-supplied observations are not accepted by an API here.
"""

import errno
import ipaddress
import re
import socket
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .aws_probes import _canonical, _digest
from .runner import Environment, Observation, RunContext, TestSpec, _identifier, _utc


@dataclass(frozen=True)
class Endpoint:
    ip: str
    port: int

    def __post_init__(self):
        address = ipaddress.ip_address(self.ip)
        if (
            address.is_unspecified
            or address.is_multicast
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("registered unicast endpoint required")
        if str(address) != self.ip or "%" in self.ip:
            raise ValueError("canonical IP address required")


@dataclass(frozen=True)
class NetworkTarget:
    app: str
    environment: Environment
    target_uid: str
    endpoint: Endpoint
    allowed_agent: str
    denied_agent: str
    health_endpoint: Endpoint
    # NET-01.3 additionally requires a DNS check from the same denied agent.
    dns_name: str | None = None

    def __post_init__(self):
        for value in (self.app, self.target_uid, self.allowed_agent, self.denied_agent):
            _identifier(value)
        if (
            not isinstance(self.environment, Environment)
            or not isinstance(self.endpoint, Endpoint)
            or not isinstance(self.health_endpoint, Endpoint)
            or self.endpoint == self.health_endpoint
            or self.allowed_agent == self.denied_agent
        ):
            raise ValueError("distinct registered agents and control endpoint required")
        if self.dns_name is not None:
            if not isinstance(self.dns_name, str) or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", self.dns_name
            ):
                raise ValueError("registered DNS control name required")

    @property
    def binding_sha256(self):
        return _digest(
            _canonical(
                {
                    "app": self.app,
                    "environment": asdict(self.environment),
                    "uid": self.target_uid,
                    "endpoint": asdict(self.endpoint),
                    "allowed_agent": self.allowed_agent,
                    "denied_agent": self.denied_agent,
                    "health": asdict(self.health_endpoint),
                    "dns": self.dns_name,
                }
            )
        )


@dataclass(frozen=True)
class Attempt:
    nonce: str
    agent: str
    destination: str
    outcome: str
    elapsed_ms: int
    observed_at: datetime


@dataclass(frozen=True)
class PolicySnapshot:
    context: RunContext
    binding_sha256: str
    sha256: str
    collected_at: datetime


def endpoint_id(endpoint):
    return f"[{endpoint.ip}]:{endpoint.port}"


class SocketTCPRunner:
    """Run inside an authenticated registered agent, with a fixed allowlist.

    The agent string is a binding label, not identity proof. The remote dispatcher
    must pin Pod/VM UID and authenticate the response. No credentials or payload
    are sent. A TCP connect alone does not verify SQL authentication or TLS.
    """

    def __init__(self, *, agent, endpoints):
        _identifier(agent)
        if (
            not isinstance(endpoints, frozenset)
            or not 1 <= len(endpoints) <= 4
            or not all(isinstance(e, Endpoint) for e in endpoints)
        ):
            raise ValueError("small registered endpoint allowlist required")
        self.agent, self.endpoints = agent, endpoints

    def __call__(self, agent, endpoint, nonce):
        if agent != self.agent or endpoint not in self.endpoints:
            raise ValueError("unregistered connection attempt")
        _identifier(nonce)
        started = time.monotonic()
        family = (
            socket.AF_INET6 if ipaddress.ip_address(endpoint.ip).version == 6 else socket.AF_INET
        )
        outcome = "error"
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(3)
                connection.connect((endpoint.ip, endpoint.port))
                outcome = "connected"
        except TimeoutError:
            outcome = "timeout"
        except OSError as error:
            if error.errno == errno.ECONNREFUSED:
                outcome = "refused"
            elif error.errno == errno.ETIMEDOUT:
                outcome = "timeout"
        elapsed = round((time.monotonic() - started) * 1000)
        return Attempt(nonce, agent, endpoint_id(endpoint), outcome, elapsed, datetime.now(UTC))


class _NetworkPortProbe:
    def __init__(
        self,
        *,
        target,
        run_tcp,
        read_policy,
        evidence_sink,
        run_dns=None,
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(target, NetworkTarget) or not all(
            callable(f) for f in (run_tcp, read_policy, evidence_sink, clock)
        ):
            raise ValueError("registered target and trusted collectors required")
        if target.dns_name is not None and not callable(run_dns):
            raise ValueError("same-agent DNS control required")
        self.target, self.tcp, self.policy, self.sink, self.dns, self.clock = (
            target,
            run_tcp,
            read_policy,
            evidence_sink,
            run_dns,
            clock,
        )

    def __call__(self, context, spec):
        t = self.target
        expected_id = "NET-01.3" if t.dns_name is not None else "NET-01.2"
        if (
            context.app != t.app
            or context.environment != t.environment
            or spec != TestSpec(expected_id, "NET-01", "1", "deny", "required")
        ):
            raise ValueError("network invocation/method mismatch")
        started = _utc(self.clock())
        nonce = str(uuid4())
        rows = {}

        def policy():
            result = self.policy(context)
            now = _utc(self.clock())
            if (
                not isinstance(result, PolicySnapshot)
                or result.context != context
                or result.binding_sha256 != t.binding_sha256
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", result.sha256)
                or not started <= _utc(result.collected_at) <= now
            ):
                raise ValueError("fresh authenticated policy snapshot required")
            return result.sha256

        before_policy = policy()

        def attempt(label, agent, destination, callback, argument, outcomes):
            before = _utc(self.clock())
            if not started <= before <= started + timedelta(seconds=60):
                raise TimeoutError("network test window expired")
            result = callback(agent, argument, nonce)
            now = _utc(self.clock())
            if (
                not isinstance(result, Attempt)
                or result.nonce != nonce
                or result.agent != agent
                or result.destination != destination
                or result.outcome not in outcomes
                or type(result.elapsed_ms) is not int
                or not 0 <= result.elapsed_ms <= 5000
                or not before <= _utc(result.observed_at) <= now
                or now > started + timedelta(seconds=60)
                or now - before > timedelta(seconds=5)
            ):
                raise ValueError("unbound, stale or incomplete network observation")
            rows[label] = {
                "outcome": result.outcome,
                "elapsed_ms": result.elapsed_ms,
                "observed_at": result.observed_at.isoformat(),
            }
            return result.outcome

        def controls(prefix):
            outcomes = []
            outcomes.append(
                attempt(
                    prefix + "_target",
                    t.allowed_agent,
                    endpoint_id(t.endpoint),
                    self.tcp,
                    t.endpoint,
                    {"connected", "refused", "timeout", "error"},
                )
                == "connected"
            )
            outcomes.append(
                attempt(
                    prefix + "_agent",
                    t.denied_agent,
                    endpoint_id(t.health_endpoint),
                    self.tcp,
                    t.health_endpoint,
                    {"connected", "refused", "timeout", "error"},
                )
                == "connected"
            )
            if t.dns_name:
                outcomes.append(
                    attempt(
                        prefix + "_dns",
                        t.denied_agent,
                        t.dns_name,
                        self.dns,
                        t.dns_name,
                        {"resolved", "error"},
                    )
                    == "resolved"
                )
            return all(outcomes)

        positive = controls("before")
        negative = None
        if positive:
            negative = attempt(
                "probe",
                t.denied_agent,
                endpoint_id(t.endpoint),
                self.tcp,
                t.endpoint,
                {"connected", "refused", "timeout", "error"},
            )
            positive = controls("after")
        after_policy = policy()
        finished = _utc(self.clock())
        if (
            not started <= finished <= started + timedelta(seconds=60)
            or before_policy != after_policy
        ):
            raise ValueError("policy changed or test window invalid")
        reason = None
        observed = None
        if not positive:
            reason = "POSITIVE_CONTROL_FAILED"
        elif negative == "connected":
            observed = "allow"
        elif negative in {"refused", "timeout"}:
            observed = "deny"
        else:
            reason = "UNEXPECTED_RESPONSE"
        payload = _canonical(
            {
                "format_version": "1",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "target_binding_sha256": t.binding_sha256,
                "policy_sha256": before_policy,
                "observations": rows,
                "positive_control": "pass" if positive else "fail",
                "observed": observed,
                "unknown_reason": reason,
                "collected_at": finished.isoformat(),
                "qualification": "bounded-connectivity-with-controls-not-policy-causality",
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            "1",
            observed,
            "pass" if positive else "fail",
            finished,
            "bravo-network-probe",
            "1",
            _digest(payload),
            reason,
        )


class NetworkAccessProbe:
    """T5 network test covers both database and pooler ports, never a subset."""

    def __init__(
        self,
        *,
        targets,
        run_tcp,
        read_policy,
        evidence_sink,
        run_dns=None,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(targets, tuple)
            or len(targets) != 2
            or not all(isinstance(t, NetworkTarget) for t in targets)
            or {t.endpoint.port for t in targets} != {5432, 6432}
        ):
            raise ValueError("registered database 5432 and pooler 6432 targets required")
        if not callable(read_policy):
            raise ValueError("authenticated policy reader required")
        first = targets[0]
        if any(
            (t.app, t.environment, t.allowed_agent, t.denied_agent, t.health_endpoint, t.dns_name)
            != (
                first.app,
                first.environment,
                first.allowed_agent,
                first.denied_agent,
                first.health_endpoint,
                first.dns_name,
            )
            for t in targets
        ):
            raise ValueError("same app/environment and probe agents required")
        self.ports = tuple(
            _NetworkPortProbe(
                target=t,
                run_tcp=run_tcp,
                read_policy=lambda c, t=t: read_policy(c, t),
                evidence_sink=evidence_sink,
                run_dns=run_dns,
                clock=clock,
            )
            for t in sorted(targets, key=lambda t: t.endpoint.port)
        )
        self.sink, self.clock = evidence_sink, clock

    def __call__(self, context, spec):
        started = _utc(self.clock())
        results = [port(context, spec) for port in self.ports]
        finished = _utc(self.clock())
        if not started <= finished <= started + timedelta(seconds=60):
            raise TimeoutError("combined DB/pooler network test window expired")
        positive = all(r.positive_control == "pass" for r in results)
        observed = None
        reason = None
        if not positive:
            reason = "POSITIVE_CONTROL_FAILED"
        elif any(r.observed is None for r in results):
            reason = next(r.unknown_reason for r in results if r.observed is None)
        else:
            observed = "allow" if any(r.observed == "allow" for r in results) else "deny"
        payload = _canonical(
            {
                "format_version": "1",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "required_ports": [5432, 6432],
                "results": [
                    {
                        "port": p.target.endpoint.port,
                        "target_binding_sha256": p.target.binding_sha256,
                        "output_sha256": r.output_sha256,
                        "observed": r.observed,
                        "positive_control": r.positive_control,
                        "unknown_reason": r.unknown_reason,
                    }
                    for p, r in zip(self.ports, results, strict=True)
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
            "1",
            observed,
            "pass" if positive else "fail",
            finished,
            "bravo-network-probe",
            "1",
            _digest(payload),
            reason,
        )
