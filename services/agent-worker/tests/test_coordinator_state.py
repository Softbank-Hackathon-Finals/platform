"""Restart-safe coordinator state: what T10 persists and what a fresh process may continue."""

import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_coordinator import TestProvider, receipt
from test_openai import install_client, response

from bravo_agent.cli import write_state
from bravo_agent.contracts import InventorySummary, SnapshotManifest, digest
from bravo_agent.coordinator import STATE_FIELDS, Coordinator, StateRejected
from bravo_agent.openai_provider import OpenAIProvider
from bravo_agent.tools import Snapshot


def stored(coordinator):
    """What a store hands back after a restart: plain JSON, no shared objects."""
    return json.loads(json.dumps(coordinator.export_state()))


def resign(state):
    body = {key: value for key, value in state.items() if key != "state_digest"}
    return body | {"state_digest": digest(body)}


class CountingProvider(TestProvider):
    __test__ = False
    calls = 0

    async def propose(self, bundle, tools):
        CountingProvider.calls += 1
        await super().propose(bundle, tools)


def test_state_round_trip_preserves_every_decision_input(context):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    state = stored(coordinator)
    assert set(state) == STATE_FIELDS
    assert state["schema_version"] == 1 and state["attempt"] == 1 and state["state"] == "PROPOSAL_READY"
    restored = Coordinator.restore(state, snapshot)
    assert restored.export_state() == state
    assert restored.result() == coordinator.result()
    # The restored copy does not alias the stored document.
    restored.audit.append({"run_id": "run-1", "attempt": 1})
    assert len(state["audit"]) == len(coordinator.audit)


def test_fresh_state_is_restorable_and_inventory_is_bound(context):
    bundle, snapshot, _, _ = context
    inventory = InventorySummary(schema_version="1.0", app_id="demo-chat", source_sha=bundle.source_sha,
                                 digest="sha256:" + "c" * 64, items=[])
    state = stored(Coordinator(bundle, snapshot, inventory))
    assert Coordinator.restore(state, snapshot, inventory).state == "READY"
    with pytest.raises(StateRejected):
        Coordinator.restore(state, snapshot)
    other = inventory.model_copy(update={"digest": "sha256:" + "d" * 64})
    with pytest.raises(StateRejected):
        Coordinator.restore(state, snapshot, other)


@pytest.mark.parametrize("tamper", [
    lambda s: s.update(attempt=0),
    lambda s: s.update(state="RETRY_ALLOWED"),
    lambda s: s["audit"].pop(),
    lambda s: s["proposal"].update(rationale="A different proposal than the one recorded"),
    lambda s: s.update(extra=True),
    lambda s: s.pop("revision"),
])
def test_tampered_state_is_rejected(context, tamper):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    state = stored(coordinator)
    tamper(state)
    with pytest.raises(StateRejected):
        Coordinator.restore(state, snapshot)


@pytest.mark.parametrize("tamper", [
    # Each keeps a valid digest: the content rules must hold on their own.
    lambda s: s.update(attempt=1),                         # fewer attempts than records
    lambda s: s.update(state="READY"),                     # forget that two attempts were spent
    lambda s: s.update(schema_version=2),
    lambda s: s.update(run_id="run-2"),
    lambda s: s["attempts"].pop(),
    lambda s: s["attempts"][0].pop("verification"),        # an unverified earlier attempt
    lambda s: s["attempts"][1]["verification"].update(attempt=1),
    lambda s: s["task"].update(summary="A different objective for the same run"),
    lambda s: s["audit"].append({"run_id": "run-1", "attempt": 3, "tool": "model"}),
])
def test_resigned_state_still_has_to_match_its_records(context, tamper):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    for _ in range(2):
        asyncio.run(coordinator.propose(TestProvider(proposal)))
        coordinator.verify(receipt(coordinator, verdict="FAIL"))
    state = stored(coordinator)
    assert Coordinator.restore(state, snapshot).state == "RETRY_ALLOWED"
    tamper(state)
    with pytest.raises(StateRejected):
        Coordinator.restore(resign(state), snapshot)


def failed_twice(context):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    for _ in range(2):
        asyncio.run(coordinator.propose(TestProvider(proposal)))
        coordinator.verify(receipt(coordinator, verdict="FAIL"))
    return coordinator


