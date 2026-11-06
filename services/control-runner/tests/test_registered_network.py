import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bravo_controls.network_probes import (
    Attempt,
    Endpoint,
    NetworkTarget,
    PolicySnapshot,
    endpoint_id,
)
from bravo_controls.registered_network import (
    ListenerSnapshot,
    RegisteredListener,
    RegisteredNetworkAccessProbe,
)
from bravo_controls.runner import Catalog, Environment, RunContext, Runner
from bravo_controls.runner import TestSpec as Spec

NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CTX = RunContext("run-a", "demo-chat", Environment("aws", "source", "registered:aws"))
TARGET = NetworkTarget(
    "demo-chat",
    CTX.environment,
    "db-service",
    Endpoint("192.0.2.1", 5432),
    "allowed-pod",
    "denied-pod",
    Endpoint("192.0.2.3", 8080),
    "health.example",
)
SHA = "sha256:" + "a" * 64
LISTENERS = (
    RegisteredListener("database", TARGET, SHA),
    RegisteredListener(
        "pooler",
        replace(TARGET, target_uid="pooler-service", endpoint=Endpoint("192.0.2.2", 5432)),
        SHA,
    ),
)
SPEC = Spec("NET-01.3", "NET-01", "2", "deny", "required")
ROOT = Path(__file__).resolve().parents[3]


def make(
    *,
    listeners=LISTENERS,
    mutate=None,
    outcomes=None,
    sink=None,
    clock=lambda: NOW,
    agent_offset=timedelta(0),
):
    evidence, calls, registrations = [], [], []
    outcomes = outcomes or {}

    def tcp(agent, endpoint, nonce):
        role = next((item.role for item in listeners if item.target.endpoint == endpoint), "health")
        negative = agent == TARGET.denied_agent and role != "health"
        calls.append((role, negative))
        outcome = outcomes.get((role, negative), "refused" if negative else "connected")
        return Attempt(nonce, agent, endpoint_id(endpoint), outcome, 2, clock() + agent_offset)

    def dns(agent, name, nonce):
        return Attempt(nonce, agent, name, "resolved", 1, clock() + agent_offset)

    def registration(context):
        row = ListenerSnapshot(context, probe.binding_sha256, clock())
        registrations.append(row)
        return mutate(row, len(registrations)) if mutate else row

    probe = RegisteredNetworkAccessProbe(
        listeners=listeners,
        read_registration=registration,
        run_tcp=tcp,
        run_dns=dns,
        read_policy=lambda c, t: PolicySnapshot(c, t.binding_sha256, SHA, clock()),
        evidence_sink=sink or evidence.append,
        clock=clock,
    )
    return probe, evidence, calls, registrations


def test_same_listener_port_still_checks_two_distinct_services():
    probe, evidence, calls, registrations = make()
    result = probe(CTX, SPEC)
    assert result.observed == "deny" and result.method_version == "2"
    assert calls.count(("database", True)) == calls.count(("pooler", True)) == 1
    assert len(registrations) == 3 and len(evidence) == 3
    saved = json.loads(evidence[-1])
    assert saved["required_roles"] == ["database", "pooler"]
    assert [row["port"] for row in saved["results"]] == [5432, 5432]
    assert b"192.0.2." not in evidence[-1]
    assert {row["target_binding_sha256"] for row in saved["results"]} == {
        t.target.binding_sha256 for t in LISTENERS
    }


@pytest.mark.parametrize("role", ["database", "pooler"])
def test_actual_connection_in_either_role_is_failure(role):
    probe, _, _, _ = make(outcomes={(role, True): "connected"})
    assert probe(CTX, SPEC).observed == "allow"


@pytest.mark.parametrize("role", ["database", "pooler"])
@pytest.mark.parametrize("outcome", ["refused", "timeout", "error"])
def test_missing_positive_control_never_looks_like_denial(role, outcome):
    probe, _, calls, _ = make(outcomes={(role, False): outcome})
    result = probe(CTX, SPEC)
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"
    assert (role, True) not in calls


@pytest.mark.parametrize("point", [1, 2, 3])
@pytest.mark.parametrize("change", ["context", "binding", "stale", "future", "type"])
def test_registration_must_stay_bound_and_fresh(point, change):
    def mutate(row, index):
        if index != point:
            return row
        return {
            "context": replace(row, context=replace(CTX, app="different")),
            "binding": replace(row, binding_sha256="sha256:" + "b" * 64),
            "stale": replace(row, collected_at=NOW - timedelta(seconds=1)),
            "future": replace(row, collected_at=NOW + timedelta(seconds=1)),
            "type": {"binding_sha256": row.binding_sha256},
        }[change]

    probe, evidence, calls, _ = make(mutate=mutate)
    with pytest.raises(ValueError):
        probe(CTX, SPEC)
    assert all("required_roles" not in json.loads(raw) for raw in evidence)
    if point == 1:
        assert calls == []


