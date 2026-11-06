"""Actual T14/T19 code, synthetic connectivity and server authority only."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from bravo_gate.contract import GateError, canonical, decode
from bravo_gate.models import RunSnapshot
from bravo_gate.network_bundle import (
    CollectedNetworkProofAuthority,
    NetworkPathRegistration,
    NetworkRunRegistration,
    output_digest,
)


def test_t14_two_path_originals_to_full_t19_gate(service, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "control-runner/src"))
    from bravo_controls.network_probes import (
        Attempt,
        Endpoint,
        NetworkAccessProbe,
        NetworkTarget,
        PolicySnapshot,
        endpoint_id,
    )
    from bravo_controls.runner import Catalog, Environment, Observation, Runner

    app, state = service
    now = state["now"]
    original = replace(state["context"], profile_id="full")
    catalog = Catalog(app.contract.catalog, app.contract.schema)
    artifacts, registrations = {}, {}
    sha = "sha256:" + "a" * 64

    def sink(raw):
        artifacts[output_digest(raw)] = raw

    for side in ("source", "target"):
        env = Environment(
            getattr(original, side + "_environment_id"),
            side,
            getattr(original, side + "_environment_ref"),
        )
        database = NetworkTarget(
            original.app,
            env,
            "db-uid",
            Endpoint("192.0.2.10", 5432),
            "allowed-pod",
            "denied-pod",
            Endpoint("192.0.2.20", 8080),
            "health.test",
        )
        pooler = replace(database, target_uid="pooler-uid", endpoint=Endpoint("192.0.2.11", 6432))
        targets = (database, pooler)

        def tcp(agent, endpoint, nonce):
            negative = agent == "denied-pod" and endpoint.port in {5432, 6432}
            return Attempt(
                nonce, agent, endpoint_id(endpoint), "refused" if negative else "connected", 2, now
            )

        def dns(agent, name, nonce):
            return Attempt(nonce, agent, name, "resolved", 2, now)

        networks = {}
        for test in ("NET-01.2", "NET-01.3"):
            bound_targets = (
                tuple(replace(t, dns_name=None) for t in targets) if test == "NET-01.2" else targets
            )
            networks[test] = NetworkAccessProbe(
                targets=bound_targets,
                run_tcp=tcp,
                run_dns=dns,
                read_policy=lambda ctx, target: PolicySnapshot(
                    ctx, target.binding_sha256, sha, now
                ),
                evidence_sink=sink,
                clock=lambda: now,
            )
            registrations[(side, test)] = tuple(
                NetworkPathRegistration(role, target.endpoint.port, target.binding_sha256, sha)
                for role, target in zip(("database", "pooler"), bound_targets, strict=True)
            )

        def synthetic(context, spec):
            return Observation(
                context,
                spec.test_id,
                spec.method_version,
                spec.expected,
                "pass" if spec.positive_control == "required" else "not_applicable",
                now,
                "synthetic-test",
                "1",
                sha,
            )

        probes = {
            spec.test_id: networks.get(spec.test_id, synthetic)
            for spec in catalog.profile("full", "1")
        }
        run = Runner(catalog, probes, clock=lambda: now).run(
            profile_id="full", profile_version="1", app=original.app, environment=env
        )
        state["runs"][side] = RunSnapshot(getattr(original, side + "_ref"), canonical(run))
    state["context"] = replace(
        original,
        **{side + "_digest": state["runs"][side].fingerprint for side in ("source", "target")},
    )
    ctx = state["context"]
    bindings = {
        (side, test): NetworkRunRegistration(
            ctx.fingerprint,
            side,
            ctx.app,
            getattr(ctx, side + "_environment_ref"),
            state["runs"][side].document["run_id"],
            test,
            "1",
            registrations[(side, test)],
            (now + timedelta(seconds=30)).isoformat(),
        )
        for side in ("source", "target")
        for test in ("NET-01.2", "NET-01.3")
    }
    app.network_verifier = CollectedNetworkProofAuthority(
        binding_provider=lambda _, __, side, test: bindings[(side, test)],
        authenticate_binding=lambda *_: True,
        artifact_provider=lambda *args: artifacts[args[-1]],
        authenticate_artifact=lambda *_: True,
        clock=lambda: now,
    )
    for side in ("source", "target"):
        for row in state["runs"][side].document["results"]:
            if row["test_id"] in {"NET-01.2", "NET-01.3"}:
                app.network_verifier("actor", ctx, side, row["test_id"], row)
    report = app.evaluate("actor", "context")
    assert report["decision"] == "PASS" and len(report["rows"]) == 20
    assert app.require_security("actor", "context", canonical(report)).authorizes_cutover is False

    # A retained aggregate and run PASS cannot conceal a missing child artifact.
    target_row = next(
        row for row in state["runs"]["target"].document["results"] if row["test_id"] == "NET-01.3"
    )
    aggregate = decode(artifacts[target_row["evidence"]["output_sha256"]])
    artifacts.pop(aggregate["results"][1]["output_sha256"])
    blocked = app.evaluate("actor", "context")
    row = next(row for row in blocked["rows"] if row["test_id"] == "NET-01.3")
    assert row["decision"] == "BLOCK" and row["reason"] == "TARGET_UNKNOWN"
    with pytest.raises(GateError):
        app.require_security("actor", "context", canonical(report))
