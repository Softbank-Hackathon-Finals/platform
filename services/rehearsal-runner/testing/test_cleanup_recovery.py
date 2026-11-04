"""T15 boundary checks against T38 cleanup and T11 in-memory coordinator.

All Kubernetes calls are mocked. This does not implement T10 recovery.
"""

import json

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot
from bravo_agent.verification import Kube


class FakeApi:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, *args, data=None):
        self.calls.append((args, data))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def namespace(uid):
    return json.dumps({"metadata": {"uid": uid}})


def test_real_cleanup_waits_until_original_namespace_is_absent(monkeypatch):
    api = FakeApi(["", namespace("uid-original"), ""])
    kube = Kube()
    monkeypatch.setattr(kube, "call", api)
    monkeypatch.setattr("bravo_agent.verification.time.sleep", lambda _: None)
    assert kube.cleanup("rehearsal-test", "uid-original") is True
    assert api.calls[0][1]["preconditions"] == {"uid": "uid-original"}
    assert len(api.calls) == 3


def test_real_cleanup_rejects_recreated_namespace(monkeypatch):
    api = FakeApi(["", namespace("uid-new")])
    kube = Kube()
    monkeypatch.setattr(kube, "call", api)
    assert kube.cleanup("rehearsal-test", "uid-original") is False
    assert api.calls[0][1]["preconditions"] == {"uid": "uid-original"}
    assert len(api.calls) == 2


def test_real_cleanup_timeout_does_not_claim_absence(monkeypatch):
    api = FakeApi([""] + [namespace("uid-original")] * 60)
    kube = Kube()
    monkeypatch.setattr(kube, "call", api)
    monkeypatch.setattr("bravo_agent.verification.time.sleep", lambda _: None)
    assert kube.cleanup("rehearsal-test", "uid-original") is False


@pytest.mark.parametrize("responses", [[RuntimeError("delete failed")],
                                        ["", RuntimeError("read unavailable")]])
def test_real_cleanup_error_is_not_success(monkeypatch, responses):
    kube = Kube()
    monkeypatch.setattr(kube, "call", FakeApi(responses))
    with pytest.raises(RuntimeError):
        kube.cleanup("rehearsal-test", "uid-original")


def test_new_coordinator_does_not_recover_attempt_history(tmp_path):
    source_sha = "1" * 40
    manifest = SnapshotManifest(source_sha=source_sha, files={"src/config.py": "a" * 64},
                                editable_paths=["src/config.py"])
    bundle = FailureBundle(run_id="run-1", source_sha=source_sha,
                           failure_code="TEST_FAILED", summary="A test failed", evidence_digest="b" * 64)
    # Creating a new Coordinator resets the counter; T10 must restore or fence the run.
    restarted = Coordinator(bundle, Snapshot(tmp_path, manifest))
    assert restarted.result()["attempt"] == 0
    assert restarted.result()["state"] == "READY"
    assert restarted.result()["attempts"] == []
