import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bravo_controls.runner import Catalog, Environment, RunContext, Runner
from bravo_controls.secret_rotation import (
    SPEC,
    AppDatabaseCheck,
    ProcessIdentity,
    RotationTarget,
    RotationTrial,
    SecretRotationProbe,
)

NOW = datetime(2026, 11, 6, tzinfo=UTC)
SHA = "sha256:" + "a" * 64
ENV = Environment("source", "source", "fixture:source")
CTX = RunContext("rotation-one", "chat", ENV)
TARGET = RotationTarget("chat", ENV.ref, "workload-one", "db-one", "chat-app",
                        "secrets:fixture/chat", SHA)
PROCESS = ProcessIdentity("pod-one", "container-one", NOW - timedelta(minutes=1), 0)


def trial():
    before = AppDatabaseCheck(PROCESS, NOW, NOW + timedelta(seconds=1), 200, 201,
                              "before-one", 1, SHA, NOW, SHA)
    after = AppDatabaseCheck(PROCESS, NOW + timedelta(seconds=5), NOW + timedelta(seconds=6),
                             200, 201, "after-one", 1, SHA, NOW + timedelta(seconds=4), SHA)
    return RotationTrial(CTX, TARGET, "execution-one", before, NOW + timedelta(seconds=2),
                         NOW + timedelta(seconds=3), NOW + timedelta(seconds=3.5),
                         "version-old", "version-new", True, True, True, after, 4000,
                         NOW + timedelta(seconds=7), SHA)


def observe(value=None, *, current=None, target=TARGET, context=CTX, spec=SPEC):
    value = trial() if value is None else value
    now = max(NOW + timedelta(seconds=10), value.finished_at)
    current = ((target, value.after.process, value.new_version, now)
               if current is None else current)
    retained = []
    probe = SecretRotationProbe(target, trial_provider=lambda _: value,
                                read_current=lambda *_: current,
                                evidence_sink=retained.append, clock=lambda: now)
    return probe(context, spec), retained


def test_complete_rotation_and_fresh_database_write_pass():
    result, retained = observe()
    assert result.observed == "success"
    assert result.positive_control == "pass"
    payload = json.loads(retained[0])
    assert payload["recovery_ms"] == 4000
    assert payload["process_unchanged"] is True
    assert TARGET.secret_ref not in retained[0].decode()
    assert "version-old" not in retained[0].decode()
    assert "version-new" not in retained[0].decode()


@pytest.mark.parametrize("field,value", [("old_version", "version-new"),
                                        ("old_credential_rejected", False),
                                        ("new_credential_accepted", False),
                                        ("old_sessions_closed", False)])
def test_rotation_or_fencing_missing_is_unknown(field, value):
    result, _ = observe(replace(trial(), **{field: value}))
    assert result.observed is None
    assert result.unknown_reason == "UNEXPECTED_RESPONSE"


@pytest.mark.parametrize("field,value", [("health_status", 503), ("write_status", 200),
                                        ("committed_rows", 0), ("committed_rows", 2),
                                        ("backend_started_at", None)])
def test_baseline_requires_db_health_and_new_committed_write(field, value):
    receipt = trial()
    result, _ = observe(replace(receipt, before=replace(receipt.before, **{field: value})))
    assert result.observed is None
    assert result.positive_control == "fail"
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"


@pytest.mark.parametrize("field,value", [("pod_uid", "other-pod"),
                                        ("container_id", "other-container"),
                                        ("restart_count", 1),
                                        ("process_started_at", NOW + timedelta(seconds=4))])
def test_restart_is_failure_even_when_http_and_data_are_healthy(field, value):
    receipt = trial()
    process = replace(PROCESS, **{field: value})
    result, _ = observe(replace(receipt, after=replace(receipt.after, process=process)))
    assert result.observed == "failure"


def test_restart_after_trial_is_not_hidden():
    result, _ = observe(current=(TARGET, replace(PROCESS, restart_count=1), "version-new",
                                 NOW + timedelta(seconds=10)))
    assert result.observed == "failure"


@pytest.mark.parametrize("elapsed,expected", [(29999, "success"), (30000, "success"),
                                            (30001, "failure")])
def test_recovery_budget_boundary(elapsed, expected):
    receipt = trial()
    end = receipt.requested_at + timedelta(milliseconds=elapsed)
    result, _ = observe(replace(receipt, after=replace(receipt.after, completed_at=end),
                                recovery_ms=elapsed, finished_at=end))
    assert result.observed == expected


@pytest.mark.parametrize("elapsed,expected", [(4000, None), (30000, "failure")])
def test_temporary_unavailability_before_deadline_cannot_be_a_final_failure(elapsed, expected):
    receipt = trial()
    end = receipt.requested_at + timedelta(milliseconds=elapsed)
    after = replace(receipt.after, completed_at=end, health_status=503, write_status=503,
                    committed_rows=0, backend_started_at=None)
    result, _ = observe(replace(receipt, after=after, recovery_ms=elapsed, finished_at=end))
    assert result.observed == expected


