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
    monkeypatch.setattr(run, "warm", lambda context: requests.append({"warm": context}))

    def relay(context, request):
        requests.append(request)
        if request["op"] == "register":
            return {"status": "PASS", "registration": {"fingerprint": request["document"]["kind"],
                                                          "revision": 1, "recorded_at": "now"}}
        return {"status": "PASS", "probe": {"stage": request["stage"], "reviews": []}}

    monkeypatch.setattr(run, "relay", relay)
    result = run.prepare(SimpleNamespace(context="platform", plan_id="plan", command=command), documents)
    assert requests[0] == {"warm": "platform"}
    assert [request["op"] for request in requests[1:]] == ["register"] * len(names) + ["probe"]
    assert requests[-1] == {"op": "probe", "plan_id": "plan", "stage": stage}
    assert result["status"] == "PASS" and result["stage"] == stage


def test_warm_fails_before_any_registration(monkeypatch):
    monkeypatch.setattr(run.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr="unavailable"))
    with pytest.raises(SystemExit) as stopped:
        run.warm("platform")
    assert stopped.value.code == 1
