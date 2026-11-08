"""The platform builds the PROPOSE candidate itself (T49, 2026-11-08 user decision: no GitHub in the happy path).

The candidate is the registered base image plus one layer that holds exactly the saved Proposal's file
replacements, pushed by digest to the platform's private ECR repository. The base is an index pinned at
registration (`base_file`), with each platform's image config, so nothing is downloaded from blob storage.
No Dockerfile step runs and no candidate code executes; the layer is deterministic, so a retry pushes the same
digests (the repository's tags are immutable). An edit outside the paths the base image copies verbatim
(`copied`, e.g. app/) cannot be expressed as a layer and is refused (candidate_rebuild_required).

    base_file: {"repository": "<account>.dkr.ecr.<region>.amazonaws.com/bravo/demo-chat",
                "source_sha": "<40 hex: the registered source the base was built from>",
                "index_digest": "sha256:...", "workdir": "/app", "copied": ["app/"],
                "platforms": {"linux/arm64": {"manifest_digest": "sha256:...", "config": "<base64>"}, ...}}

The returned reader binds the receipt to the exact saved input/intent/result, like the T7 reader it replaces:
T10's evidence_reader still rechecks authority under the plan lock and the T15/T31 diff binding is unchanged.
"""

import base64
import gzip
import hashlib
import io
import json
import os
import tarfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from bravo_agent.contracts import digest

from .initial_proposal import (
    CandidatePublication,
    InitialInput,
    InitialIntent,
    InitialProposalExecution,
    InitialResult,
)
from .store import EventError

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json")
MAX_MANIFEST = 4 * 1024 * 1024


def publication_request_id(intent: InitialIntent) -> str:
    """Map the immutable 69-character T10 ID to a 64-character build key (the T7 mapping, kept)."""
    intent = InitialIntent.model_validate_json(intent.model_dump_json())
    return hashlib.sha256(intent.request_id.encode("ascii")).hexdigest()


def workflow_diff_hash(value, result):
    """Change.diff_hash: the same bytes T15 builds and T31 applies (bravo_apply.binding.render_diff)."""
    from bravo_apply.binding import diff_hash, render_diff

    edits = [edit.model_dump(mode="json") for edit in result.proposal.edits]
    return diff_hash(render_diff(dict(value.files), edits))


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # blob storage redirects would carry the registry token elsewhere


class Registry:
    """The OCI distribution API of one repository with an ECR docker config (`AWS:<token>`).

    The credential file is a mounted Secret the platform refreshes (ecr_credentials); it is read per call."""

    def __init__(self, repository, credentials_file, *, opener=None):
        host, _, name = repository.partition("/")
        if not host or not name or "/" not in repository:
            raise ValueError("candidate_registry_invalid")
        self.host, self.base = host, f"https://{host}/v2/{name}"
        self.credentials_file = Path(credentials_file)
        self.opener = opener or urllib.request.build_opener(_NoRedirect)

    def _auth(self):
        auths = json.loads(self.credentials_file.read_text())["auths"]
        entry = auths.get(self.host) or auths.get("https://" + self.host)
        if not entry or not entry.get("auth"):
            raise EventError("candidate_registry_credential_missing")
        return "Basic " + entry["auth"]

    def _send(self, method, url, *, body=None, headers=None):
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"Authorization": self._auth(), **(headers or {})})
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.headers, response.read(MAX_MANIFEST + 1)
        except urllib.error.HTTPError as error:
            return error.code, error.headers, b""
        except (urllib.error.URLError, TimeoutError, OSError):
            raise EventError("candidate_registry_unreachable") from None

    def manifest(self, reference):
        status, headers, raw = self._send("GET", f"{self.base}/manifests/{reference}",
                                          headers={"Accept": ", ".join(INDEX_TYPES + MANIFEST_TYPES)})
        if status != 200 or len(raw) > MAX_MANIFEST:
            raise EventError("candidate_registry_read_failed")
        return raw, headers.get("Content-Type", "").split(";")[0]

    def put_blob(self, data):
        blob = _sha(data)
        status, _, _ = self._send("HEAD", f"{self.base}/blobs/{blob}")
        if status == 200:
            return blob
        status, headers, _ = self._send("POST", f"{self.base}/blobs/uploads/")
        location = headers.get("Location") if headers else None
        if status != 202 or not location:
            raise EventError("candidate_registry_write_failed")
        if location.startswith("/"):
            location = f"https://{self.host}{location}"
        status, _, _ = self._send("PUT", location + ("&" if "?" in location else "?") + "digest=" + blob, body=data,
                                  headers={"Content-Type": "application/octet-stream"})
        if status != 201:
            raise EventError("candidate_registry_write_failed")
        return blob

    def put_manifest(self, reference, data, media_type):
        status, headers, _ = self._send("PUT", f"{self.base}/manifests/{reference}", body=data,
                                        headers={"Content-Type": media_type})
        if status != 201 or (headers.get("Docker-Content-Digest") or _sha(data)) != _sha(data):
            raise EventError("candidate_registry_write_failed")
        return _sha(data)


