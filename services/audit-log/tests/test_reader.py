"""Public reader and write-receipt registration (review #270, T21 console audit screen)."""

import json

import pytest
from test_adapters_and_cli import _rehashed_overwrite, _stored_run, _VersionedS3, _write

import bravo_audit
from bravo_audit import RunRefused, read_local_run, read_run
from bravo_audit.adapters import from_agent_tool_event
from bravo_audit.cli import main
from bravo_audit.ledger import RunLedger

PLAN, RUN = "plan-demo", "run-demo-1"
SANDBOX_BUCKET_DELETE = {"run_id": RUN, "attempt": 1, "tool": "sandbox.exec", "outcome": "DENIED",
                         "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}


def _append(tmp_path, events, *extra, source="platform.events", name="events.jsonl"):
    args = ["append", "--source", source, "--input", _write(tmp_path, events, name), "--sink", str(tmp_path / "store"),
            "--receipts", str(tmp_path / f"{name}.receipts.json"), *extra]
    if source == "agent.tools":
        args += ["--plan", PLAN, "--resume-from", str(tmp_path / "store/audit" / PLAN / RUN)]
    return main(args)


def _registered_store(tmp_path, platform_events):
    """Two platform records written with --registration-out; returns (S3 objects, registration)."""
    reg_path = tmp_path / "registration.json"
    objects = _stored_run(tmp_path, platform_events)
    # _stored_run wrote without a registration; write the next run of the same chain with one.
    assert _append(tmp_path, [SANDBOX_BUCKET_DELETE], "--registration-out", str(reg_path),
                   source="agent.tools", name="tools.jsonl") == 0
    assert reg_path.stat().st_mode & 0o777 == 0o600
    run_dir = tmp_path / "store/audit" / PLAN / RUN
    objects = {f"audit/{PLAN}/{RUN}/{p.name}": json.loads(p.read_text()) for p in sorted(run_dir.glob("*.json"))}
    return objects, json.loads(reg_path.read_text())


def test_registration_comes_from_the_last_write_receipt(tmp_path, platform_events):
    objects, reg = _registered_store(tmp_path, platform_events)
    last = objects[sorted(objects)[-1]]
    assert set(reg) == {"schema_version", "plan_id", "run_id", "record_count", "chain_digest", "last_sequence",
                        "written_at"}
    assert (reg["plan_id"], reg["run_id"], reg["record_count"], reg["last_sequence"]) == (PLAN, RUN, 3, 3)
    receipts = json.loads((tmp_path / "tools.jsonl.receipts.json").read_text())
    assert reg["chain_digest"] == last["sha256"] == receipts["receipts"][-1]["sha256"]
    assert receipts["registration"] == reg
    # A call that writes nothing (only redeliveries) leaves the host's registration alone.
    (tmp_path / "registration.json").unlink()
    assert _append(tmp_path, [SANDBOX_BUCKET_DELETE], "--registration-out", str(tmp_path / "registration.json"),
                   source="agent.tools", name="tools.jsonl") == 0
    assert not (tmp_path / "registration.json").exists()


def test_happy_path_returns_verified_records(tmp_path, platform_events):
    objects, reg = _registered_store(tmp_path, platform_events)
    fake = _VersionedS3(objects)
    run = read_run(fake, "bucket", "audit", PLAN, RUN, registration=reg)
    assert isinstance(run, bravo_audit.VerifiedRun)
    assert (run.record_count, run.chain_digest, run.registered, run.unregistered_records) == (3, reg["chain_digest"], True, 0)
    assert [r["sequence"] for r in run.records] == [1, 2, 3]
    assert fake.prefixes == [f"audit/{PLAN}/{RUN}/"]
    unanchored = read_run(_VersionedS3(objects), "bucket", "audit", PLAN, RUN)
    assert unanchored.registered is False and unanchored.chain_digest == reg["chain_digest"]
    local = read_local_run(tmp_path / "store", PLAN, RUN, registration=reg)
    assert local.records == run.records


def test_sandbox_exec_bucket_delete_round_trips(tmp_path, platform_events):
    """T11 sandbox_provider (#503) audits blocked commands as tool=sandbox.exec."""
    assert from_agent_tool_event(SANDBOX_BUCKET_DELETE, PLAN, 0)[3] == "DESTRUCTIVE_ATTEMPT_BLOCKED"
    objects, reg = _registered_store(tmp_path, platform_events)
    for run in (read_run(_VersionedS3(objects), "bucket", "audit", PLAN, RUN, registration=reg),
                read_local_run(tmp_path / "store", PLAN, RUN, registration=reg)):
        last = run.records[-1]
        assert (last["source"], last["kind"]) == ("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED")
        assert (last["payload"]["tool"], last["payload"]["category"]) == ("sandbox.exec", "bucket_delete")


@pytest.mark.parametrize("tool", ["sandbox.stdin", "sandbox.apply_patch", "plan.propose", "unlisted", "model"])
def test_any_t11_tool_name_is_accepted(tool):
    assert from_agent_tool_event(SANDBOX_BUCKET_DELETE | {"tool": tool}, PLAN, 0)[5]["tool"] == tool


@pytest.mark.parametrize("tool", ["Sandbox.Exec", "sandbox exec", "", "independent_verifier", 7])
def test_tool_names_outside_the_t11_rule_are_refused(tool):
    with pytest.raises(ValueError):
        from_agent_tool_event(SANDBOX_BUCKET_DELETE | {"tool": tool}, PLAN, 0)


def test_overwritten_version_is_refused(tmp_path, platform_events):
    tampered = _rehashed_overwrite(_stored_run(tmp_path, platform_events))
    with pytest.raises(RunRefused) as caught:
        read_run(_VersionedS3(tampered), "bucket", "audit", PLAN, RUN)
    assert (caught.value.code, caught.value.sequence) == ("overwritten", 2)


def test_delete_marker_is_refused(tmp_path, platform_events):
    objects = _stored_run(tmp_path, platform_events)
    with pytest.raises(RunRefused) as caught:
        read_run(_VersionedS3(objects, markers=[sorted(objects)[0]]), "bucket", "audit", PLAN, RUN)
    assert (caught.value.code, caught.value.sequence) == ("delete_marker", 1)


def _forged_chain(count):
    """A complete, consistently re-hashed replacement of the run, each key a single version
    (e.g. written to a fresh prefix or bucket the reader is pointed at)."""
    forged = RunLedger(PLAN, RUN, now=lambda: "2026-11-05T10:00:00Z")
    out = {}
    for n in range(1, count + 1):
        record, _ = forged.append("platform.events", "gate.completed", {"decision": "PASS", "n": n}, f"t10:{PLAN}:{n}")
        out[f"audit/{PLAN}/{RUN}/{n:08d}.json"] = record.as_dict()
    return out


def test_a_fully_rehashed_rewrite_is_caught_only_by_the_registration(tmp_path, platform_events):
    _objects, reg = _registered_store(tmp_path, platform_events)
    forged = _forged_chain(reg["record_count"])
    assert read_run(_VersionedS3(forged), "bucket", "audit", PLAN, RUN).record_count == 3  # chain alone passes
    with pytest.raises(RunRefused) as caught:
        read_run(_VersionedS3(forged), "bucket", "audit", PLAN, RUN, registration=reg)
    assert caught.value.code == "registration_mismatch"


def test_registration_anchors_a_prefix_of_a_growing_run(tmp_path, platform_events):
    objects, reg = _registered_store(tmp_path, platform_events)
    first_two = {k: v for k, v in objects.items() if not k.endswith("00000003.json")}
    with pytest.raises(RunRefused) as caught:  # fewer records than registered
        read_run(_VersionedS3(first_two), "bucket", "audit", PLAN, RUN, registration=reg)
    assert caught.value.code == "registration_mismatch"
    with pytest.raises(RunRefused) as caught:  # nothing at all where a registered run should be
        read_run(_VersionedS3({}), "bucket", "audit", PLAN, RUN, registration=reg)
    assert caught.value.code == "registration_mismatch"
    # The writer kept going after the host took this registration: later records are counted, not shown.
    assert _append(tmp_path, [SANDBOX_BUCKET_DELETE | {"attempt": 2}], source="agent.tools", name="more.jsonl") == 0
    run = read_local_run(tmp_path / "store", PLAN, RUN, registration=reg)
    assert (run.record_count, run.unregistered_records, run.chain_digest) == (3, 1, reg["chain_digest"])


@pytest.mark.parametrize(("change", "code"), [
    (lambda r: r | {"run_id": "run-demo-2"}, "registration_mismatch"),
    (lambda r: r | {"last_sequence": 2}, "registration_invalid"),
    (lambda r: r | {"extra": 1}, "registration_invalid"),
    (lambda r: "not a registration", "registration_invalid"),
])
def test_registration_for_another_run_or_malformed_is_refused(tmp_path, platform_events, change, code):
    objects, reg = _registered_store(tmp_path, platform_events)
    with pytest.raises(RunRefused) as caught:
        read_run(_VersionedS3(objects), "bucket", "audit", PLAN, RUN, registration=change(reg))
    assert caught.value.code == code


def test_store_errors_and_bad_identifiers_become_fixed_codes(tmp_path):
    class Broken:
        def get_paginator(self, _name):
            raise RuntimeError("AccessDenied on bucket secret-bucket-name")

    with pytest.raises(RunRefused) as caught:
        read_run(Broken(), "bucket", "audit", PLAN, RUN)
    assert caught.value.code == "store_unreadable" and "secret-bucket-name" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    with pytest.raises(RunRefused) as caught:
        read_run(Broken(), "bucket", "audit", "../plan", RUN)
    assert caught.value.code == "invalid_identifier"
    with pytest.raises(RunRefused) as caught:
        read_local_run(tmp_path, PLAN, RUN)
    assert caught.value.code == "run_not_found"


def test_record_under_another_key_is_refused(tmp_path, platform_events):
    objects = _stored_run(tmp_path, platform_events)
    keys = sorted(objects)
    moved = {keys[0]: objects[keys[0]], keys[1].replace("00000002", "00000007"): objects[keys[1]]}
    with pytest.raises(RunRefused) as caught:
        read_run(_VersionedS3(moved), "bucket", "audit", PLAN, RUN)
    assert caught.value.code == "record_outside_its_run"


def test_cli_verify_against_a_registration(monkeypatch, tmp_path, platform_events, capsys):
    from test_adapters_and_cli import _boto

    objects, reg = _registered_store(tmp_path, platform_events)
    reg_path = tmp_path / "registration.json"
    _boto(monkeypatch, _VersionedS3(objects))
    capsys.readouterr()
    assert main(["verify", f"s3://bucket/audit/{PLAN}/{RUN}", "--registration", str(reg_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["ok"], out["records"], out["registered"], out["chain_digest"]) == (True, 3, True, reg["chain_digest"])
    _boto(monkeypatch, _VersionedS3(_forged_chain(3)))
    assert main(["verify", f"s3://bucket/audit/{PLAN}/{RUN}", "--registration", str(reg_path)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "registration_mismatch"
