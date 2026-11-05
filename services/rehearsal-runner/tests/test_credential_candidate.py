import hashlib

import pytest

from rehearsal_runner.credential_candidate import prepare_candidate_files
from rehearsal_runner.credential_proposal_source import ProposedEdit


class Snapshot:
    def __init__(self, value="source", denied=False):
        self.value, self.denied = value, denied
        self.calls = []

    def read(self, path):
        self.calls.append(path)
        if self.denied:
            raise ValueError("T11 path denied")
        return self.value


def edit(path="app/db_secret.py"):
    return ProposedEdit(path, hashlib.sha256(b"source").hexdigest(), "fixture replacement")


def test_candidate_uses_expected_original_and_hides_contents():
    files = prepare_candidate_files(Snapshot(), (edit(),))
    assert files[0].sha256 == hashlib.sha256(b"fixture replacement").hexdigest()
    assert "fixture replacement" not in repr(files)


def test_changed_source_stops_before_candidate_is_returned():
    with pytest.raises(ValueError, match="base mismatch"):
        prepare_candidate_files(Snapshot("changed"), (edit(),))


def test_t11_denial_is_not_bypassed():
    snapshot = Snapshot(denied=True)
    with pytest.raises(ValueError, match="T11 path denied"):
        prepare_candidate_files(snapshot, (edit(),))
    assert snapshot.calls == ["app/db_secret.py"]


@pytest.mark.parametrize("edits", [(edit("app/main.py"),), (edit(), edit()), (edit("other.py"),)])
def test_out_of_scope_edits_are_rejected_before_read(edits):
    snapshot = Snapshot()
    with pytest.raises(ValueError):
        prepare_candidate_files(snapshot, edits)
    assert snapshot.calls == []


def test_change_diff_is_applicable_and_bound_to_snapshot(tmp_path):
    import subprocess

    from rehearsal_runner.credential_candidate import prepare_candidate_change

    original = "first\r\nlast-without-newline"
    replacement = "first\r\nmodified-without-newline"
    snapshot = Snapshot(original)
    candidate = ProposedEdit("app/db_secret.py", hashlib.sha256(original.encode()).hexdigest(), replacement)
    change = prepare_candidate_change(snapshot, (candidate,))
    assert snapshot.calls == ["app/db_secret.py"]
    assert change.diff_hash == "sha256:" + hashlib.sha256(change.diff).hexdigest()
    assert "modified-without-newline" not in repr(change)
    (tmp_path / "app").mkdir()
    path = tmp_path / "app/db_secret.py"
    path.write_bytes(original.encode())
    subprocess.run(["git", "apply", "--check", "-"], cwd=tmp_path, input=change.diff, check=True)
    subprocess.run(["git", "apply", "-"], cwd=tmp_path, input=change.diff, check=True)
    assert path.read_bytes() == replacement.encode()


def test_noop_candidate_is_not_an_approval_diff():
    from rehearsal_runner.credential_candidate import prepare_candidate_change

    original = "source"
    candidate = ProposedEdit("app/db_secret.py", hashlib.sha256(original.encode()).hexdigest(), original)
    with pytest.raises(ValueError, match="no changes"):
        prepare_candidate_change(Snapshot(original), (candidate,))