def test_a_stopped_attempt_cannot_be_resigned_into_a_retry(context):
    """The recorded receipt decides: effects unknown means NEEDS_HUMAN, whatever the state says."""
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    assert coordinator.verify(receipt(coordinator, verdict="FAIL", effects_known=False)) == "NEEDS_HUMAN"
    state = stored(coordinator)
    state.update(state="RETRY_ALLOWED", bundle=state["attempts"][-1]["verification"]["failure"])
    CountingProvider.calls = 0
    with pytest.raises(StateRejected):
        asyncio.run(Coordinator.restore(resign(state), snapshot).propose(CountingProvider(proposal)))
    assert CountingProvider.calls == 0


@pytest.mark.parametrize("change", [
    {"proposal_digest": "b" * 64},                        # a receipt for another proposal
    {"source_sha": "2" * 40},                             # or another revision
    {"evidence_digest": "c" * 64},                        # failure evidence that is not this receipt's
    {"effects_known": False},
    {"verdict": "UNKNOWN", "failure": None},
])
@pytest.mark.parametrize("position", [0, 1])
def test_every_recorded_receipt_is_rejudged_on_restore(context, change, position):
    state = stored(failed_twice(context))
    assert Coordinator.restore(state, context[1]).state == "RETRY_ALLOWED"
    state["attempts"][position]["verification"].update(change)
    with pytest.raises(StateRejected):
        Coordinator.restore(resign(state), context[1])


@pytest.mark.parametrize("change", [{"attempt": 2}, {"proposal_digest": "b" * 64}, {"source_sha": "2" * 40}])
def test_a_mismatched_receipt_stops_the_run_and_the_stopped_state_restores(context, change):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    assert coordinator.verify(receipt(coordinator, **change)) == "NEEDS_HUMAN"
    # Only the reason is kept; the receipt is not this attempt's result.
    assert "verification" not in coordinator.attempts[-1]
    assert coordinator.audit[-1]["reason"] == "RECEIPT_MISMATCH"
    restored = Coordinator.restore(stored(coordinator), snapshot)
    assert restored.state == "NEEDS_HUMAN" and restored.export_state() == stored(coordinator)


def test_states_from_the_normal_path_always_restore(context):
    bundle, snapshot, proposal, _ = context
    saved, failing = [], {"revision": None}

    def store(state):
        if state["revision"] == failing["revision"]:
            raise OSError("store unavailable")
        saved.append(state)

    # (verdict, revision whose checkpoint fails): 1 before the model call, 2 after it, 3 after the receipt.
    for verdict, failing["revision"] in [("PASS", None), ("FAIL", None), ("FAIL", 3), ("PASS", 3),
                                         ("FAIL", 2), ("FAIL", 1)]:
        coordinator = Coordinator(bundle, snapshot, checkpoint=store)
        asyncio.run(coordinator.propose(TestProvider(proposal)))
        if coordinator.state == "PROPOSAL_READY":
            coordinator.verify(receipt(coordinator, verdict=verdict))
        saved.append(coordinator.export_state())
    for state in saved:
        Coordinator.restore(json.loads(json.dumps(state)), snapshot)


@pytest.mark.parametrize("value", [True, 1.0])
def test_attempt_numbers_in_records_are_integers(context, value):
    state = stored(failed_twice(context))
    state["attempts"][0]["attempt"] = value
    with pytest.raises(StateRejected):
        Coordinator.restore(resign(state), context[1])


def test_deeply_nested_state_is_rejected_not_a_crash(context):
    state = stored(Coordinator(*context[:2]))
    nested = []
    for _ in range(200_000):
        nested = [nested]
    state["audit"] = nested
    with pytest.raises(StateRejected):
        Coordinator.restore(state, context[1])


def test_an_earlier_attempt_must_have_ended_in_a_retry(context):
    state = stored(failed_twice(context))
    del state["attempts"][0]["verification"]
    state["attempts"][0]["state"] = "NEEDS_HUMAN"
    with pytest.raises(StateRejected):
        Coordinator.restore(resign(state), context[1])


