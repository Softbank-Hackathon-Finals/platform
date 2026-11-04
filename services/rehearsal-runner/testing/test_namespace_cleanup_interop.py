"""Exercise T15 cleanup delegation using T38's real UID-precondition code."""

import json

from bravo_agent.verification import Kube

from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle
from rehearsal_runner.runner import Namespace

NAME = "rehearsal-" + "a" * 24


def test_t15_cleanup_delegates_original_uid_and_checks_absence(monkeypatch):
    calls = []
    t38 = Kube()
    def call(*args, data=None):
        calls.append((args, data))
        return ""
    monkeypatch.setattr(t38, "call", call)
    lifecycle = NamespaceLifecycle(kube=None, cleaner=t38, profile={})
    assert lifecycle.cleanup(Namespace(NAME, "uid-original")) is True
    assert calls[0][1]["preconditions"] == {"uid": "uid-original"}
    assert calls[1][0] == ("get", "namespace", NAME, "--ignore-not-found", "-o", "json")


def test_t15_cleanup_never_claims_success_for_recreated_namespace(monkeypatch):
    t38 = Kube()
    replies = iter(["", json.dumps({"metadata": {"uid": "uid-replacement"}})])
    monkeypatch.setattr(t38, "call", lambda *args, data=None: next(replies))
    lifecycle = NamespaceLifecycle(kube=None, cleaner=t38, profile={})
    assert lifecycle.cleanup(Namespace(NAME, "uid-original")) is False