@pytest.mark.parametrize(
    "listeners",
    [
        LISTENERS[:1],
        LISTENERS + LISTENERS[:1],
        list(LISTENERS),
        (LISTENERS[0], replace(LISTENERS[1], role="database")),
        (
            LISTENERS[0],
            replace(
                LISTENERS[1], target=replace(LISTENERS[1].target, target_uid=TARGET.target_uid)
            ),
        ),
        (
            LISTENERS[0],
            replace(LISTENERS[1], target=replace(LISTENERS[1].target, endpoint=TARGET.endpoint)),
        ),
        (LISTENERS[0], replace(LISTENERS[1], target=replace(LISTENERS[1].target, app="different"))),
        (
            LISTENERS[0],
            replace(LISTENERS[1], target=replace(LISTENERS[1].target, allowed_agent="another")),
        ),
    ],
)
def test_incomplete_duplicate_or_mixed_registration_rejected(listeners):
    with pytest.raises(ValueError):
        make(listeners=listeners)


@pytest.mark.parametrize(
    "change", [{"role": "other"}, {"target": None}, {"registration_sha256": "unchecked"}]
)
def test_unbound_listener_rejected(change):
    with pytest.raises(ValueError):
        replace(LISTENERS[0], **change)


@pytest.mark.parametrize(
    "spec", [replace(SPEC, method_version="1"), replace(SPEC, test_id="NET-01.2")]
)
def test_old_method_never_opens_connections(spec):
    probe, _, calls, _ = make()
    with pytest.raises(ValueError):
        probe(CTX, spec)
    assert not calls


def test_sink_failure_never_returns_completed_observation():
    def fail(_):
        raise OSError("storage unavailable")

    probe, _, _, _ = make(sink=fail)
    with pytest.raises(OSError):
        probe(CTX, SPEC)


def test_deadline_cannot_restart_for_second_listener():
    elapsed = 0

    def clock():
        return NOW + timedelta(seconds=elapsed)

    def mutate(row, index):
        nonlocal elapsed
        if index == 2:
            elapsed = 61
        return row

    probe, evidence, calls, _ = make(clock=clock, mutate=mutate)
    with pytest.raises(ValueError):
        probe(CTX, SPEC)
    assert ("pooler", True) not in calls and len(evidence) == 1


def test_candidate_catalog_does_not_rewrite_existing_full_profile():
    schema = json.loads((ROOT / "docs/controls/control-result.schema.json").resolve().read_text())
    current = json.loads((ROOT / "docs/controls/profiles.json").resolve().read_text())
    candidate = json.loads(
        (ROOT / "services/control-runner/docs/candidates/network-method-2-profile.json").read_text()
    )
    old, new = Catalog(current, schema), Catalog(candidate, schema)
    assert len(new.profile("full", "2")) == 20
    assert (
        next(t for t in old.profile("full", "1") if t.test_id == SPEC.test_id).method_version == "1"
    )
    with pytest.raises(ValueError):
        old.profile("full", "2")
    probe, _, _, _ = make()
    run = Runner(new, {SPEC.test_id: probe}, clock=lambda: NOW).run(
        profile_id="full",
        profile_version="2",
        app=CTX.app,
        environment=CTX.environment,
        registered_context=CTX,
    )
    row = next(r for r in run["results"] if r["test_id"] == SPEC.test_id)
    assert row["verdict"] == "PASS" and row["method_version"] == "2"
    assert sum(r["verdict"] == "UNKNOWN" for r in run["results"]) == 19


def test_namespace_and_external_methods_are_not_interchangeable():
    listeners = tuple(
        replace(item, target=replace(item.target, dns_name=None)) for item in LISTENERS
    )
    probe, _, calls, _ = make(listeners=listeners)
    with pytest.raises(ValueError):
        probe(CTX, SPEC)
    assert not calls
    result = probe(CTX, replace(SPEC, test_id="NET-01.2"))
    assert result.observed == "deny" and result.method_version == "2"


def test_old_runner_rejects_candidate_method_observation():
    schema = json.loads((ROOT / "docs/controls/control-result.schema.json").read_text())
    catalog = Catalog(json.loads((ROOT / "docs/controls/profiles.json").read_text()), schema)
    probe, _, _, _ = make()
    observation = probe(CTX, SPEC)
    run = Runner(catalog, {SPEC.test_id: lambda *_: observation}, clock=lambda: NOW).run(
        profile_id="full",
        profile_version="1",
        app=CTX.app,
        environment=CTX.environment,
        registered_context=CTX,
    )
    row = next(r for r in run["results"] if r["test_id"] == SPEC.test_id)
    assert row["verdict"] == "UNKNOWN"


@pytest.mark.parametrize("offset", [-1000, -400, 400, 1000])
def test_small_agent_offset_retained_but_coordinator_dates_evidence(offset):
    probe, evidence, _, _ = make(agent_offset=timedelta(milliseconds=offset))
    result = probe(CTX, SPEC)
    assert result.observed == "deny" and result.collected_at == NOW
    for raw in evidence[:2]:
        for row in json.loads(raw)["observations"].values():
            assert datetime.fromisoformat(row["observed_at"]) == NOW
            assert datetime.fromisoformat(row["agent_observed_at"]) == NOW + timedelta(
                milliseconds=offset
            )
            assert row["max_clock_offset_ms"] == 1000


@pytest.mark.parametrize("offset", [-1001, 1001, 10000])
def test_excessive_agent_offset_still_rejects(offset):
    probe, evidence, _, _ = make(agent_offset=timedelta(milliseconds=offset))
    with pytest.raises(ValueError):
        probe(CTX, SPEC)
    assert evidence == []
