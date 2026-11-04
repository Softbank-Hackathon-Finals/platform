import json
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.network_probes import (
    Attempt,
    Endpoint,
    NetworkAccessProbe,
    NetworkTarget,
    PolicySnapshot,
    SocketTCPRunner,
    _NetworkPortProbe,
    endpoint_id,
)
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
ENV = Environment("target", "target", "k8s:test")
CTX = RunContext("run-a", "chat", ENV)
TARGET = NetworkTarget(
    "chat",
    ENV,
    "db-uid",
    Endpoint("192.0.2.10", 5432),
    "app-agent",
    "other-agent",
    Endpoint("192.0.2.20", 443),
)
SHA = "sha256:" + "a" * 64


def fixture(
    *, target=TARGET, outcomes=None, mutate=None, policies=None, sink=None, clock=lambda: NOW
):
    outputs = dict(outcomes or {})
    calls = []
    evidence = []

    def tcp(agent, endpoint, nonce):
        key = (
            "negative"
            if agent == target.denied_agent and endpoint == target.endpoint
            else "target"
            if endpoint == target.endpoint
            else "agent"
        )
        calls.append(key)
        outcome = outputs.get(key, "connected")
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        result = Attempt(nonce, agent, endpoint_id(endpoint), outcome, 3, NOW)
        return mutate(result) if mutate else result

    def dns(agent, name, nonce):
        calls.append("dns")
        return Attempt(nonce, agent, name, outputs.get("dns", "resolved"), 1, NOW)

    hashes = iter(policies or [SHA, SHA])
    p = _NetworkPortProbe(
        target=target,
        run_tcp=tcp,
        run_dns=dns,
        read_policy=lambda c: PolicySnapshot(c, target.binding_sha256, next(hashes), NOW),
        evidence_sink=sink or evidence.append,
        clock=clock,
    )
    spec = Spec("NET-01.3" if target.dns_name else "NET-01.2", "NET-01", "1", "deny", "required")
    return p, spec, calls, evidence


@pytest.mark.parametrize(
    "outcome,expected",
    [("timeout", "deny"), ("refused", "deny"), ("connected", "allow"), ("error", None)],
)
def test_matched_controls_determine_observation(outcome, expected):
    p, spec, calls, evidence = fixture(outcomes={"negative": outcome})
    result = p(CTX, spec)
    assert result.observed == expected and result.positive_control == "pass"
    assert calls == ["target", "agent", "negative", "target", "agent"]
    saved = json.loads(evidence[0])
    assert saved["policy_sha256"] == SHA
    assert b"192.0.2.10" not in evidence[0]
    assert result.unknown_reason == ("UNEXPECTED_RESPONSE" if expected is None else None)


@pytest.mark.parametrize("key", ["target", "agent"])
@pytest.mark.parametrize("failure", ["timeout", "refused", "error"])
def test_failed_control_prevents_probe_and_pass(key, failure):
    p, spec, calls, evidence = fixture(outcomes={key: failure, "negative": "timeout"})
    result = p(CTX, spec)
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    assert "negative" not in calls and evidence


@pytest.mark.parametrize("key", ["target", "agent"])
@pytest.mark.parametrize("outcome", ["timeout", "connected"])
def test_control_failure_after_negative_is_unknown(key, outcome):
    p, spec, _, _ = fixture(outcomes={key: ["connected", "timeout"], "negative": outcome})
    result = p(CTX, spec)
    assert result.observed is None and result.positive_control == "fail"


def test_namespace_probe_requires_both_dns_controls():
    target = replace(TARGET, dns_name="control.test.svc")
    p, spec, calls, _ = fixture(target=target, outcomes={"negative": "timeout"})
    assert p(CTX, spec).observed == "deny"
    assert calls.count("dns") == 2
    p, spec, calls, _ = fixture(target=target, outcomes={"dns": "error"})
    assert p(CTX, spec).unknown_reason == "POSITIVE_CONTROL_FAILED"
    assert "negative" not in calls


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "replayed"},
        {"agent": "other"},
        {"destination": "other"},
        {"outcome": "made-up"},
        {"elapsed_ms": -1},
        {"elapsed_ms": 6000},
        {"elapsed_ms": True},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"observed_at": NOW + timedelta(seconds=1)},
    ],
)
def test_unbound_observations_rejected(change):
    p, spec, _, evidence = fixture(mutate=lambda r: replace(r, **change))
    with pytest.raises(ValueError):
        p(CTX, spec)
    assert not evidence


def test_policy_change_during_probe_not_accepted():
    p, spec, _, evidence = fixture(policies=[SHA, "sha256:" + "b" * 64])
    with pytest.raises(ValueError):
        p(CTX, spec)
    assert not evidence


def test_wrong_context_or_method_never_opens_connection():
    p, spec, calls, _ = fixture()
    with pytest.raises(ValueError):
        p(replace(CTX, app="other"), spec)
    with pytest.raises(ValueError):
        p(CTX, replace(spec, test_id="NET-01.3"))
    assert not calls


def test_sink_error_prevents_observation():
    def sink(_):
        raise OSError("unavailable")

    p, spec, _, _ = fixture(sink=sink)
    with pytest.raises(OSError):
        p(CTX, spec)


@pytest.mark.parametrize(
    "ip,port",
    [
        ("0.0.0.0", 5432),
        ("224.0.0.1", 5432),
        ("::", 5432),
        ("example.com", 5432),
        ("127.0.0.1", 0),
        ("127.0.0.1", True),
    ],
)
def test_unregistered_endpoint_shape_rejected(ip, port):
    with pytest.raises(ValueError):
        Endpoint(ip, port)


