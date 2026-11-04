import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "export_target_image.py"
SPEC = importlib.util.spec_from_file_location("target_image_export", SOURCE)
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


def fixture():
    blobs = {}

    def add(content, kind):
        data = content if isinstance(content, bytes) else json.dumps(content).encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        blobs["blobs/sha256/" + digest[7:]] = data
        return {"digest": digest, "size": len(data), "mediaType": kind}

    config = add({"architecture": "amd64", "os": "linux", "config": {
        "User": "10001", "Labels": {"org.opencontainers.image.revision": export.SOURCE}}}, "application/vnd.oci.image.config.v1+json")
    app = add({"config": config, "layers": [add(str(i).encode(), "layer") for i in range(9)]},
              "application/vnd.oci.image.manifest.v1+json")
    attest = add({"config": add({}, "application/vnd.oci.image.config.v1+json"),
                  "layers": [add(b"attestation", "application/vnd.in-toto+json")]},
                 "application/vnd.oci.image.manifest.v1+json")
    root = add({"schemaVersion": 2, "manifests": [
        {**app, "platform": {"architecture": "amd64", "os": "linux"}}, attest]},
        "application/vnd.oci.image.index.v1+json")
    blobs["oci-layout"] = b'{"imageLayoutVersion":"1.0.0"}'
    blobs["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [
        {**root, "annotations": {"org.opencontainers.image.ref.name": export.IMAGE + ":sha-" + export.SOURCE}}
    ]}).encode()
    return blobs, root["digest"], app["digest"]


def archive(path, files):
    with tarfile.open(path, "w") as output:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))


class TargetImageExportTests(unittest.TestCase):
    def test_verify_only_receipt_matches_verified_bytes_without_provider(self):
        files, index, amd64 = fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
            path = pathlib.Path(directory) / "fixture.tar"
            receipt = pathlib.Path(directory) / "receipt.json"
            archive(path, files)
            expected = path.read_bytes()
            with patch.object(sys, "argv", ["export", "--verify-only", "--archive", str(path), "--receipt", str(receipt)]), \
                    patch.object(export, "run", side_effect=AssertionError("provider call")) as provider, \
                    contextlib.redirect_stdout(io.StringIO()):
                export.main()
            provider.assert_not_called()
            result = json.loads(receipt.read_text())
            self.assertEqual(result["archive_sha256"], hashlib.sha256(expected).hexdigest())
            self.assertEqual(result["archive_bytes"], len(expected))
            self.assertEqual(result["verified_blob_count"], 15)

    def test_path_replacement_during_verification_does_not_issue_receipt(self):
        self.assert_concurrent_change_rejected("replace")

    def test_same_size_in_place_write_during_verification_does_not_issue_receipt(self):
        self.assert_concurrent_change_rejected("in_place")

    def assert_concurrent_change_rejected(self, mode):
        files, index, amd64 = fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
            path = pathlib.Path(directory) / "fixture.tar"
            receipt = pathlib.Path(directory) / "receipt.json"
            archive(path, files)
            before = path.stat()
            original = export._verify_archive_stream

            def mutate(stream):
                inventory = original(stream)
                if mode == "replace":
                    replacement = pathlib.Path(directory) / "replacement"
                    replacement.write_bytes(b"not an OCI archive\n")
                    os.replace(replacement, path)
                else:
                    # Same length, and restore mtime: ctime still records the write.
                    with path.open("r+b") as changed:
                        changed.write(b"not an OCI archive\n")
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assertEqual(path.stat().st_size, before.st_size)
                return inventory

            with patch.object(export, "_verify_archive_stream", side_effect=mutate), \
                    patch.object(sys, "argv", ["export", "--verify-only", "--archive", str(path), "--receipt", str(receipt)]), \
                    self.assertRaisesRegex(export.Failure, "archive_changed_during_verification"):
                export.main()
            self.assertFalse(receipt.exists())

    def test_complete_graph_and_fixed_runtime_identity_pass(self):
        files, index, amd64 = fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
            path = pathlib.Path(directory) / "fixture.tar"
            archive(path, files)
            self.assertEqual(len(export.verify_archive(path)), 15)

    def test_tampered_or_missing_layer_is_rejected(self):
        for mode in ("tampered", "missing"):
            files, index, amd64 = fixture()
            layer = next(k for k, v in files.items() if v == b"0")
            if mode == "tampered":
                files[layer] = b"wrong"
            else:
                del files[layer]
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, \
                 patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
                path = pathlib.Path(directory) / "fixture.tar"
                archive(path, files)
                with self.assertRaises(export.Failure):
                    export.verify_archive(path)

    def test_unreachable_or_path_traversal_member_is_rejected(self):
        for name in ("../../outside", "blobs/sha256/" + hashlib.sha256(b"extra").hexdigest()):
            files, index, amd64 = fixture()
            files[name] = b"extra"
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, \
                 patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
                path = pathlib.Path(directory) / "fixture.tar"
                archive(path, files)
                with self.assertRaises(export.Failure):
                    export.verify_archive(path)

    def test_unreviewed_index_is_rejected(self):
        files, _, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "fixture.tar"
            archive(path, files)
            with self.assertRaises(export.Failure):
                export.verify_archive(path)

    def test_import_reference_override_is_rejected(self):
        files, index, amd64 = fixture()
        wrapper = json.loads(files["index.json"])
        wrapper["manifests"][0]["annotations"]["org.opencontainers.image.ref.name"] = "other/image:latest"
        files["index.json"] = json.dumps(wrapper).encode()
        with tempfile.TemporaryDirectory() as directory, patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
            path = pathlib.Path(directory) / "fixture.tar"
            archive(path, files)
            with self.assertRaisesRegex(export.Failure, "unexpected_import_reference"):
                export.verify_archive(path)

    def test_duplicate_member_is_rejected(self):
        files, index, amd64 = fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(export, "INDEX", index), patch.object(export, "AMD64", amd64):
            path = pathlib.Path(directory) / "fixture.tar"
            archive(path, files)
            with tarfile.open(path, "a") as output:
                duplicate = tarfile.TarInfo("index.json")
                duplicate.size = len(files["index.json"])
                output.addfile(duplicate, io.BytesIO(files["index.json"]))
            with self.assertRaisesRegex(export.Failure, "unexpected_archive_member"):
                export.verify_archive(path)

    def test_contract_rejects_other_repository_or_source(self):
        config = {"account_id": "000000000000", "region": export.REGION,
                  "repository_url": "000000000000.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat",
                  "source_sha": export.SOURCE, "digest": export.INDEX, "status": "image_copied_and_ci_pull_verified"}
        export.validate_contract(config)
        for key in ("repository_url", "account_id", "source_sha", "digest", "region", "status"):
            with self.subTest(key=key), self.assertRaises(export.Failure):
                export.validate_contract({**config, key: "unreviewed"})


if __name__ == "__main__":
    unittest.main()
