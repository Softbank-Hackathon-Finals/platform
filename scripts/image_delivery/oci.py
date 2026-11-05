"""Validate OCI content, runtime provenance and complete scan coverage before publication."""

from __future__ import annotations

import hashlib
import json
import re


class Failure(RuntimeError):
    pass


def source_sha(value):
    if not re.fullmatch(r"[a-f0-9]{40}", value or ""):
        raise Failure("invalid_source_sha")
    return value


def digest_value(value):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value or ""):
        raise Failure("invalid_image_digest")
    return value


def scan_summary(report):
    if not isinstance(report, dict):
        raise Failure("scan_report_not_object")
    results = report.get("Results")
    if (
        not isinstance(results, list)
        or not results
        or not all(
            isinstance(item, dict) and isinstance(item.get("Type"), str)
            for item in results
        )
    ):
        raise Failure("scan_coverage_missing")
    types = {item.get("Type") for item in results}
    if not {"debian", "python-pkg"}.issubset(types):
        # Preserve the established code consumed by the Executor/attachment tools.
        raise Failure("executor_package_coverage_missing")
    if any(
        item.get(key) is not None and not isinstance(item[key], list)
        for item in results
        for key in ("Vulnerabilities", "Secrets")
    ):
        raise Failure("scan_findings_malformed")
    vulnerabilities = sum(len(item.get("Vulnerabilities") or []) for item in results)
    secrets = sum(len(item.get("Secrets") or []) for item in results)
    if vulnerabilities or secrets:
        raise Failure("image_scan_has_findings")
    return {
        "scanners": ["vuln", "secret"],
        "severity": ["HIGH", "CRITICAL"],
        "ignore_unfixed": True,
        "package_types": sorted(types),
        "vulnerability_count": 0,
        "secret_match_count": 0,
    }


def verify_layout(layout, digest, source, *, repository):
    digest_value(digest)
    source_sha(source)
    blobs = layout / "blobs/sha256"
    if any(p.is_symlink() for p in (layout, layout / "blobs", blobs)):
        raise Failure("oci_symlink")
    inventory = {}
    for blob in blobs.iterdir():
        if (
            blob.is_symlink()
            or not blob.is_file()
            or not re.fullmatch(r"[a-f0-9]{64}", blob.name)
        ):
            raise Failure("unexpected_oci_blob")
        with blob.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != blob.name:
                raise Failure("oci_blob_digest_mismatch")
        inventory[blob.name] = blob.stat().st_size

    def document(identifier):
        digest_value(identifier)
        key = identifier.removeprefix("sha256:")
        if key not in inventory:
            raise Failure("oci_descriptor_missing")
        value = json.loads((blobs / key).read_text())
        if not isinstance(value, dict):
            raise Failure("oci_document_not_object")
        return value

    def descriptor_digest(descriptor):
        if not isinstance(descriptor, dict):
            raise Failure("oci_descriptor_malformed")
        child = digest_value(descriptor.get("digest"))
        if (
            type(descriptor.get("size")) is not int
            or inventory.get(child.removeprefix("sha256:")) != descriptor["size"]
            or not isinstance(descriptor.get("mediaType"), str)
        ):
            raise Failure("oci_descriptor_missing_or_size_mismatch")
        return child

    entrypoint = layout / "index.json"
    if entrypoint.is_symlink():
        raise Failure("oci_symlink")
    transport = json.loads(entrypoint.read_text())
    if (
        not isinstance(transport, dict)
        or transport.get("schemaVersion") != 2
        or not isinstance(transport.get("manifests"), list)
        or len(transport["manifests"]) != 1
        or descriptor_digest(transport["manifests"][0]) != digest
    ):
        raise Failure("oci_transport_target_mismatch")
    index = document(digest)
    if index.get("schemaVersion") != 2 or not isinstance(index.get("manifests"), list):
        raise Failure("oci_index_missing")
    pending, visited = [digest], set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        item = document(identifier)
        descriptors = item.get("manifests", []) + item.get("layers", [])
        if "config" in item:
            descriptors.append(item["config"])
        for descriptor in descriptors:
            child = descriptor_digest(descriptor)
            if (
                "manifest" in descriptor["mediaType"]
                or "index" in descriptor["mediaType"]
            ):
                pending.append(child)
    runtime = [
        m
        for m in index["manifests"]
        if m.get("platform") == {"architecture": "amd64", "os": "linux"}
    ]
    if len(runtime) != 1:
        raise Failure("runtime_platform_ambiguous")
    attestations = [
        m
        for m in index["manifests"]
        if m.get("annotations", {}).get("vnd.docker.reference.type")
        == "attestation-manifest"
        and m["annotations"].get("vnd.docker.reference.digest") == runtime[0]["digest"]
    ]
    if not attestations or len(runtime) + len(attestations) != len(index["manifests"]):
        raise Failure("runtime_attestation_mismatch")
    predicates = set()
    for attestation in attestations:
        for layer in document(attestation["digest"]).get("layers", []):
            if layer["mediaType"] != "application/vnd.in-toto+json":
                raise Failure("attestation_payload_type_mismatch")
            statement = document(layer["digest"])
            if not any(
                subject.get("digest") == {"sha256": runtime[0]["digest"][7:]}
                for subject in statement.get("subject", [])
            ):
                raise Failure("attestation_subject_mismatch")
            predicates.add(statement.get("predicateType"))
    if "https://spdx.dev/Document" not in predicates or not predicates.intersection(
        {"https://slsa.dev/provenance/v0.2", "https://slsa.dev/provenance/v1"}
    ):
        raise Failure("provenance_or_sbom_missing")
    config = document(document(runtime[0]["digest"])["config"]["digest"])["config"]
    labels = config.get("Labels", {})
    if (
        labels.get("org.opencontainers.image.revision") != source
        or labels.get("org.opencontainers.image.source")
        != "https://github.com/" + repository
        or config.get("User") != "10001:10001"
    ):
        raise Failure("runtime_source_or_user_mismatch")
    return inventory