@pytest.mark.parametrize("stage", ["READY", "ANALYZING", "RETRY_ALLOWED", "record"])
def test_the_store_cannot_swap_the_model_input(context, stage):
    bundle, snapshot, proposal, _ = context
    checkpoints = []
    coordinator = Coordinator(bundle, snapshot, checkpoint=checkpoints.append)
    if stage != "READY":
        asyncio.run(coordinator.propose(TestProvider(proposal)))
    if stage in {"RETRY_ALLOWED", "record"}:
        coordinator.verify(receipt(coordinator, verdict="FAIL"))
    state = checkpoints[0] if stage == "ANALYZING" else stored(coordinator)
    swapped = state["attempts"][0]["input"] if stage == "record" else state["bundle"]
    swapped["summary"] = "A model input the store substituted for the recorded one"
    if stage == "record":
        state["attempts"][0]["input_digest"] = digest(swapped)
    with pytest.raises(StateRejected):
        Coordinator.restore(resign(state), snapshot)


def tool_events(coordinator, attempt):
    """T15's reader of one attempt (rehearsal_runner.bucket_proposer.attempt_tool_events)."""
    events = [e for e in coordinator.audit if e["attempt"] == attempt and e["tool"] != "independent_verifier"]
    assert len(events) == coordinator.attempts[attempt - 1]["audit_event_count"], "tool events incomplete"
    return events


@pytest.mark.parametrize("failing_revision", [1, 2, 3])
def test_coordinator_stops_keep_the_attempt_event_count(context, failing_revision):
    bundle, snapshot, proposal, _ = context

    def store(state):
        if state["revision"] == failing_revision:
            raise OSError("store unavailable")

    coordinator = Coordinator(bundle, snapshot, checkpoint=store)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    if coordinator.state == "PROPOSAL_READY":
        coordinator.verify(receipt(coordinator, verdict="FAIL"))
    assert coordinator.state == "NEEDS_HUMAN"
    assert tool_events(coordinator, 1)[-1]["reason"] == "STATE_CHECKPOINT_FAILED"


def test_state_is_bound_to_the_same_snapshot(context, tmp_path):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    changed = SnapshotManifest.model_validate(snapshot.manifest.model_dump() | {"editable_paths": ["src/config.py"],
                                              "files": snapshot.manifest.files | {"src/other.py": "f" * 64}})
    with pytest.raises(StateRejected):
        Coordinator.restore(stored(coordinator), Snapshot(snapshot.root, changed))


@pytest.mark.parametrize("failed_attempts", [0, 2])
def test_state_captured_while_analyzing_restores_as_needs_human(context, failed_attempts):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    for _ in range(failed_attempts):
        asyncio.run(coordinator.propose(TestProvider(proposal)))
        coordinator.verify(receipt(coordinator, verdict="FAIL"))
    checkpoints = []
    coordinator.checkpoint = checkpoints.append
    CountingProvider.calls = 0
    asyncio.run(coordinator.propose(CountingProvider(proposal)))
    # Write-ahead: the in-flight state was stored before the provider ran.
    in_flight = checkpoints[0]
    assert in_flight["state"] == "ANALYZING" and in_flight["attempt"] == failed_attempts + 1
    assert len(in_flight["attempts"]) == failed_attempts

    restored = Coordinator.restore(json.loads(json.dumps(in_flight)), snapshot)
    assert restored.state == "NEEDS_HUMAN" and restored.attempt == failed_attempts + 1
    assert restored.attempts[-1]["state"] == "NEEDS_HUMAN" and restored.attempts[-1]["audit_truncated"] is True
    assert restored.audit[-1]["reason"] == "RESTART_OUTCOME_UNKNOWN"
    with pytest.raises(ValueError):
        asyncio.run(restored.propose(CountingProvider(proposal)))
    assert CountingProvider.calls == 1
    # The converted state itself is a valid stored state and stays stopped.
    again = Coordinator.restore(stored(restored), snapshot)
    assert again.state == "NEEDS_HUMAN" and len(again.attempts) == failed_attempts + 1


def test_checkpoint_failure_before_the_model_call_does_not_call_the_model(context):
    bundle, snapshot, proposal, _ = context

    def unavailable(_state):
        raise OSError("store unavailable")

    CountingProvider.calls = 0
    coordinator = Coordinator(bundle, snapshot, checkpoint=unavailable)
    result = asyncio.run(coordinator.propose(CountingProvider(proposal)))
    assert CountingProvider.calls == 0
    assert result["state"] == "NEEDS_HUMAN" and result["audit"][-1]["reason"] == "STATE_CHECKPOINT_FAILED"


