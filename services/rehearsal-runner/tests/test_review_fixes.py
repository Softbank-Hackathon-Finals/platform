import hashlib
import json
import signal
from types import SimpleNamespace

import pytest

from rehearsal_runner import isolation_cli, isolation_gate
from rehearsal_runner.bucket_probe import ReadResult, observe_bucket_reads
from rehearsal_runner.isolation.fixtures import Fixtures
from rehearsal_runner.isolation.kube import ProbeError


class ChunkStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def read(self, size):
        return next(self.chunks, b"")

    def close(self):
        self.closed = True


class Reader:
    def __init__(self, streams, status=200):
        self.streams = iter(streams)
        self.status = status

    def get(self, bucket, key):
        return ReadResult(self.status, next(self.streams, None))


def test_short_reads_cannot_hide_bytes_after_valid_prefix():
    first = ChunkStream([b"known", b"X"])
    result = observe_bucket_reads(Reader([first, ChunkStream([b"known"])]), Reader([], 403),
                                  bucket="b", key="k", expected_sha256=hashlib.sha256(b"known").hexdigest(),
                                  max_bytes=5)
    assert result.authenticated_before == "unknown"
    assert result.authenticated_after == "matches"
    assert first.closed


def test_multi_chunk_valid_object_requires_eof():
    result = observe_bucket_reads(Reader([ChunkStream([b"kn", b"own"]), ChunkStream([b"k", b"nown"])]),
                                  Reader([], 403), bucket="b", key="k",
                                  expected_sha256=hashlib.sha256(b"known").hexdigest(), max_bytes=5)
    assert result.authenticated_before == result.authenticated_after == "matches"


class FixtureKube:
    def __init__(self, creation=None):
        self.creation = creation or {"metadata": {"name": "isolation-victim-abc", "uid": "original"}}
        self.calls = []

    def apply(self, obj):
        self.calls.append(("apply", obj))

    def create(self, obj):
        self.calls.append(("create", obj))
        return self.creation

    def run(self, *args, stdin=None):
        self.calls.append((args, stdin))
        return SimpleNamespace(returncode=1)


def test_victim_cleanup_uses_creation_uid_and_never_deletes_by_name():
    kube = FixtureKube()
    fx = Fixtures("rehearsal-test", "abc", "image")
    fx.create(kube)
    fx.cleanup(kube)
    raw = [entry for entry in kube.calls if isinstance(entry[0], tuple) and "--raw" in entry[0]]
    assert len(raw) == 1
    assert json.loads(raw[0][1])["preconditions"] == {"uid": "original"}
    assert not any(isinstance(entry[0], tuple) and entry[0][:2] == ("delete", "namespace") for entry in kube.calls)


def test_unknown_creation_uid_never_deletes_victim_namespace():
    kube = FixtureKube({"metadata": {"name": "isolation-victim-abc"}})
    fx = Fixtures("rehearsal-test", "abc", "image")
    with pytest.raises(ProbeError):
        fx.create(kube)
    fx.cleanup(kube)
    assert not any(isinstance(entry[0], tuple) and "--raw" in entry[0] for entry in kube.calls)


@pytest.mark.parametrize("name,labels", [("default", {"app.kubernetes.io/part-of": "bravo-rehearsal"}),
                                         ("rehearsal-foreign", {}),
                                         ("rehearsal-foreign", {"app.kubernetes.io/part-of": "other"})])
def test_outside_namespace_or_missing_owner_causes_no_writes(monkeypatch, name, labels):
    calls = []
    class Kube:
        def get_json(self, *args):
            return {"metadata": {"uid": "original", "labels": labels}}
        def run(self, *args, **kwargs):
            calls.append(args)
    monkeypatch.setattr(isolation_gate, "run", lambda *_: pytest.fail("must not run"))
    with pytest.raises(isolation_gate.IsolationBlocked):
        isolation_gate.check_isolation(Kube(), name, [], "image")
    assert calls == []


def test_cli_sigterm_unwinds_fixture_cleanup(monkeypatch, tmp_path):
    calls, created = [], []
    profile = tmp_path / "profile.env"
    profile.write_text("unused")
    class Kube:
        def __init__(self, context):
            pass
        def apply(self, obj):
            created.append(obj)
        def create(self, obj):
            created.append(obj)
            return {"metadata": {"name": obj["metadata"]["name"], "uid": "victim-original"}}
        def get_json(self, *args):
            return {"metadata": {"uid": "original", "labels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}}}
        def run(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0)
    def interrupted(kube, fx, targets):
        fx.create(kube)
        signal.raise_signal(signal.SIGTERM)
    monkeypatch.setattr(isolation_cli, "Kubectl", Kube)
    monkeypatch.setattr(isolation_cli, "read_env", lambda _: {})
    monkeypatch.setattr(isolation_cli, "targets", lambda _: [])
    monkeypatch.setattr(isolation_gate, "run", interrupted)
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        isolation_cli.main(["--namespace", "rehearsal-test", "--profile", str(profile)])
    assert exc.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == previous
    assert len(created) == 5
    assert len(calls) == 2
    assert json.loads(calls[1][1]["stdin"])["preconditions"] == {"uid": "victim-original"}


def test_recreated_victim_is_not_deleted_by_uid_precondition():
    class Replaced(FixtureKube):
        def __init__(self):
            super().__init__()
            self.current_uid = "original"
        def run(self, *args, stdin=None):
            self.calls.append((args, stdin))
            if "--raw" in args:
                if json.loads(stdin)["preconditions"]["uid"] == self.current_uid:
                    self.current_uid = None
                    return SimpleNamespace(returncode=0)
                return SimpleNamespace(returncode=1)
            return SimpleNamespace(returncode=0)
    kube = Replaced()
    fx = Fixtures("rehearsal-test", "abc", "image")
    fx.create(kube)
    kube.current_uid = "replacement"
    fx.cleanup(kube)
    assert kube.current_uid == "replacement"


def test_cleanup_attempts_victim_even_if_pod_cleanup_errors():
    class BrokenPods(FixtureKube):
        def run(self, *args, stdin=None):
            self.calls.append((args, stdin))
            if "--raw" not in args:
                raise TimeoutError()
            return SimpleNamespace(returncode=0)
    kube = BrokenPods()
    fx = Fixtures("rehearsal-test", "abc", "image")
    fx.create(kube)
    with pytest.raises(TimeoutError):
        fx.cleanup(kube)
    assert json.loads(kube.calls[-1][1])["preconditions"] == {"uid": "original"}
