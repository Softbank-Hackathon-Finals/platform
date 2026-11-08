"""The platform-built candidate: an in-memory OCI registry, synthetic T10 inputs, real journals."""

import base64
import gzip
import hashlib
import io
import json
import tarfile

import pytest
from bravo_agent.contracts import Proposal, digest

from bravo_platform_events import candidate_build as candidate
from bravo_platform_events.initial_proposal import InitialInput, InitialIntent, InitialResult
from bravo_platform_events.store import EventError

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
REPOSITORY = "111111111111.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat"
SOURCE = "b" * 40


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    """Immutable tags, manifests by digest or tag, content-addressed blobs; records every write."""

    def __init__(self):
        self.blobs, self.manifests, self.tags, self.writes = {}, {}, {}, []
        self.fail = None

    def manifest(self, reference):
        reference = self.tags.get(reference, reference)
        if reference not in self.manifests:
            raise EventError("candidate_registry_read_failed")
        return self.manifests[reference]

    def put_blob(self, data):
        if self.fail == "blob":
            raise EventError("candidate_registry_write_failed")
        self.blobs[sha(data)] = data
        self.writes.append(("blob", sha(data)))
        return sha(data)

    def put_manifest(self, reference, data, media_type):
        if self.fail == "manifest":
            raise EventError("candidate_registry_write_failed")
        if not reference.startswith("sha256:"):
            if self.tags.get(reference, sha(data)) != sha(data):
                raise EventError("candidate_registry_write_failed")  # ECR: tags are immutable
            self.tags[reference] = sha(data)
        self.manifests[sha(data)] = (data, media_type)
        self.writes.append(("manifest", reference))
        return sha(data)


def seed(registry, tmp_path, *, source_sha=SOURCE, platforms=("linux/amd64", "linux/arm64"), copied=("app/",)):
    """A base index like the demo app's: two images and one build attestation, pinned with its configs."""
    entries, pins = [], {}
    for key in platforms:
        os_, arch = key.split("/")
        config = json.dumps({"architecture": arch, "os": os_, "config": {"User": "10001"},
                             "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]},
                             "history": [{"created_by": "COPY app ./app"}]}).encode()
        registry.blobs[sha(config)] = config
        manifest = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST,
                               "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                          "digest": sha(config), "size": len(config)},
                               "layers": [{"mediaType": LAYER, "digest": "sha256:" + "1" * 64, "size": 10}]}).encode()
        registry.manifests[sha(manifest)] = (manifest, MANIFEST)
        entries.append({"mediaType": MANIFEST, "digest": sha(manifest), "size": len(manifest),
                        "platform": {"architecture": arch, "os": os_}})
        pins[key] = {"manifest_digest": sha(manifest), "config": base64.b64encode(config).decode()}
    attestation = b'{"schemaVersion":2}'
    registry.manifests[sha(attestation)] = (attestation, MANIFEST)
    entries.append({"mediaType": MANIFEST, "digest": sha(attestation), "size": len(attestation),
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "annotations": {"vnd.docker.reference.type": "attestation-manifest"}})
    index = json.dumps({"schemaVersion": 2, "mediaType": INDEX, "manifests": entries,
                        "annotations": {"org.opencontainers.image.revision": source_sha}}).encode()
    registry.manifests[sha(index)] = (index, INDEX)
    path = tmp_path / "candidate-base.json"
    path.write_text(json.dumps({"repository": REPOSITORY, "source_sha": source_sha, "index_digest": sha(index),
                                "workdir": "/app", "copied": list(copied), "platforms": pins}))
    return path


