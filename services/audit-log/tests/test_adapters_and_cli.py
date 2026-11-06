import json

import pytest

from bravo_audit.adapters import from_agent_tool_event, from_platform_event
from bravo_audit.cli import main


def test_platform_events_keep_type_as_kind(platform_events):
    plan, run, source, kind, origin, payload = from_platform_event(platform_events[1])
    assert (plan, run, source, kind, origin) == ("plan-demo", "run-demo-1", "platform.events", "gate.completed", "t10:plan-demo:2")
    assert payload["data"]["decision"] == "BLOCK" and "plan_id" not in payload
    platform_events[0]["event_id"] = "plan-demo:9"
    with pytest.raises(ValueError):
        from_platform_event(platform_events[0])
    platform_events[1]["event_type"] = "model.said"
    with pytest.raises(ValueError):
        from_platform_event(platform_events[1])
    unknown_version = platform_events[1] | {"event_type": "gate.completed", "schema_version": "9.9"}
    with pytest.raises(ValueError, match="schema version"):
        from_platform_event(unknown_version)


def test_tool_events_expose_reason_as_kind_for_the_demo_scene(tool_events):
    plan, run, source, kind, origin, payload = from_agent_tool_event(tool_events[1], "plan-demo", 1)
    assert (source, kind, origin) == ("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED", "t11:run-demo-1:attempt1:1")
    assert payload == {"attempt": 1, "tool": "rehearsal.exec", "outcome": "DENIED",
                       "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}
    tool_events[0]["outcome"] = "MAYBE"
    with pytest.raises(ValueError):
        from_agent_tool_event(tool_events[0], "plan-demo", 0)


def test_cli_append_then_verify_round_trip(tmp_path, platform_events, tool_events, capsys):
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(e) for e in platform_events), encoding="utf-8")
    assert main(["append", "--source", "platform.events", "--input", str(events), "--sink", str(tmp_path / "store"),
                 "--receipts", str(tmp_path / "r1.json")]) == 0
    run_dir = tmp_path / "store/audit/plan-demo/run-demo-1"
    stored = sorted(run_dir.glob("*.json"))
    assert [p.name for p in stored] == ["00000001.json", "00000002.json"]
    # continue the same run with tool events, resuming from what is stored
    resume = tmp_path / "stored.jsonl"
    resume.write_text("\n".join(p.read_text(encoding="utf-8") for p in stored), encoding="utf-8")
    tools = tmp_path / "tools.jsonl"
    tools.write_text("\n".join(json.dumps(e) for e in tool_events), encoding="utf-8")
    assert main(["append", "--source", "agent.tools", "--plan", "plan-demo", "--input", str(tools),
                 "--sink", str(tmp_path / "store"), "--receipts", str(tmp_path / "r2.json"),
                 "--resume-from", str(resume)]) == 0
    assert main(["verify", str(run_dir)]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["records"] == 4
    receipts = json.loads((tmp_path / "r2.json").read_text(encoding="utf-8"))
    assert receipts["records"] == 2 and receipts["receipts"][0]["key"].endswith("00000003.json")
    # redelivery of the same upstream events after a resume creates no new records
    resume.write_text("\n".join(p.read_text(encoding="utf-8") for p in sorted(run_dir.glob("*.json"))), encoding="utf-8")
    assert main(["append", "--source", "agent.tools", "--plan", "plan-demo", "--input", str(tools),
                 "--sink", str(tmp_path / "store"), "--receipts", str(tmp_path / "r3.json"),
                 "--resume-from", str(resume)]) == 0
    r3 = json.loads((tmp_path / "r3.json").read_text(encoding="utf-8"))
    assert r3["records"] == 0 and r3["absorbed_redeliveries"] == 2 and len(list(run_dir.glob("*.json"))) == 4
    stored_last = json.loads(sorted(run_dir.glob("*.json"))[-1].read_text(encoding="utf-8"))["sha256"]
    assert r3["chain_digest"] == stored_last  # review: a duplicate-only resume keeps the stored chain digest
    # same upstream id with different content is a conflict, not a new record
    changed = dict(platform_events[0])
    changed["data"] = {"from": None, "to": "ANALYZE"}
    events.write_text(json.dumps(changed), encoding="utf-8")
    assert main(["append", "--source", "platform.events", "--input", str(events), "--sink", str(tmp_path / "store"),
                 "--receipts", str(tmp_path / "r4.json"), "--resume-from", str(resume)]) == 3
    assert len(list(run_dir.glob("*.json"))) == 4


def test_cli_rejects_events_of_another_run(tmp_path, platform_events):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(platform_events[0]), encoding="utf-8")
    assert main(["append", "--source", "platform.events", "--run", "run-other", "--input", str(events),
                 "--sink", str(tmp_path / "store"), "--receipts", str(tmp_path / "r.json")]) == 2