def test_readyz_or_an_existing_backend_cannot_prove_credential_refresh():
    receipt = trial()
    result, _ = observe(replace(receipt, after=replace(receipt.after,
                                                     backend_started_at=NOW)))
    assert result.observed is None


def test_reused_write_marker_is_unknown():
    receipt = trial()
    result, _ = observe(replace(receipt, after=replace(receipt.after,
                                                     marker_id=receipt.before.marker_id)))
    assert result.observed is None


@pytest.mark.parametrize("target", [replace(TARGET, app="other-app"),
                                    replace(TARGET, environment_ref="fixture:target"),
                                    replace(TARGET, database_uid="other-db"),
                                    replace(TARGET, workload_uid="other-workload"),
                                    replace(TARGET, database_role="other-role"),
                                    replace(TARGET, secret_ref="secrets:other"),
                                    replace(TARGET, revision_sha256="sha256:" + "b" * 64)])
def test_trial_cannot_move_between_registrations(target):
    with pytest.raises(ValueError):
        observe(replace(trial(), target=target))


@pytest.mark.parametrize("current", [(TARGET, PROCESS, "another-version", NOW),
                                     (TARGET, PROCESS, "version-new", NOW),
                                     (TARGET, PROCESS, "version-new", NOW + timedelta(days=1)),
                                     (TARGET, None, "version-new", NOW)])
def test_current_readback_required(current):
    with pytest.raises(ValueError):
        observe(current=current)


@pytest.mark.parametrize("context", [replace(CTX, run_id="another-run"),
                                     replace(CTX, app="other-app"),
                                     replace(CTX, environment=Environment("target", "target", ENV.ref))])
def test_context_cannot_be_rebound(context):
    with pytest.raises(ValueError):
        observe(context=context)


@pytest.mark.parametrize("bad", [True, -1, float("nan"), 4.0, "4000", 0])
def test_duration_must_be_finite_monotonic_measurement_consistent_with_window(bad):
    with pytest.raises(ValueError):
        replace(trial(), recovery_ms=bad)


@pytest.mark.parametrize("field", ["requested_at", "applied_at", "fenced_at", "finished_at"])
def test_stage_order_and_aware_timestamps(field):
    with pytest.raises(ValueError):
        replace(trial(), **{field: NOW.replace(tzinfo=None)})


def test_stale_trial_is_rejected():
    probe = SecretRotationProbe(TARGET, trial_provider=lambda _: trial(),
                                read_current=lambda *_: pytest.fail("must not read stale trial"),
                                evidence_sink=lambda _: None,
                                clock=lambda: NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        probe(CTX, SPEC)


def test_evidence_sink_failure_cannot_return_a_pass():
    def broken(_):
        raise OSError("private transport detail")

    probe = SecretRotationProbe(TARGET, trial_provider=lambda _: trial(),
                                read_current=lambda *_: (TARGET, PROCESS, "version-new", NOW + timedelta(seconds=10)),
                                evidence_sink=broken, clock=lambda: NOW + timedelta(seconds=10))
    with pytest.raises(OSError):
        probe(CTX, SPEC)


def test_qualification_never_reexecutes_a_rotation():
    receipt = trial()
    for _ in range(2):
        result, _ = observe(receipt)
        assert result.observed == "success"
    # There is deliberately no rotate/fence callback or credential in this reader.
    assert "password" not in repr(receipt)


@pytest.mark.parametrize("broken", [False, True])
def test_full_profile_keeps_other_nineteen_unknown_and_hides_provider_errors(broken):
    root = Path(__file__).resolve().parents[3]
    catalog = Catalog(json.loads((root / "docs/controls/profiles.json").read_text()),
                      json.loads((root / "docs/controls/control-result.schema.json").read_text()))
    now = NOW + timedelta(seconds=10)

    def provide(_):
        if broken:
            raise RuntimeError("password=private-fixture-must-not-be-published")
        return trial()

    probe = SecretRotationProbe(TARGET, trial_provider=provide,
                                read_current=lambda *_: (TARGET, PROCESS, "version-new", now),
                                evidence_sink=lambda _: None, clock=lambda: now)
    result = Runner(catalog, {SPEC.test_id: probe}, clock=lambda: now).run(
        profile_id="full", profile_version="1", app=CTX.app, environment=ENV,
        registered_context=CTX)
    row = next(r for r in result["results"] if r["test_id"] == SPEC.test_id)
    assert row["verdict"] == ("UNKNOWN" if broken else "PASS")
    assert len(result["results"]) == 20
    assert all(r["verdict"] == "UNKNOWN" for r in result["results"] if r is not row)
    assert "private-fixture" not in json.dumps(result)
