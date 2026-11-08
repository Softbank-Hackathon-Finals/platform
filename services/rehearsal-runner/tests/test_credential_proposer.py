from types import SimpleNamespace

import pytest

from rehearsal_runner.credential_proposer import T11CredentialProposer, credential_summary
from rehearsal_runner.secret_task import prepare_credential_migration


class Snapshot:
    def __init__(self, blocked=False):
        self.paths = []
        self.blocked = blocked

    def read(self, path):
        self.paths.append(path)
        if self.blocked:
            raise ValueError("T11 policy blocked path")
        return "source content"


class Coordinator:
    def __init__(self, task, paths=None, blocked=False):
        self.bundle = SimpleNamespace(run_id=task.run_id, source_sha=task.source_sha,
                                      summary=credential_summary(task))
        self.state, self.attempt = "READY", 0
        self.snapshot = Snapshot(blocked)
        self.calls = 0
        self.paths = paths if paths is not None else ["app/db_secret.py", "app/main.py"]

    async def propose(self, provider):
        self.calls += 1
        return {"state": "PROPOSAL_READY", "attempt": 1, "run_id": self.bundle.run_id,
                "source_sha": self.bundle.source_sha, "proposal_digest": "b" * 64,
                "proposal": {"edits": [{"path": path} for path in self.paths]}}


def task():
    return prepare_credential_migration("run-1", "a" * 40)


def test_forwards_bounded_task_to_coordinator():
    t = task()
    c = Coordinator(t)
    proposal = T11CredentialProposer(c, object()).propose(t)
    assert proposal.proposal_digest == "b" * 64
    assert c.snapshot.paths == ["app/db_secret.py", "app/config.py", "app/main.py"]
    assert c.calls == 1


@pytest.mark.parametrize("extra, ok", [
    ("\nInventory finding: openbao+eso needs injected credentials", True),  # T10 initial_model_input
    ("\nInventory finding: one\ntwo", False), ("\nSomething else", False), (" trailing", False)])
def test_initial_summary_allows_only_the_inventory_line(extra, ok):
    t = task()
    c = Coordinator(t)
    c.bundle.summary += extra
    if ok:
        assert T11CredentialProposer(c, object()).propose(t).proposal_digest == "b" * 64
    else:
        with pytest.raises(ValueError, match="invalid initial proposal context"):
            T11CredentialProposer(c, object()).propose(t)
        assert c.calls == 0


def test_blocked_t11_path_never_calls_model():
    t = task()
    c = Coordinator(t, blocked=True)
    with pytest.raises(ValueError):
        T11CredentialProposer(c, object()).propose(t)
    assert c.calls == 0


@pytest.mark.parametrize("paths", [[], ["app/main.py"], ["app/db_secret.py", "other.py"],
                                   ["app/db_secret.py", "app/db_secret.py"]])
def test_rejects_missing_source_or_out_of_scope_edits(paths):
    t = task()
    with pytest.raises(ValueError):
        T11CredentialProposer(Coordinator(t, paths), object()).propose(t)


def test_rejects_wrong_source_revision_before_snapshot_or_model():
    t = task()
    c = Coordinator(t)
    c.bundle.source_sha = "c" * 40
    with pytest.raises(ValueError):
        T11CredentialProposer(c, object()).propose(t)
    assert c.snapshot.paths == [] and c.calls == 0


def test_retry_rejects_unverified_feedback_before_snapshot_or_model():
    t = task()
    c = Coordinator(t)
    c.state, c.attempt = "RETRY_ALLOWED", 1
    c.bundle.evidence_digest = "a" * 64
    with pytest.raises(ValueError, match="feedback mismatch"):
        T11CredentialProposer(c, object(), attempt=2, feedback_digest="b" * 64).propose(t)
    assert c.snapshot.paths == [] and c.calls == 0
