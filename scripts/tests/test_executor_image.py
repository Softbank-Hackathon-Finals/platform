import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


image = module("executor_image")
provider = module("executor_provider_config")
SOURCE = "a" * 40
ACCOUNT = "111122223333"
DESTINATION = ACCOUNT + ".dkr.ecr.ap-northeast-2.amazonaws.com/bravo/cutover-executor"


def layout(path, *, source=SOURCE, user="10001:10001", subject=None, sbom=True):
    blobs = path / "blobs/sha256"
    blobs.mkdir(parents=True)

    def blob(data, media="application/vnd.oci.image.manifest.v1+json"):
        value = json.dumps(data).encode()
        digest = hashlib.sha256(value).hexdigest()
        (blobs / digest).write_bytes(value)
        return {"mediaType": media, "digest": "sha256:" + digest, "size": len(value)}

    config = blob({"config": {"User": user, "Labels": {
        "org.opencontainers.image.source": "https://github.com/" + image.REPOSITORY,
        "org.opencontainers.image.revision": source}}}, "application/vnd.oci.image.config.v1+json")
    layer = blob({"filesystem": "fixture"}, "application/vnd.oci.image.layer.v1.tar")
    runtime = blob({"schemaVersion": 2, "config": config, "layers": [layer]})
    runtime["platform"] = {"architecture": "amd64", "os": "linux"}
    statements = ["https://slsa.dev/provenance/v1"] + (["https://spdx.dev/Document"] if sbom else [])
    layers = [blob({"subject": [{"digest": {"sha256": subject or runtime["digest"][7:]}}],
                    "predicateType": value}, "application/vnd.in-toto+json") for value in statements]
    attestation = blob({"schemaVersion": 2, "layers": layers})
    attestation["annotations"] = {"vnd.docker.reference.type": "attestation-manifest",
                                  "vnd.docker.reference.digest": runtime["digest"]}
    index = blob({"schemaVersion": 2, "manifests": [runtime, attestation]}, "application/vnd.oci.image.index.v1+json")
    (path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [index]}))
    return index["digest"], blobs, layer


class ArtifactBoundaryTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_rebuild_attempt_does_not_overwrite_previous_publication(self):
        first = image.publication_tag(SOURCE, {"GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"})
        second = image.publication_tag(SOURCE, {"GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "2"})
        self.assertNotEqual(first, second)
        with self.assertRaises(image.Failure):
            image.publication_tag(SOURCE, {})

    def test_transport_cannot_publish_a_different_unverified_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            digest, _, layer = layout(path)
            (path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [layer]}))
            with self.assertRaisesRegex(image.Failure, "oci_transport_target_mismatch"):
                image.verify_layout(path, digest, SOURCE)

    def test_exact_source_with_bound_provenance_and_sbom(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            digest, blobs, _ = layout(path)
            self.assertEqual(len(image.verify_layout(path, digest, SOURCE)), len(list(blobs.iterdir())))

    def test_wrong_source_user_or_attestation_is_rejected(self):
        for kwargs in ({"source": "b" * 40}, {"user": "0"}, {"subject": "b" * 64}, {"sbom": False}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                digest, _, _ = layout(path, **kwargs)
                with self.assertRaises(image.Failure):
                    image.verify_layout(path, digest, SOURCE)

    def test_tampered_missing_or_symlinked_blob_is_rejected(self):
        for mutation in ("tamper", "remove", "symlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                digest, blobs, layer = layout(path)
                target = blobs / layer["digest"][7:]
                if mutation == "tamper":
                    target.write_bytes(b"changed")
                else:
                    target.unlink()
                    if mutation == "symlink":
                        target.symlink_to(path / "untrusted")
                with self.assertRaises(image.Failure):
                    image.verify_layout(path, digest, SOURCE)

    def test_scan_needs_package_coverage_and_no_findings(self):
        clean = {"Results": [{"Type": "debian"}, {"Type": "python-pkg"}]}
        self.assertEqual(image.scan_summary(clean)["vulnerability_count"], 0)
        bad = [None, [], {}, {"Results": []}, {"Results": [None]}, {"Results": [{"Type": "debian"}]},
               {"Results": [{"Type": "debian", "Secrets": {}}, {"Type": "python-pkg"}]},
               {"Results": [{"Type": "debian", "Vulnerabilities": [{"Severity": "HIGH"}]}, {"Type": "python-pkg"}]},
               {"Results": [{"Type": "debian"}, {"Type": "python-pkg", "Secrets": [{"Title": "fixture"}]}]}]
        for report in bad:
            with self.subTest(report=report), self.assertRaises(image.Failure):
                image.scan_summary(report)

    def test_workflow_identity_and_branch_are_all_required(self):
        env = {"GITHUB_REPOSITORY": image.REPOSITORY, "GITHUB_REPOSITORY_ID": "1443045223",
               "GITHUB_REPOSITORY_OWNER_ID": "350259663", "GITHUB_REF": "refs/heads/develop",
               "GITHUB_EVENT_NAME": "workflow_dispatch"}
        image.workflow_boundary(env)
        for key in env:
            with self.subTest(key=key), self.assertRaises(image.Failure):
                image.workflow_boundary({**env, key: "another-value"})

    def test_destination_is_exact_account_region_and_executor_repo(self):
        image.destination_boundary(DESTINATION, ACCOUNT)
        for value in (DESTINATION.replace("cutover-executor", "demo-chat"), DESTINATION + "-other",
                      DESTINATION.replace(ACCOUNT, "222222222222"), DESTINATION.replace("ap-northeast-2", "us-east-1")):
            with self.subTest(value=value), self.assertRaises(image.Failure):
                image.destination_boundary(value, ACCOUNT)

    def test_wrong_publisher_cannot_copy(self):
        identity = json.dumps({"Account": ACCOUNT, "Arn": "arn:aws:sts::111122223333:assumed-role/another-role/session"})
        with patch.object(image, "verify", return_value={}), patch.object(image, "run", return_value=identity) as run:
            with self.assertRaisesRegex(image.Failure, "publisher_identity_mismatch"):
                image.publish(Path("unused"), SOURCE, "sha256:" + "c" * 64, DESTINATION, ACCOUNT)
            self.assertEqual(run.call_count, 1)

    def test_different_digest_at_immutable_tag_cannot_copy(self):
        identity = json.dumps({"Account": ACCOUNT, "Arn": "arn:aws:sts::111122223333:assumed-role/bravo-executor-image-publisher/session"})
        with patch.object(image, "verify", return_value={}), patch.object(image, "run", return_value=identity) as run, \
                patch.object(image, "existing_digest", return_value="sha256:" + "d" * 64):
            with self.assertRaisesRegex(image.Failure, "immutable_tag_conflict"):
                image.publish(Path("unused"), SOURCE, "sha256:" + "c" * 64, DESTINATION, ACCOUNT)
            self.assertEqual(run.call_count, 1)


@contextmanager
def source_repository():
    with tempfile.TemporaryDirectory() as directory:
        previous = Path.cwd()
        os.chdir(directory)
        try:
            def git(*args):
                return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL).decode().strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.com")
            root = Path(image.COMPONENT)
            (root / "src/bravo_cutover").mkdir(parents=True)
            for name in ("Dockerfile", "pyproject.toml", "uv.lock", "src/bravo_cutover/server.py"):
                (root / name).write_text("reviewed source\n")

            def commit():
                git("add", ".")
                git("commit", "-qm", "fixture")
                return git("rev-parse", "HEAD")

            source = commit()
            git("update-ref", "refs/remotes/origin/develop", source)
            yield root, source, commit, git
        finally:
            os.chdir(previous)


class SourceBoundaryTests(unittest.TestCase):
    def test_context_uses_committed_component_not_dirty_or_untracked_files(self):
        with source_repository() as (root, source, _, _):
            (root / "Dockerfile").write_text("unreviewed edit\n")
            (root / ".env").write_text("local fixture\n")
            manifest = image.prepare(source, Path("context"))
            self.assertEqual(Path("context/Dockerfile").read_text(), "reviewed source\n")
            self.assertFalse(Path("context/.env").exists())
            self.assertEqual(manifest["component"], image.COMPONENT)

    def test_commit_outside_reviewed_branch_is_rejected(self):
        with source_repository() as (root, _, commit, _):
            (root / "Dockerfile").write_text("new branch\n")
            source = commit()
            with self.assertRaisesRegex(image.Failure, "source_not_reviewed"):
                image.prepare(source, Path("context"))

    def test_tracked_credential_file_and_symlink_are_rejected(self):
        for name in (".env", "client.key", "linked"):
            with self.subTest(name=name), source_repository() as (root, _, commit, git):
                path = root / name
                if name == "linked":
                    path.symlink_to("Dockerfile")
                else:
                    path.write_text("fixture\n")
                source = commit()
                git("update-ref", "refs/remotes/origin/develop", source)
                with self.assertRaises(image.Failure):
                    image.prepare(source, Path("context"))


class ProviderConfigurationTests(unittest.TestCase):
    def current(self):
        return {"apiVersion": "kubelet.config.k8s.io/v1", "kind": "CredentialProviderConfig", "providers": [{
            "name": "ecr-credential-provider", "apiVersion": "credentialprovider.kubelet.k8s.io/v1",
            "defaultCacheDuration": "0s", "env": [{"name": "AWS_REGION", "value": "ap-northeast-2"}],
            "matchImages": [ACCOUNT + ".dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat"]}]}

    def test_addition_preserves_original_and_is_idempotent(self):
        current = self.current()
        before = copy.deepcopy(current)
        desired = provider.extend(current, ACCOUNT)
        self.assertEqual(current, before)
        self.assertEqual(desired["providers"][0]["matchImages"], [before["providers"][0]["matchImages"][0], DESTINATION])
        self.assertEqual(provider.extend(desired, ACCOUNT), desired)

    def test_foreign_account_wildcard_or_provider_options_are_rejected(self):
        for key, value in (("matchImages", ["*.amazonaws.com"]), ("name", "other"),
                           ("env", [{"name": "AWS_PROFILE", "value": "another"}]), ("args", ["--other"])):
            current = self.current()
            current["providers"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                provider.extend(current, ACCOUNT)
        with self.assertRaises(ValueError):
            provider.extend(self.current(), "222222222222")

    def test_unknown_root_or_malformed_provider_is_rejected(self):
        for current in ([], {**self.current(), "unknown": True}, {**self.current(), "providers": [None]}):
            with self.subTest(current=current), self.assertRaises(ValueError):
                provider.extend(current, ACCOUNT)


if __name__ == "__main__":
    unittest.main()