def test_tcp_agent_only_uses_registered_endpoints():
    runner = SocketTCPRunner(agent="a", endpoints=frozenset({TARGET.endpoint}))
    with pytest.raises(ValueError):
        runner("other", TARGET.endpoint, "nonce")
    with pytest.raises(ValueError):
        runner("a", TARGET.health_endpoint, "nonce")


def test_actual_local_socket_connected_and_refused():
    # No data payload, no public listener. This proves transport classification,
    # not NetworkPolicy or remote environment isolation.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        ep = Endpoint("127.0.0.1", server.getsockname()[1])
        runner = SocketTCPRunner(agent="test", endpoints=frozenset({ep}))
        assert runner("test", ep, "first").outcome == "connected"
        accepted, _ = server.accept()
        accepted.close()
    assert runner("test", ep, "second").outcome == "refused"


def test_unreachable_route_not_counted_as_policy_denial(monkeypatch):
    import errno

    def broken(*args):
        raise OSError(errno.ENETUNREACH, "no route")

    monkeypatch.setattr(socket.socket, "connect", broken)
    runner = SocketTCPRunner(agent="test", endpoints=frozenset({TARGET.endpoint}))
    assert runner("test", TARGET.endpoint, "nonce").outcome == "error"


@pytest.mark.parametrize(
    "change",
    [
        {"binding_sha256": "sha256:" + "f" * 64},
        {"context": replace(CTX, app="other")},
        {"collected_at": NOW - timedelta(seconds=1)},
        {"sha256": "unverified"},
    ],
)
def test_wrong_or_stale_policy_provenance_rejected(change):
    p, spec, calls, evidence = fixture()
    p.policy = lambda c: replace(PolicySnapshot(c, TARGET.binding_sha256, SHA, NOW), **change)
    with pytest.raises(ValueError):
        p(CTX, spec)
    assert not calls and not evidence


def test_missing_policy_read_permission_never_grants_pass():
    p, spec, calls, evidence = fixture()

    def denied(c):
        raise PermissionError("read not granted")

    p.policy = denied
    with pytest.raises(PermissionError):
        p(CTX, spec)
    assert not calls and not evidence


def test_whole_probe_expiration_does_not_become_transport_denial():
    times = iter([NOW, NOW, NOW + timedelta(seconds=61)])
    p, spec, calls, evidence = fixture(clock=lambda: next(times))
    with pytest.raises(TimeoutError):
        p(CTX, spec)
    assert not calls and not evidence


@pytest.mark.parametrize(
    "raw,pooler,expected",
    [
        ("timeout", "timeout", "deny"),
        ("refused", "timeout", "deny"),
        ("connected", "timeout", "allow"),
        ("timeout", "connected", "allow"),
        ("error", "timeout", None),
        ("timeout", "error", None),
    ],
)
def test_public_probe_checks_both_ports(raw, pooler, expected):
    targets = (
        TARGET,
        replace(TARGET, endpoint=Endpoint("192.0.2.11", 6432), target_uid="pooler-uid"),
    )
    calls = []
    evidence = []

    def tcp(agent, ep, nonce):
        calls.append((agent, ep.port))
        outcome = (
            {5432: raw, 6432: pooler}.get(ep.port, "connected")
            if agent == TARGET.denied_agent
            else "connected"
        )
        return Attempt(nonce, agent, endpoint_id(ep), outcome, 3, NOW)

    p = NetworkAccessProbe(
        targets=targets,
        run_tcp=tcp,
        read_policy=lambda c, t: PolicySnapshot(c, t.binding_sha256, SHA, NOW),
        evidence_sink=evidence.append,
        clock=lambda: NOW,
    )
    result = p(CTX, Spec("NET-01.2", "NET-01", "1", "deny", "required"))
    assert result.observed == expected
    assert (TARGET.denied_agent, 5432) in calls and (TARGET.denied_agent, 6432) in calls
    assert json.loads(evidence[-1])["required_ports"] == [5432, 6432]
    assert len(evidence) == 3


@pytest.mark.parametrize(
    "targets",
    [
        (TARGET,),
        (TARGET, TARGET),
        (TARGET, replace(TARGET, endpoint=Endpoint("192.0.2.11", 6432), app="other")),
    ],
)
def test_incomplete_or_mixed_network_suite_rejected(targets):
    with pytest.raises(ValueError):
        NetworkAccessProbe(
            targets=targets,
            run_tcp=lambda *a: None,
            read_policy=lambda *a: None,
            evidence_sink=lambda p: None,
        )


def test_one_unhealthy_port_prevents_whole_suite_pass():
    targets = (TARGET, replace(TARGET, endpoint=Endpoint("192.0.2.11", 6432)))

    def tcp(agent, ep, nonce):
        outcome = (
            "timeout"
            if ep.port == 6432 or (agent == TARGET.denied_agent and ep.port == 5432)
            else "connected"
        )
        return Attempt(nonce, agent, endpoint_id(ep), outcome, 3, NOW)

    p = NetworkAccessProbe(
        targets=targets,
        run_tcp=tcp,
        read_policy=lambda c, t: PolicySnapshot(c, t.binding_sha256, SHA, NOW),
        evidence_sink=lambda b: None,
        clock=lambda: NOW,
    )
    result = p(CTX, Spec("NET-01.2", "NET-01", "1", "deny", "required"))
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"