def _base(path):
    base = json.loads(Path(path).read_text())
    platforms = base.get("platforms") if isinstance(base, dict) else None
    if (type(base) is not dict
            or set(base) != {"repository", "source_sha", "index_digest", "workdir", "copied", "platforms"}
            or type(platforms) is not dict or not platforms
            or not str(base["workdir"]).startswith("/") or ".." in PurePosixPath(base["workdir"]).parts
            or type(base["copied"]) is not list or not base["copied"]
            or not all(isinstance(p, str) and p.endswith("/") and ".." not in p for p in base["copied"])
            or any(set(v) != {"manifest_digest", "config"} for v in platforms.values())):
        raise ValueError("candidate_base_configuration_invalid")
    for item in platforms.values():
        item["config"] = base64.b64decode(item["config"], validate=True)
    return base


def layer(workdir, edits):
    """(gzip bytes, diff_id): the replaced files at <workdir>/<path>, root-owned 0644, mtime 0, sorted.

    Parent directories exist in the base image and are not rewritten."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for edit in sorted(edits, key=lambda e: e["path"]):
            data = edit["replacement"].encode("utf-8")
            info = tarfile.TarInfo(str(PurePosixPath(workdir.lstrip("/")) / edit["path"]))
            info.size, info.mode, info.mtime, info.uid, info.gid = len(data), 0o644, 0, 0, 0
            tar.addfile(info, io.BytesIO(data))
    tar_bytes = raw.getvalue()
    return gzip.compress(tar_bytes, compresslevel=6, mtime=0), _sha(tar_bytes)


class PlatformCandidateBuilder:
    def __init__(self, *, base_file, credentials_file, receipts_dir, registry=None):
        self.base = _base(base_file)
        self.registry = registry or Registry(self.base["repository"], credentials_file)
        self.receipts = Path(receipts_dir)
        if not self.receipts.is_absolute() or not self.receipts.parent.is_dir():
            raise ValueError("candidate_receipts_mount_required")

    def _edits(self, value, result):
        edits = [edit.model_dump(mode="json") for edit in result.proposal.edits]
        for edit in edits:
            path = PurePosixPath(edit["path"])
            if (path.is_absolute() or ".." in path.parts or edit["path"] not in value.files
                    or not any(edit["path"].startswith(prefix) for prefix in self.base["copied"])):
                raise EventError("candidate_rebuild_required")  # not expressible as a layer on the base
        return edits

    def _push(self, request_id, edits, proposal_digest):
        base, registry = self.base, self.registry
        index_raw, index_type = registry.manifest(base["index_digest"])
        index = json.loads(index_raw)
        if _sha(index_raw) != base["index_digest"] or index_type not in INDEX_TYPES:
            raise EventError("candidate_base_mismatch")
        gz, diff_id = layer(base["workdir"], edits)
        layer_digest = registry.put_blob(gz)
        children, built = [], {}
        for entry in index["manifests"]:
            platform = entry.get("platform") or {}
            key = f"{platform.get('os')}/{platform.get('architecture')}" + (
                f"/{platform['variant']}" if platform.get("variant") else "")
            if key not in base["platforms"]:
                continue  # attestation manifests describe the base build, not the candidate
            pinned = base["platforms"][key]
            raw, media_type = registry.manifest(entry["digest"])
            manifest = json.loads(raw)
            if (_sha(raw) != entry["digest"] or entry["digest"] != pinned["manifest_digest"]
                    or media_type not in MANIFEST_TYPES or manifest["config"]["digest"] != _sha(pinned["config"])):
                raise EventError("candidate_base_mismatch")
            config = json.loads(pinned["config"])
            config["rootfs"]["diff_ids"] = [*config["rootfs"]["diff_ids"], diff_id]
            config["history"] = [*config.get("history", []), {
                "created_by": f"bravo platform candidate {request_id[:12]}: {len(edits)} file(s)",
                "comment": proposal_digest}]
            config_raw = _canonical(config)
            config_digest = registry.put_blob(config_raw)
            manifest["config"] = {**manifest["config"], "digest": config_digest, "size": len(config_raw)}
            manifest["layers"] = [*manifest["layers"], {"mediaType": manifest["layers"][-1]["mediaType"],
                                                        "digest": layer_digest, "size": len(gz)}]
            child_raw = _canonical(manifest)
            child = registry.put_manifest(_sha(child_raw), child_raw, media_type)
            children.append({"mediaType": media_type, "digest": child, "size": len(child_raw), "platform": platform})
            built[key] = child
        if set(built) != set(base["platforms"]):
            raise EventError("candidate_base_mismatch")  # every pinned platform must be in the base index
        candidate = {key: value for key, value in index.items() if key not in ("manifests", "annotations")}
        candidate["manifests"] = children
        candidate["annotations"] = {"dev.bravo.candidate.request": request_id,
                                    "dev.bravo.candidate.base": base["index_digest"]}
        candidate_raw = _canonical(candidate)
        tag = f"platform-candidate-{request_id[:32]}"
        image = registry.put_manifest(tag, candidate_raw, index_type)
        readback, _ = registry.manifest(image)
        if _sha(readback) != image:
            raise EventError("candidate_registry_read_failed")
        return image, tag, layer_digest, diff_id, built

    def publication(self, value: InitialInput, intent: InitialIntent, result: InitialResult):
        """Build and push once per request (a saved receipt is reused); return T10's local reader."""
        value = InitialInput.model_validate_json(value.model_dump_json())
        intent = InitialIntent.model_validate_json(intent.model_dump_json())
        if (intent.plan_id, intent.plan_version, intent.input_digest, intent.binding_digest) != (
                value.binding.plan_id, value.binding.plan_version, digest(value), digest(value.binding)):
            raise EventError("initial_publication_unconfirmed")
        result = InitialProposalExecution._checked(intent, value, result)
        if value.binding.source_sha != self.base["source_sha"]:
            raise EventError("candidate_base_mismatch")  # the base must be the registered source
        request_id = publication_request_id(intent)
        edits = self._edits(value, result)
        diff_hash = workflow_diff_hash(value, result)
        path = self.receipts / f"{request_id}.json"
        receipt = json.loads(path.read_text()) if path.exists() else None
        if receipt is None:
            image, tag, layer_digest, diff_id, built = self._push(request_id, edits, result.proposal_digest)
            receipt = {"kind": "bravo.platform-candidate.v1", "request_id": intent.request_id,
                       "plan_digest": value.binding.plan_digest, "source_sha": value.binding.source_sha,
                       "proposal_digest": result.proposal_digest, "diff_hash": diff_hash,
                       "base": {"repository": self.base["repository"], "index_digest": self.base["index_digest"]},
                       "layer": {"digest": layer_digest, "diff_id": diff_id, "files": sorted(e["path"] for e in edits)},
                       "platforms": built, "image": {"repository": self.base["repository"], "tag": tag,
                                                     "digest": image}}
            self.receipts.mkdir(mode=0o700, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(receipt, sort_keys=True))
            os.replace(tmp, path)  # write-once: the same request always maps to the same pushed digest
        if (receipt["request_id"], receipt["plan_digest"], receipt["source_sha"], receipt["proposal_digest"],
                receipt["diff_hash"]) != (intent.request_id, value.binding.plan_digest, value.binding.source_sha,
                                          result.proposal_digest, diff_hash):
            raise EventError("initial_publication_unconfirmed")
        bound = digest(dict(input=value.model_dump(), intent=intent.model_dump(), result=result.model_dump()))
        record = CandidatePublication(receipt_id="pcb-" + digest(receipt).removeprefix("sha256:")[:60],
            request_id=intent.request_id, plan_digest=value.binding.plan_digest, source_sha=value.binding.source_sha,
            proposal_digest=result.proposal_digest, diff_hash=diff_hash, image_digest=receipt["image"]["digest"])
        frozen = record.model_dump_json()

        def read(_connection, current_value, current_intent, current_result):
            if digest(dict(input=current_value.model_dump(), intent=current_intent.model_dump(),
                           result=current_result.model_dump())) != bound:
                raise ValueError("candidate_publication_unconfirmed")
            return CandidatePublication.model_validate_json(frozen)

        return read