@pytest.mark.parametrize("mutate", ["run", "plan"])
def test_cli_rejects_a_batch_that_mixes_runs_even_without_filters(tmp_path, platform_events, mutate):
    other = dict(platform_events[1])
    if mutate == "run":
        other["run_id"] = "run-demo-2"
    else:
        other["plan_id"], other["event_id"] = "plan-other", "plan-other:2"
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(platform_events[0]) + "\n" + json.dumps(other), encoding="utf-8")
    assert main(["append", "--source", "platform.events", "--input", str(events), "--sink", str(tmp_path / "store"),
                 "--receipts", str(tmp_path / "r.json")]) == 2
    assert not (tmp_path / "store").exists()



def _write(tmp_path, events, name="events.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return str(path)


class _VersionedS3:
    """Stands in for list_object_versions/get_object only; the live path was exercised against a
    real bucket (docs/evidence/t24-audit-live-2026-11-06). ``objects`` maps key -> list of
    versions (oldest first); ``markers`` are keys with a delete marker."""

    def __init__(self, objects, markers=()):
        self.objects = {k: (v if isinstance(v, list) else [v]) for k, v in objects.items()}
        self.markers = set(markers)
        self.prefixes = []

    def get_paginator(self, name):
        assert name == "list_object_versions"  # never the current-version-only listing
        outer = self

        class _Pages:
            def paginate(self, Bucket, Prefix):
                outer.prefixes.append(Prefix)
                # Unordered on purpose: the loader must sort by key, i.e. by zero-padded sequence.
                yield {"Versions": [{"Key": k, "VersionId": f"v{i}"} for k in reversed(list(outer.objects))
                                    if k.startswith(Prefix) for i, _ in enumerate(outer.objects[k])],
                       "DeleteMarkers": [{"Key": k, "VersionId": "dm"} for k in outer.markers if k.startswith(Prefix)]}
        return _Pages()

    def get_object(self, Bucket, Key, VersionId):
        import io
        return {"Body": io.BytesIO(json.dumps(self.objects[Key][int(VersionId[1:])]).encode())}


def _stored_run(tmp_path, platform_events):
    assert main(["append", "--source", "platform.events", "--input", _write(tmp_path, platform_events),
                 "--sink", str(tmp_path / "store"), "--receipts", str(tmp_path / "r.json")]) == 0
    run_dir = tmp_path / "store/audit/plan-demo/run-demo-1"
    return {f"audit/plan-demo/run-demo-1/{p.name}": json.loads(p.read_text()) for p in sorted(run_dir.glob("*.json"))}


def _boto(monkeypatch, fake):
    import sys
    import types
    # boto3 is the optional "aws" extra; CI runs without it, so the module itself is stood in.
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda service: fake))


