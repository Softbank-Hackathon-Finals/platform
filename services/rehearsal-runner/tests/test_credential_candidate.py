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