def inputs(*, path="app/config.py", source_sha=SOURCE):
    content = "VALUE = 1\n"
    before = hashlib.sha256(content.encode()).hexdigest()
    value = InitialInput(schema_version=2,
        analysis=dict(inventory_digest="f" * 64, item_id="item-001", grade="code_change",
                      rationale="Synthetic credential migration analysis"),
        binding=dict(plan_id="plan-one", tenant_id="tenant-one", plan_digest="a" * 64,
                     target_environment_id="target-one", plan_version=3, task_id="credential", task_digest="d" * 64,
                     source_sha=source_sha, collection_scope_digest="e" * 64, inventory_digest="f" * 64,
                     comparison_digest="1" * 64, analysis_receipt_id="analysis-one"), run_id="run-one",
        manifest=dict(source_sha=source_sha, files={path: before}, editable_paths=[path]), files={path: content})
    intent = InitialIntent(request_id="t10p-" + "2" * 64, plan_id="plan-one", plan_version=3,
        input_ref="initial-one", input_digest=digest(value), binding_digest=digest(value.binding),
        runtime_source_sha="3" * 40, recipient_public_key="4" * 64)
    proposal = Proposal(run_id="run-one", source_sha=source_sha, rationale="Synthetic replacement", risk="low",
        edits=[dict(path=path, before_sha256=before, replacement="VALUE = 2\n")])
    result = InitialResult(request_id=intent.request_id, input_digest=intent.input_digest,
        runtime_source_sha=intent.runtime_source_sha, execution_receipt_id="model-fixture", proposal=proposal,
        proposal_digest=digest(proposal), schema_valid=True, lint_passed=True)
    return value, intent, result


def builder(registry, tmp_path, **seeded):
    return candidate.PlatformCandidateBuilder(base_file=seed(registry, tmp_path, **seeded),
        credentials_file=tmp_path / "unused", receipts_dir=tmp_path / "receipts", registry=registry)


def test_candidate_is_the_base_plus_one_layer_of_the_proposal_for_every_platform(tmp_path):
    registry = FakeRegistry()
    value, intent, result = inputs()
    read = builder(registry, tmp_path).publication(value, intent, result)
    published = read(None, value, intent, result)
    index_raw, media_type = registry.manifest(published.image_digest)
    index = json.loads(index_raw)
    assert media_type == INDEX and [m["platform"]["architecture"] for m in index["manifests"]] == ["amd64", "arm64"]
    assert published.diff_hash == candidate.workflow_diff_hash(value, result)
    assert (published.request_id, published.source_sha, published.proposal_digest) == (
        intent.request_id, SOURCE, result.proposal_digest) and published.receipt_id.startswith("pcb-")
    for child in index["manifests"]:
        manifest = json.loads(registry.manifest(child["digest"])[0])
        config = json.loads(registry.blobs[manifest["config"]["digest"]])
        new = manifest["layers"][-1]
        assert len(manifest["layers"]) == 2 and new["mediaType"] == LAYER
        raw = gzip.decompress(registry.blobs[new["digest"]])
        assert config["rootfs"]["diff_ids"][-1] == sha(raw) and len(config["history"]) == 2
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            [member] = tar.getmembers()
            assert (member.name, member.mode, member.uid, member.mtime) == ("app/app/config.py", 0o644, 0, 0)
            assert tar.extractfile(member).read() == b"VALUE = 2\n"


def test_the_same_request_is_built_once_and_reproducibly(tmp_path):
    registry = FakeRegistry()
    value, intent, result = inputs()
    first = builder(registry, tmp_path).publication(value, intent, result)(None, value, intent, result)
    writes = len(registry.writes)
    again = builder(registry, tmp_path).publication(value, intent, result)(None, value, intent, result)
    assert again == first and len(registry.writes) == writes  # the saved receipt is reused, nothing pushed
    (tmp_path / "receipts" / f"{candidate.publication_request_id(intent)}.json").unlink()
    rebuilt = builder(registry, tmp_path).publication(value, intent, result)(None, value, intent, result)
    assert rebuilt.image_digest == first.image_digest  # deterministic layer: an immutable tag accepts the retry