def test_s3_records_are_read_in_sequence_order_under_one_run_prefix(monkeypatch, tmp_path, platform_events, capsys):
    objects = _stored_run(tmp_path, platform_events)
    objects["audit/plan-demo/run-demo-10/00000001.json"] = {"not": "this run"}
    fake = _VersionedS3(objects)
    _boto(monkeypatch, fake)
    capsys.readouterr()
    assert main(["verify", "s3://bucket/audit/plan-demo/run-demo-1"]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 2
    # The trailing slash keeps run-demo-10 out of run-demo-1.
    assert fake.prefixes == ["audit/plan-demo/run-demo-1/"]


def _rehashed_overwrite(objects):
    """Review #257 P1 reproduction: rewrite record 2 with a consistent re-hashed chain as a new version."""
    from bravo_audit.ledger import RunLedger

    keys = sorted(objects)
    first = objects[keys[0]]
    forged = RunLedger.resume("plan-demo", "run-demo-1", [first])
    record, _ = forged.append("platform.events", "gate.completed", {"decision": "PASS", "forged": True}, "t10:forged")
    out = dict(objects)
    out[keys[1]] = [objects[keys[1]], record.as_dict()]
    return out


def test_verify_and_resume_refuse_a_record_rewritten_as_a_new_version(monkeypatch, tmp_path, platform_events, capsys):
    tampered = _rehashed_overwrite(_stored_run(tmp_path, platform_events))
    _boto(monkeypatch, _VersionedS3(tampered))
    capsys.readouterr()
    assert main(["verify", "s3://bucket/audit/plan-demo/run-demo-1"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["reason"] == "overwritten:2"
    tools = _write(tmp_path, [{"run_id": "run-demo-1", "attempt": 1, "tool": "repo.read", "outcome": "ALLOWED",
                               "reason": "POLICY_ACCEPTED"}], "t.jsonl")
    assert main(["append", "--source", "agent.tools", "--plan", "plan-demo", "--input", tools, "--sink",
                 str(tmp_path / "store2"), "--receipts", str(tmp_path / "r2.json"),
                 "--resume-from", "s3://bucket/audit/plan-demo/run-demo-1"]) == 3
    assert not (tmp_path / "store2").exists()


def test_verify_refuses_a_delete_marker(monkeypatch, tmp_path, platform_events, capsys):
    objects = _stored_run(tmp_path, platform_events)
    _boto(monkeypatch, _VersionedS3(objects, markers=[sorted(objects)[0]]))
    capsys.readouterr()
    assert main(["verify", "s3://bucket/audit/plan-demo/run-demo-1"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "delete_marker:1"


def test_demo_retention_defaults_to_one_day(monkeypatch, tmp_path, platform_events):
    import inspect

    from bravo_audit import cli
    from bravo_audit.sink import FileSink, ObjectLockSink

    assert inspect.signature(ObjectLockSink.__init__).parameters["retention_days"].default == 1
    seen = []
    monkeypatch.setattr(cli, "_sink", lambda target, days: seen.append(days) or FileSink(tmp_path / "store"))
    assert main(["append", "--source", "platform.events", "--input", _write(tmp_path, platform_events),
                 "--sink", "s3://bucket/audit", "--receipts", str(tmp_path / "r.json")]) == 0
    assert seen == [1]


def _forwarding_event(platform_events, tool_events, **data):
    event = dict(platform_events[1], data=dict(platform_events[1]["data"], tool_events=tool_events, **data))
    return event


def test_forwarded_tool_events_keep_their_original_index(platform_events, tool_events):
    from bravo_audit.adapters import expand_platform_event

    rows = expand_platform_event(_forwarding_event(platform_events, tool_events))
    assert [r[2] for r in rows] == ["platform.events", "agent.tools", "agent.tools"]
    assert [r[4] for r in rows[1:]] == ["t11:run-demo-1:attempt1:0", "t11:run-demo-1:attempt1:1"]
    assert rows[2][3] == "DESTRUCTIVE_ATTEMPT_BLOCKED"
    # The platform record carries a count and digest, not a second copy of the array.
    assert "tool_events" not in rows[0][5]["data"] and rows[0][5]["data"]["tool_events_count"] == 2
    # Same origin ids as submitting the attempt's events directly, so either path dedupes the other.
    assert [r[4] for r in rows[1:]] == [from_agent_tool_event(e, "plan-demo", i)[4] for i, e in enumerate(tool_events)]


@pytest.mark.parametrize("change", [
    lambda d: d.update(tool_events_status="UNAVAILABLE"),
    lambda d: d.update(tool_events="not a list"),
    lambda d: d["tool_events"][0].update(run_id="run-other"),
])
def test_partial_or_foreign_forwarded_events_are_rejected(platform_events, tool_events, change):
    from bravo_audit.adapters import expand_platform_event

    event = _forwarding_event(platform_events, [dict(e) for e in tool_events])
    change(event["data"])
    with pytest.raises(ValueError):
        expand_platform_event(event)


def test_cli_absorbs_tool_events_seen_both_directly_and_forwarded(tmp_path, platform_events, tool_events, capsys):
    store = str(tmp_path / "store")
    assert main(["append", "--source", "platform.events", "--input", _write(tmp_path, platform_events[:1]),
                 "--sink", store, "--receipts", str(tmp_path / "r1.json")]) == 0
    run = str(tmp_path / "store/audit/plan-demo/run-demo-1")
    assert main(["append", "--source", "agent.tools", "--plan", "plan-demo", "--input", _write(tmp_path, tool_events, "t.jsonl"),
                 "--sink", store, "--receipts", str(tmp_path / "r2.json"), "--resume-from", run]) == 0
    forwarded = [_forwarding_event(platform_events, tool_events)]
    capsys.readouterr()
    assert main(["append", "--source", "platform.events", "--input", _write(tmp_path, forwarded, "f.jsonl"),
                 "--sink", store, "--receipts", str(tmp_path / "r3.json"), "--resume-from", run]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["records"] == 1 and out["absorbed_redeliveries"] == 2
    assert main(["verify", run]) == 0