def test_attempt_cap_holds_across_restarts(context):
    bundle, snapshot, proposal, _ = context
    state = stored(Coordinator(bundle, snapshot))
    CountingProvider.calls = 0
    for attempt in range(1, 4):
        coordinator = Coordinator.restore(state, snapshot)  # a fresh process every attempt
        result = asyncio.run(coordinator.propose(CountingProvider(proposal)))
        assert result["attempt"] == attempt
        state = stored(coordinator)
        coordinator = Coordinator.restore(state, snapshot)  # and again before the receipt arrives
        assert coordinator.verify(receipt(coordinator, verdict="FAIL")) == (
            "RETRY_ALLOWED" if attempt < 3 else "NEEDS_HUMAN")
        state = stored(coordinator)
    final = Coordinator.restore(state, snapshot)
    with pytest.raises(ValueError):
        asyncio.run(final.propose(CountingProvider(proposal)))
    assert CountingProvider.calls == 3 and len(final.attempts) == 3
    # Even a re-signed document cannot open a fourth attempt.
    with pytest.raises(StateRejected):
        Coordinator.restore(resign(state | {"state": "RETRY_ALLOWED"}), snapshot)


def test_verify_after_restore_keeps_the_proposal_and_attempt_binding(context):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    coordinator.verify(receipt(coordinator, verdict="FAIL"))
    asyncio.run(Coordinator.restore(stored(coordinator), snapshot).propose(TestProvider(proposal)))
    retry = Coordinator.restore(stored(coordinator), snapshot)
    asyncio.run(retry.propose(TestProvider(proposal)))
    state = stored(retry)
    for changes in ({"attempt": 1}, {"proposal_digest": "b" * 64}, {"run_id": "other"}):
        assert Coordinator.restore(state, snapshot).verify(receipt(retry, **changes)) == "NEEDS_HUMAN"
    assert Coordinator.restore(state, snapshot).verify(receipt(retry)) == "AWAITING_APPROVAL"


