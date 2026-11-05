import json

import pytest

from bravo_audit.adapters import from_agent_tool_event, from_platform_event
from bravo_audit.cli import main


def test_platform_events_keep_type_as_kind(platform_events):
    plan, run, source, kind, payload = from_platform_event(platform_events[1])
    assert (plan, run, source, kind) == ("plan-demo", "run-demo-1", "platform.events", "gate.completed")
    assert payload["data"]["decision"] == "BLOCK" and "plan_id" not in payload
    platform_events[0]["event_id"] = "plan-demo:9"
    with pytest.raises(ValueError):
        from_platform_event(platform_events[0])
    platform_events[1]["event_type"] = "model.said"
    with pytest.raises(ValueError):
        from_platform_event(platform_events[1])


def test_tool_events_expose_reason_as_kind_for_the_demo_scene(tool_events):
    plan, run, source, kind, payload = from_agent_tool_event(tool_events[1], "plan-demo")
    assert (source, kind) == ("agent.tools", "DESTRUCTIVE_ATTEMPT_BLOCKED")
    assert payload == {"attempt": 1, "tool": "rehearsal.exec", "outcome": "DENIED",
                       "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}
    tool_events[0]["outcome"] = "MAYBE"
    with pytest.raises(ValueError):
        from_agent_tool_event(tool_events[0], "plan-demo")


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
    # a second append of the same sequence must not overwrite history
    assert main(["append", "--source", "agent.tools", "--plan", "plan-demo", "--input", str(tools),
                 "--sink", str(tmp_path / "store"), "--receipts", str(tmp_path / "r3.json"),
                 "--resume-from", str(resume)]) == 3


def test_cli_rejects_events_of_another_run(tmp_path, platform_events):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps(platform_events[0]), encoding="utf-8")
    assert main(["append", "--source", "platform.events", "--run", "run-other", "--input", str(events),
                 "--sink", str(tmp_path / "store"), "--receipts", str(tmp_path / "r.json")]) == 2