@pytest.mark.parametrize("seeded, path, error", [
    ({}, "Dockerfile", "candidate_rebuild_required"),
    ({"copied": ("lib/",)}, "app/config.py", "candidate_rebuild_required"),
    ({"source_sha": "c" * 40}, "app/config.py", "candidate_base_mismatch"),
])
def test_what_cannot_be_a_layer_on_the_registered_base_is_refused(tmp_path, seeded, path, error):
    registry = FakeRegistry()
    value, intent, result = inputs(path=path)
    with pytest.raises(EventError, match=error):
        builder(registry, tmp_path, **seeded).publication(value, intent, result)
    assert registry.writes == []


def test_a_changed_base_or_a_missing_platform_is_never_built_on(tmp_path):
    registry = FakeRegistry()
    value, intent, result = inputs()
    built = builder(registry, tmp_path)
    built.base["platforms"]["linux/arm64"]["config"] = b"{}"  # pin no longer matches the registry
    with pytest.raises(EventError, match="candidate_base_mismatch"):
        built.publication(value, intent, result)
    built = builder(FakeRegistry(), tmp_path)
    built.base["platforms"]["linux/s390x"] = built.base["platforms"]["linux/amd64"]
    with pytest.raises(EventError, match="candidate_base_mismatch"):
        built.publication(value, intent, result)


def test_registry_failure_holds_without_a_receipt(tmp_path):
    registry = FakeRegistry()
    registry.fail = "manifest"
    value, intent, result = inputs()
    with pytest.raises(EventError, match="candidate_registry_write_failed"):
        builder(registry, tmp_path).publication(value, intent, result)
    assert not (tmp_path / "receipts").exists()


def test_reader_is_frozen_to_the_inputs_it_was_built_for(tmp_path):
    value, intent, result = inputs()
    read = builder(FakeRegistry(), tmp_path).publication(value, intent, result)
    other = result.model_copy(update={"execution_receipt_id": "model-other"})
    with pytest.raises(ValueError):
        read(None, value, intent, other)


def test_base_pin_shape_is_strict(tmp_path):
    path = seed(FakeRegistry(), tmp_path)
    for change in ({"workdir": "app"}, {"copied": ["../"]}, {"platforms": {}}, {"extra": 1}):
        path.write_text(json.dumps(json.loads(path.read_text()) | change))
        with pytest.raises(ValueError, match="candidate_base_configuration_invalid"):
            candidate.PlatformCandidateBuilder(base_file=path, credentials_file=tmp_path / "x",
                                               receipts_dir=tmp_path / "r", registry=FakeRegistry())
        path = seed(FakeRegistry(), tmp_path)


class Response:
    def __init__(self, status, headers=None, body=b""):
        self.status, self.headers, self.body = status, headers or {}, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _limit):
        return self.body


def test_http_registry_uses_the_mounted_ecr_credential_and_monolithic_upload(tmp_path):
    import urllib.error

    credentials = tmp_path / "config.json"
    host = REPOSITORY.split("/")[0]
    credentials.write_text(json.dumps({"auths": {host: {"auth": base64.b64encode(b"AWS:token").decode()}}}))
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request.get_method(), request.full_url, request.get_header("Authorization")))
            if request.get_method() == "HEAD":
                raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
            if request.get_method() == "POST":
                return Response(202, {"Location": "/v2/bravo/demo-chat/blobs/uploads/u1?state=x"})
            return Response(201, {"Docker-Content-Digest": sha(b"blob")})

    registry = candidate.Registry(REPOSITORY, credentials, opener=Opener())
    assert registry.put_blob(b"blob") == sha(b"blob")
    assert [c[0] for c in calls] == ["HEAD", "POST", "PUT"]
    assert calls[2][1] == f"https://{host}/v2/bravo/demo-chat/blobs/uploads/u1?state=x&digest={sha(b'blob')}"
    assert {c[2] for c in calls} == {"Basic " + base64.b64encode(b"AWS:token").decode()}
    credentials.write_text(json.dumps({"auths": {}}))
    with pytest.raises(EventError, match="candidate_registry_credential_missing"):
        registry.put_blob(b"blob")