def test_fail_retry_pass_across_restart_with_the_openai_path(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    stored_states = []
    first = Coordinator(bundle, snapshot, checkpoint=stored_states.append)
    install_client(monkeypatch, [response("repo_read", {"path": "src/config.py"}),
                                 response("plan_propose", proposal)])
    assert asyncio.run(first.propose(OpenAIProvider("fixture-model")))["state"] == "PROPOSAL_READY"
    assert first.verify(receipt(first, verdict="FAIL")) == "RETRY_ALLOWED"
    assert [state["state"] for state in stored_states] == ["ANALYZING", "PROPOSAL_READY", "RETRY_ALLOWED"]
    assert [state["revision"] for state in stored_states] == [1, 2, 3]
    del first  # the runner process ends here

    second = Coordinator.restore(json.loads(json.dumps(stored_states[-1])), snapshot,
                                 checkpoint=stored_states.append)
    calls, _ = install_client(monkeypatch, [response("repo_read", {"path": "src/config.py"}),
                                            response("plan_propose", proposal)])
    result = asyncio.run(second.propose(OpenAIProvider("fixture-model")))
    model_input = json.loads(calls[0]["input"][0]["content"])
    assert model_input["attempt"] == 2 and model_input["task"] == bundle.model_dump()
    assert model_input["failure"]["summary"].startswith("Independent validator")
    assert result["state"] == "PROPOSAL_READY" and result["attempt"] == 2
    assert second.verify(receipt(second)) == "AWAITING_APPROVAL"
    final = Coordinator.restore(stored_states[-1], snapshot)
    assert final.state == "AWAITING_APPROVAL" and stored_states[-1]["revision"] == 6
    assert [record["attempt"] for record in final.attempts] == [1, 2]
    assert final.result()["task_digest"] == digest(bundle)


RESTART_SCRIPT = """
import asyncio, json, sys
from pathlib import Path
from bravo_agent.contracts import SnapshotManifest, VerificationReceipt
from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot

state_path, root, manifest_path, proposal_path = map(Path, sys.argv[1:5])
proposal = json.loads(proposal_path.read_text())

class Fake:
    async def propose(self, bundle, tools):
        tools.dispatch("mcp__bravo__repo_read", {"path": "src/config.py"})
        tools.dispatch("mcp__bravo__plan_propose", proposal)

snapshot = Snapshot(root, SnapshotManifest.model_validate_json(manifest_path.read_text()))
coordinator = Coordinator.restore(json.loads(state_path.read_text()), snapshot)
asyncio.run(coordinator.propose(Fake()))
result = coordinator.result()
state = coordinator.verify(VerificationReceipt(
    run_id=result["run_id"], source_sha=result["source_sha"], proposal_digest=result["proposal_digest"],
    attempt=coordinator.attempt, verdict="PASS", effects_known=True, evidence_digest="e" * 64))
print(json.dumps({"attempt": coordinator.attempt, "state": state}))
"""


def test_a_separate_process_continues_the_retry_from_the_stored_state(context, tmp_path):
    bundle, snapshot, proposal, _ = context
    coordinator = Coordinator(bundle, snapshot)
    asyncio.run(coordinator.propose(TestProvider(proposal)))
    coordinator.verify(receipt(coordinator, verdict="FAIL"))
    paths = {name: tmp_path / f"{name}.json" for name in ("state", "manifest", "proposal")}
    paths["state"].write_text(json.dumps(coordinator.export_state()))
    paths["manifest"].write_text(snapshot.manifest.model_dump_json())
    paths["proposal"].write_text(json.dumps(proposal))
    run = subprocess.run([sys.executable, "-c", RESTART_SCRIPT, str(paths["state"]), str(snapshot.root),
                          str(paths["manifest"]), str(paths["proposal"])],
                         capture_output=True, text=True, check=True)
    assert json.loads(run.stdout) == {"attempt": 2, "state": "AWAITING_APPROVAL"}


def cli():
    script = Path(sys.executable).parent / "bravo-agent"
    return [str(script)] if script.exists() else [sys.executable, "-m", "bravo_agent.cli"]


def analyze(context, tmp_path):
    bundle, snapshot, _, _ = context
    (tmp_path / "bundle.json").write_text(bundle.model_dump_json())
    (tmp_path / "manifest.json").write_text(snapshot.manifest.model_dump_json())
    command = cli() + ["analyze", "--bundle", str(tmp_path / "bundle.json"),
                       "--manifest", str(tmp_path / "manifest.json"), "--snapshot", str(snapshot.root),
                       "--output", str(tmp_path / "result.json"), "--state-output", str(tmp_path / "state.json"),
                       "--model", "explicit-test-model"]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"ACCESS_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}
    return lambda: subprocess.run(command, env=environment, capture_output=True, text=True, check=False)


def temporaries(directory):
    return [path.name for path in directory.iterdir() if path.name.endswith(".tmp")]


def test_bravo_agent_cli_writes_a_restorable_state(context, tmp_path):
    _, snapshot, proposal, _ = context
    command = analyze(context, tmp_path)
    run = command()
    assert run.returncode == 2 and "state=NEEDS_HUMAN" in run.stdout
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["state"] == "NEEDS_HUMAN" and state["attempt"] == 1 and state["revision"] == 2
    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600 and temporaries(tmp_path) == []
    restored = Coordinator.restore(state, snapshot)
    assert restored.result()["audit"] == json.loads((tmp_path / "result.json").read_text())["audit"]
    with pytest.raises(ValueError):
        asyncio.run(restored.propose(TestProvider(proposal)))
    # A second run never starts over on top of an existing state file.
    (tmp_path / "result.json").unlink()
    saved = (tmp_path / "state.json").read_bytes()
    again = command()
    assert again.returncode == 2 and (tmp_path / "state.json").read_bytes() == saved
    assert not (tmp_path / "result.json").exists()


def test_cli_refuses_a_dangling_state_symlink(context, tmp_path):
    (tmp_path / "state.json").symlink_to(tmp_path / "elsewhere.json")
    run = analyze(context, tmp_path)()
    assert run.returncode == 2 and "state=" not in run.stdout
    assert not (tmp_path / "elsewhere.json").exists() and not (tmp_path / "result.json").exists()


def test_a_leftover_temporary_file_does_not_wedge_later_runs(context, tmp_path):
    (tmp_path / "state.json.tmp").write_text("left by an earlier crash")
    run = analyze(context, tmp_path)()
    assert run.returncode == 2 and "state=NEEDS_HUMAN" in run.stdout
    assert json.loads((tmp_path / "state.json").read_text())["revision"] == 2
    assert temporaries(tmp_path) == ["state.json.tmp"]


def test_the_first_state_write_never_replaces_an_existing_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("created by someone else")
    with pytest.raises(FileExistsError):
        write_state(path, {"revision": 1}, replace=False)
    assert path.read_text() == "created by someone else" and temporaries(tmp_path) == []
    write_state(path, {"revision": 2}, replace=True)
    assert json.loads(path.read_text()) == {"revision": 2} and temporaries(tmp_path) == []
