"""Demo preparation warms both Deployments, registers current contracts, then probes."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("security_program_run",
    Path(__file__).resolve().parents[3] / "operations/security-program/run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


@pytest.mark.parametrize("command,names,stage", [
    ("prepare-change", ("patch-preflight",), "change"),
    ("prepare-cutover", ("patch-postflight", "migration-preflight"), "cutover"),
])
def test_prepare_registers_before_the_live_probe(tmp_path, monkeypatch, command, names, stage):
    documents = []
    for name in names:
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps({"kind": name}))
        documents.append((name, path, 0))
    requests = []
    monkeypatch.setattr(run, "warm", lambda context, **_kwargs: requests.append({"warm": context}))

    def relay(context, request, **_kwargs):
        requests.append(request)
        if request["op"] == "register":
            return {"status": "PASS", "registration": {"fingerprint": request["document"]["kind"],
                                                          "revision": 1, "recorded_at": "now"}}
        return {"status": "PASS", "probe": {"stage": request["stage"], "reviews": []}}

    monkeypatch.setattr(run, "relay", relay)
    result = run.prepare(SimpleNamespace(context="platform", plan_id="plan", command=command,
                                         budget_seconds=270), documents)
    assert requests[0] == {"warm": "platform"}
    assert [request["op"] for request in requests[1:]] == ["register"] * len(names) + ["probe"]
    assert requests[-1] == {"op": "probe", "plan_id": "plan", "stage": stage}
    assert result["status"] == "PASS" and result["stage"] == stage


def test_warm_fails_before_any_registration(monkeypatch):
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr="unavailable"))
    with pytest.raises(SystemExit) as stopped:
        run.warm("platform", deadline=10, clock=lambda: 0)
    assert stopped.value.code == 1


def test_budget_is_shared_by_warm_registration_and_probe(tmp_path, monkeypatch):
    document = tmp_path / "patch.json"
    document.write_text(json.dumps({"kind": "patch-preflight"}))
    ticks = iter((0, 0, 4))

    def clock():
        return next(ticks)

    seen = []
    monkeypatch.setattr(run, "warm", lambda context, **kwargs: seen.append(("warm", kwargs["deadline"])))

    def relay(context, request, **kwargs):
        seen.append((request["op"], kwargs["deadline"]))
        if request["op"] == "register":
            return {"status": "PASS", "registration": {"revision": 1}}
        return {"status": "PASS", "probe": {"stage": "change", "reviews": []}}

    monkeypatch.setattr(run, "relay", relay)
    answer = run.prepare(SimpleNamespace(context="platform", plan_id="plan", command="prepare-change",
                                         budget_seconds=45),
                         (("patch-preflight", document, 0),), clock=clock)
    assert {deadline for _, deadline in seen} == {45}
    assert answer["budget_seconds"] == 45 and answer["elapsed_ms"] == 4000


def test_relay_timeout_is_fail_closed(monkeypatch):
    def expired(*_args, **_kwargs):
        raise run.subprocess.TimeoutExpired("kubectl", 1)

    monkeypatch.setattr(run.subprocess, "run", expired)
    assert run.relay("platform", {"op": "probe"}, deadline=1, clock=lambda: 0) == {
        "status": "FAIL", "reason": "security_relay_timeout"}
    assert run.relay("platform", {"op": "probe"}, deadline=0, clock=lambda: 0) == {
        "status": "FAIL", "reason": "demo_budget_exceeded"}
