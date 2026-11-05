"""Restart-safe coordinator state: what T10 persists and what a fresh process may continue."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_coordinator import TestProvider, receipt
from test_openai import install_client, response

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


def test_bravo_agent_cli_writes_a_restorable_state(context, tmp_path):
    bundle, snapshot, proposal, _ = context
    (tmp_path / "bundle.json").write_text(bundle.model_dump_json())
    (tmp_path / "manifest.json").write_text(snapshot.manifest.model_dump_json())
    command = cli() + ["analyze", "--bundle", str(tmp_path / "bundle.json"),
                       "--manifest", str(tmp_path / "manifest.json"), "--snapshot", str(snapshot.root),
                       "--output", str(tmp_path / "result.json"), "--state-output", str(tmp_path / "state.json"),
                       "--model", "explicit-test-model"]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"ACCESS_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}
    run = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert run.returncode == 2 and "state=NEEDS_HUMAN" in run.stdout
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["state"] == "NEEDS_HUMAN" and state["attempt"] == 1 and state["revision"] == 2
    assert not (tmp_path / "state.json.tmp").exists()
    restored = Coordinator.restore(state, snapshot)
    assert restored.result()["audit"] == json.loads((tmp_path / "result.json").read_text())["audit"]
    with pytest.raises(ValueError):
        asyncio.run(restored.propose(TestProvider(proposal)))
    # A second run never starts over on top of an existing state file.
    (tmp_path / "result.json").unlink()
    saved = (tmp_path / "state.json").read_bytes()
    again = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert again.returncode == 2 and (tmp_path / "state.json").read_bytes() == saved
    assert not (tmp_path / "result.json").exists()
