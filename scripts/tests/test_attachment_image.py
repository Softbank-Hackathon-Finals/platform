"""The attachment image path must keep the published images' bar and pack only what it verified."""
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.version_info < (3, 12):
    raise unittest.SkipTest("the reused Executor verifier needs Python 3.12")

from test_executor_image import layout

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(os.environ.get("T8_ATTACH_SCRIPTS_DIR") or ROOT / "scripts")
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("attachment_image", SCRIPTS / "attachment_image.py")
attachment_image = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(attachment_image)
executor_image = attachment_image.executor_image

SOURCE = "a" * 40
BASE = "python:3.12-slim-bookworm@sha256:" + "d" * 64
DOCKERFILE = f"FROM {BASE}\nUSER 10001:10001\n"
CLEAN_REPORT = {"Results": [{"Type": "debian", "Vulnerabilities": []}, {"Type": "python-pkg"}]}
PRIVATE_OUTPUT = "private output with a key"


def git(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True).stdout.strip()


def platform_layout(path, *, source, platform):
    """The Executor fixture is linux/amd64. Rewrite only its index for another platform, as buildx would build it."""
    digest, blobs, layer = layout(path, source=source)
    index = json.loads((blobs / digest[7:]).read_text())
    index["manifests"][0]["platform"]["architecture"] = platform.removeprefix("linux/")
    value = json.dumps(index).encode()
    (blobs / digest[7:]).unlink()
    digest = "sha256:" + hashlib.sha256(value).hexdigest()
    (blobs / digest[7:]).write_bytes(value)
    descriptor = {"mediaType": "application/vnd.oci.image.index.v1+json", "digest": digest, "size": len(value)}
    (path / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [descriptor]}))
    return digest, blobs, layer


def write_context(repo, *, dockerfile=DOCKERFILE):
    directory = repo / attachment_image.CONTEXT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    for name in attachment_image.CONTEXT_FILES:
        (directory / name).write_text(dockerfile if name.endswith("Dockerfile") else "# " + name + "\n")
    (directory / "unrelated.py").write_text("# must not reach the build context\n")
    return directory


def repository(path, *, reviewed=True, dockerfile=DOCKERFILE):
    git(path, "init", "-q")
    git(path, "config", "user.name", "T8 fixture")
    git(path, "config", "user.email", "t8-fixture@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    write_context(path, dockerfile=dockerfile)
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "fixture")
    head = git(path, "rev-parse", "HEAD")
    if reviewed:
        git(path, "update-ref", "refs/remotes/origin/develop", head)
    return head


class FakeDocker:
    """Answers the docker commands with real files, recording every command."""

    def __init__(self, source, *, report=None, fail=None, probe_output=b"attachment_runtime_probe=pass\n", tamper=False,
                 produced=None):
        self.source = source
        self.report = CLEAN_REPORT if report is None else report
        self.fail = fail
        self.probe_output = probe_output
        self.tamper = tamper
        self.produced = produced  # A builder that ignores --platform and makes this one instead.
        self.commands = []

    def phase_of(self, command):
        if command[:3] == ["docker", "buildx", "build"]:
            return "image_build"
        if command[:2] == ["docker", "run"] and attachment_image.TRIVY_IMAGE in command:
            return "image_scan"
        if command[:2] == ["docker", "load"]:
            return "runtime_load"
        if command[:2] == ["docker", "run"]:
            return "runtime_probe"
        if command[:2] == ["docker", "image"]:
            return "runtime_cleanup"
        return None

    def __call__(self, command, phase):
        self.commands.append(command)
        which = self.phase_of(command)
        if which is None:
            return attachment_image_real_run(command, phase)
        if self.fail == which:
            raise attachment_image.Failure(phase + "_failed")
        if which == "image_build":
            output = command[command.index("--output") + 1]
            destination = Path(re.search(r"dest=([^,]+)", output).group(1))
            platform = self.produced or command[command.index("--platform") + 1]
            digest, blobs, _ = platform_layout(destination, source=self.source, platform=platform)
            (destination / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')
            (destination / "ingest").mkdir()
            (destination / "ingest" / "partial").write_text("left by the builder")
            metadata = Path(command[command.index("--metadata-file") + 1])
            metadata.write_text(json.dumps({"containerimage.digest": digest}))
            if self.tamper:
                victim = min(blobs.iterdir())
                victim.write_bytes(b"tampered")
        elif which == "image_scan":
            mount = next(item for item in command if item.endswith(":/out"))
            Path(mount.split(":")[0], "trivy.json").write_text(json.dumps(self.report))
        elif which == "runtime_probe":
            return self.probe_output
        return b""


attachment_image_real_run = attachment_image.run


class Workspace(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()


class SourceContext(Workspace):
    def context(self, source):
        destination = self.root / "context"
        attachment_image.source_context(self.repo, source, destination)
        return destination

    def test_only_the_five_build_files_from_the_reviewed_commit(self):
        head = repository(self.repo)
        destination = self.context(head)
        self.assertEqual(sorted(p.name for p in destination.iterdir()), sorted(attachment_image.CONTEXT_FILES))
        self.assertFalse((destination / "unrelated.py").exists())

    def test_local_edits_do_not_reach_the_build(self):
        head = repository(self.repo)
        file = self.repo / attachment_image.CONTEXT_DIRECTORY / "attachment_copy.py"
        file.write_text("edited after review\n")
        self.assertNotIn("edited", (self.context(head) / "attachment_copy.py").read_text())

    def test_commit_that_is_not_in_develop_is_refused(self):
        head = repository(self.repo, reviewed=False)
        with self.assertRaisesRegex(attachment_image.Failure, "source_not_reviewed"):
            self.context(head)

    def test_invalid_commit_name_is_refused_before_git(self):
        repository(self.repo)
        for value in ("HEAD", "a" * 39, "A" * 40, "--help", None):
            with self.subTest(value=value), self.assertRaises(attachment_image.Failure):
                self.context(value)

    def test_missing_build_file_is_refused(self):
        repository(self.repo)
        git(self.repo, "rm", "-q", f"{attachment_image.CONTEXT_DIRECTORY}/sync_attachments.py")
        git(self.repo, "commit", "-q", "-m", "remove")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/develop", head)
        with self.assertRaises(attachment_image.Failure):
            self.context(head)

    def test_symlinked_build_file_is_refused(self):
        repository(self.repo)
        target = self.repo / attachment_image.CONTEXT_DIRECTORY / "sync_attachments.py"
        target.unlink()
        target.symlink_to("attachment_copy.py")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "symlink")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/develop", head)
        with self.assertRaisesRegex(attachment_image.Failure, "source_archive_inventory_mismatch"):
            self.context(head)


class TheBarIsNotLowered(unittest.TestCase):
    """The published image workflow is the standard; a stronger one there must fail here."""

    WORKFLOW = (ROOT / ".github/workflows/publish-executor-image.yml").read_text()

    def build(self, platform="linux/amd64"):
        return attachment_image.build_command(SOURCE, BASE, Path("/ctx"), Path("/layout"), Path("/meta.json"), platform)

    def scan(self):
        return attachment_image.scan_command(Path("/layout"), Path("/out"))

    def test_workflow_still_states_the_values_this_path_copies(self):
        for setting in ("platforms: linux/amd64", "provenance: mode=max", "sbom: true", "scanners: vuln,secret",
                        "severity: HIGH,CRITICAL", "ignore-unfixed: true", "exit-code: '1'", "format: json"):
            self.assertIn(setting, self.WORKFLOW)

    def test_build_makes_one_oci_index_for_the_chosen_platform_with_provenance_and_sbom(self):
        for platform in ("linux/amd64", "linux/arm64"):
            with self.subTest(platform=platform):
                command = self.build(platform)
                self.assertEqual(command[command.index("--platform") + 1], platform)
                self.assertEqual(command.count("--platform"), 1)
        command = self.build()
        self.assertEqual(command[:3], ["docker", "buildx", "build"])
        self.assertIn("--provenance=mode=max", command)
        self.assertRegex(" ".join(command), r"--sbom=generator=\S+@sha256:[0-9a-f]{64}")
        self.assertIn("type=oci,dest=/layout,tar=false", command)
        self.assertNotIn("--push", command)

    def test_attestations_are_bound_by_naming_the_image(self):
        # BuildKit leaves an attestation subject empty for an unnamed image, and the verifier refuses that.
        command = self.build()
        self.assertEqual(command[command.index("-t") + 1], f"{attachment_image.IMAGE_NAME}:sha-{SOURCE}")

    def test_build_labels_the_source_the_verifier_checks(self):
        command = " ".join(self.build())
        self.assertIn(f"org.opencontainers.image.revision={SOURCE}", command)
        self.assertIn(f"org.opencontainers.image.source=https://github.com/{executor_image.REPOSITORY}", command)

    def test_build_context_is_the_extracted_directory_only(self):
        command = self.build()
        self.assertEqual(command[-1], "/ctx")
        self.assertEqual(command[command.index("-f") + 1], "/ctx/attachment-copy.Dockerfile")

    def test_mirror_can_only_supply_the_same_pinned_base(self):
        command = self.build()
        self.assertEqual(command[command.index("--build-context") + 1],
                         f"docker.io/library/{BASE}=docker-image://mirror.gcr.io/library/{BASE}")

    def test_scan_settings_match_the_published_images(self):
        command = self.scan()
        joined = " ".join(command)
        for setting in ("--scanners vuln,secret", "--severity HIGH,CRITICAL", "--ignore-unfixed", "--exit-code 1",
                        "--format json", "--input /layout"):
            self.assertIn(setting, joined)
        self.assertRegex(attachment_image.TRIVY_IMAGE, r"@sha256:[0-9a-f]{64}$")
        self.assertIn(attachment_image.TRIVY_IMAGE, command)

    def test_scan_cannot_change_the_scanned_bytes(self):
        self.assertIn("/layout:/layout:ro", self.scan())
        self.assertNotIn("--ignorefile", self.scan())

    def test_base_must_be_a_pinned_python_image(self):
        self.assertEqual(attachment_image.pinned_base(DOCKERFILE), BASE)
        for text in ("FROM python:3.12-slim-bookworm\n", "FROM python:latest\n", "FROM other/python@sha256:" + "d" * 64 + "\n",
                     "FROM python@sha256:abc\n", f"FROM {BASE}\nFROM {BASE}\n", ""):
            with self.subTest(text=text), self.assertRaisesRegex(attachment_image.Failure, "base_image_not_pinned"):
                attachment_image.pinned_base(text)

    def test_probe_runs_the_chosen_platform(self):
        for platform in ("linux/amd64", "linux/arm64"):
            with self.subTest(platform=platform):
                command = attachment_image.probe_command("localhost/x@sha256:" + "c" * 64, platform)
                self.assertEqual(command[command.index("--platform") + 1], platform)

    def test_probe_runs_isolated_and_checks_the_copy_modules(self):
        command = attachment_image.probe_command("localhost/x@sha256:" + "c" * 64, "linux/amd64")
        joined = " ".join(command)
        for setting in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--rm"):
            self.assertIn(setting, joined)
        self.assertNotIn("-e", command)
        for module in ("boto3", "psycopg", "attachment_copy", "attachment_stores", "sync_attachments"):
            self.assertIn(module, attachment_image.PROBE)
        self.assertIn("10001", attachment_image.PROBE)
        self.assertIn("setuptools", attachment_image.PROBE)
        compile(attachment_image.PROBE, "probe", "exec")


class Archive(Workspace):
    def built(self):
        path = self.root / "layout"
        digest, blobs, _ = layout(path, source=SOURCE)
        (path / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}')
        (path / "ingest").mkdir()
        (path / "ingest" / "partial").write_text("left by the builder")
        return path, digest, blobs

    def pack(self, path):
        archive = self.root / "image.tar"
        attachment_image.pack(path, archive)
        return archive

    def rewrite(self, archive, add):
        """Copy the archive and let add() append one crafted member."""
        crafted = self.root / "crafted.tar"
        with tarfile.open(archive) as original, tarfile.open(crafted, "w") as output:
            for member in original.getmembers():
                output.addfile(member, original.extractfile(member))
            add(output)
        return crafted

    def test_archive_holds_exactly_the_layout_and_verifies_as_built(self):
        path, digest, blobs = self.built()
        archive = self.pack(path)
        with tarfile.open(archive) as stored:
            names = stored.getnames()
        self.assertEqual(sorted(names), sorted(["oci-layout", "index.json", *(f"blobs/sha256/{p.name}" for p in blobs.iterdir())]))
        self.assertEqual(attachment_image.verify_archive(archive, digest, SOURCE, self.root, "linux/amd64"),
                         executor_image.verify_layout(path, digest, SOURCE))

    def test_packing_does_not_record_who_or_when(self):
        path, _, _ = self.built()
        with tarfile.open(self.pack(path)) as stored:
            for member in stored.getmembers():
                self.assertEqual((member.uid, member.gid, member.uname, member.gname, member.mtime, member.mode),
                                 (0, 0, "", "", 0, 0o644))

    def test_same_layout_packs_to_the_same_bytes(self):
        path, _, _ = self.built()
        first = self.pack(path).read_bytes()
        os.utime(path / "index.json", (1, 1))
        self.assertEqual(self.pack(path).read_bytes(), first)

    def test_changed_blob_is_refused(self):
        path, digest, _ = self.built()
        archive = self.pack(path)
        with tarfile.open(archive) as stored:
            victim = next(m for m in stored.getmembers() if m.name.startswith("blobs/"))
            content = stored.extractfile(victim).read()
        tampered = self.root / "tampered.tar"
        with tarfile.open(archive) as stored, tarfile.open(tampered, "w") as output:
            for member in stored.getmembers():
                body = stored.extractfile(member).read()
                if member.name == victim.name:
                    body = bytes([content[0] ^ 1]) + content[1:]
                member.size = len(body)
                output.addfile(member, io.BytesIO(body))
        with self.assertRaises(attachment_image.Failure):
            attachment_image.verify_archive(tampered, digest, SOURCE, self.root, "linux/amd64")

    def test_unexpected_members_are_refused_without_being_extracted(self):
        path, digest, _ = self.built()
        archive = self.pack(path)

        def regular(name):
            def add(output):
                info = tarfile.TarInfo(name)
                info.size = 1
                output.addfile(info, io.BytesIO(b"x"))
            return add

        def link(output):
            info = tarfile.TarInfo("blobs/sha256/" + "f" * 64)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            output.addfile(info)

        def duplicate(output):
            info = tarfile.TarInfo("index.json")
            info.size = 1
            output.addfile(info, io.BytesIO(b"x"))

        cases = {"traversal": regular("../escaped"), "extra": regular("extra.txt"), "bad blob": regular("blobs/sha256/short"),
                 "link": link, "duplicate": duplicate}
        for name, add in cases.items():
            with self.subTest(name), self.assertRaisesRegex(attachment_image.Failure, "unexpected_archive_member"):
                attachment_image.verify_archive(self.rewrite(archive, add), digest, SOURCE, self.root, "linux/amd64")
        self.assertFalse((self.root / "escaped").exists())

    def test_archive_of_another_source_is_refused(self):
        path, digest, _ = self.built()
        with self.assertRaises(attachment_image.Failure):
            attachment_image.verify_archive(self.pack(path), digest, "b" * 40, self.root, "linux/amd64")

    def test_archive_of_another_platform_is_refused(self):
        path, digest, _ = self.built()
        with self.assertRaisesRegex(attachment_image.Failure, "runtime_platform_ambiguous"):
            attachment_image.verify_archive(self.pack(path), digest, SOURCE, self.root, "linux/arm64")


class OnePlatformIsVerified(unittest.TestCase):
    """The shared verifier checks the platform it is asked for, and other callers keep linux/amd64."""

    def test_supported_platforms(self):
        self.assertEqual(attachment_image.PLATFORMS, ("linux/amd64", "linux/arm64"))

    def test_each_platform_passes_only_as_itself(self):
        for built in attachment_image.PLATFORMS:
            for asked in attachment_image.PLATFORMS:
                with self.subTest(built=built, asked=asked), tempfile.TemporaryDirectory() as directory:
                    digest, _, _ = platform_layout(Path(directory), source=SOURCE, platform=built)
                    if built == asked:
                        self.assertTrue(attachment_image.verify_layout(Path(directory), digest, SOURCE, asked))
                    else:
                        with self.assertRaisesRegex(attachment_image.Failure, "runtime_platform_ambiguous"):
                            attachment_image.verify_layout(Path(directory), digest, SOURCE, asked)

    def test_other_callers_still_accept_only_amd64(self):
        with tempfile.TemporaryDirectory() as directory:
            digest, _, _ = platform_layout(Path(directory), source=SOURCE, platform="linux/arm64")
            with self.assertRaisesRegex(executor_image.Failure, "runtime_platform_ambiguous"):
                executor_image.verify_layout(Path(directory), digest, SOURCE)


class WholePath(Workspace):
    def setUp(self):
        super().setUp()
        self.head = repository(self.repo)

    def build(self, docker=None, platform="linux/amd64"):
        self.docker = docker or FakeDocker(self.head)
        with patch.object(attachment_image, "run", self.docker):
            return attachment_image.build(self.repo, self.head, self.output, platform)

    def outputs(self):
        return sorted(path.name for path in self.output.iterdir())

    def assert_refused(self, reason, docker):
        with self.assertRaisesRegex(attachment_image.Failure, reason):
            self.build(docker)
        self.assertEqual(self.outputs(), [])

    def test_receipt_binds_source_digest_and_archive_bytes(self):
        receipt = self.build()
        archive = self.output / f"attachment-copy-{self.head[:12]}-amd64.tar"
        self.assertEqual(self.outputs(), sorted([archive.name, f"attachment-copy-{self.head[:12]}-amd64.receipt.json"]))
        self.assertEqual(receipt["source_sha"], self.head)
        self.assertEqual(receipt["archive_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(receipt["archive_bytes"], archive.stat().st_size)
        self.assertEqual(receipt["image"], f"{attachment_image.IMAGE_NAME}@{receipt['digest']}")
        self.assertEqual(receipt["platform"], "linux/amd64")
        self.assertEqual(receipt["scan"]["vulnerability_count"], 0)
        self.assertGreater(receipt["verified_blob_count"], 0)
        stored = json.loads(archive.with_name(archive.stem + ".receipt.json").read_text())
        self.assertEqual(stored, receipt)

    def test_arm64_is_built_verified_probed_and_named_as_arm64(self):
        receipt = self.build(platform="linux/arm64")
        self.assertEqual(receipt["platform"], "linux/arm64")
        self.assertEqual(self.outputs(), [f"attachment-copy-{self.head[:12]}-arm64.receipt.json",
                                          f"attachment-copy-{self.head[:12]}-arm64.tar"])
        platforms = {self.docker.phase_of(c): c[c.index("--platform") + 1] for c in self.docker.commands if "--platform" in c}
        self.assertEqual(platforms, {"image_build": "linux/arm64", "runtime_probe": "linux/arm64"})

    def test_builder_output_for_another_platform_leaves_no_archive(self):
        with self.assertRaisesRegex(attachment_image.Failure, "runtime_platform_ambiguous"):
            self.build(FakeDocker(self.head, produced="linux/amd64"), platform="linux/arm64")
        self.assertEqual(self.outputs(), [])

    def test_both_platforms_of_one_commit_can_share_a_directory(self):
        self.build(platform="linux/amd64")
        self.build(platform="linux/arm64")
        self.assertEqual(len(self.outputs()), 4)

    def test_receipt_does_not_claim_what_happens_on_the_vm(self):
        receipt = self.build()
        for field in ("target_import_verified", "target_job_verified", "built_in_ci"):
            self.assertIs(receipt[field], False)
        for field in ("provenance_and_sbom_verified", "scanned_before_packing", "isolated_runtime_probe_verified"):
            self.assertIs(receipt[field], True)

    def test_receipt_carries_no_local_path(self):
        self.assertNotIn(str(self.root), json.dumps(self.build()))

    def test_scan_runs_on_the_build_output_before_anything_is_packed(self):
        self.build()
        names = [self.docker.phase_of(command) for command in self.docker.commands]
        names = [name for name in names if name]
        self.assertEqual(names, ["image_build", "image_scan", "runtime_load", "runtime_probe", "runtime_cleanup"])

    def test_scan_failure_leaves_no_archive(self):
        self.assert_refused("image_scan_failed", FakeDocker(self.head, fail="image_scan"))

    def test_scan_findings_or_missing_coverage_leave_no_archive(self):
        finding = {"Results": [{"Type": "debian", "Vulnerabilities": [{"VulnerabilityID": "x"}]}, {"Type": "python-pkg"}]}
        self.assert_refused("image_scan_has_findings", FakeDocker(self.head, report=finding))
        self.assert_refused("executor_package_coverage_missing", FakeDocker(self.head, report={"Results": [{"Type": "debian"}]}))

    def test_build_failure_leaves_no_archive(self):
        self.assert_refused("image_build_failed", FakeDocker(self.head, fail="image_build"))

    def test_tampered_layout_leaves_no_archive(self):
        self.assert_refused("oci_", FakeDocker(self.head, tamper=True))

    def test_archive_that_differs_from_the_verified_layout_leaves_no_archive(self):
        real_pack = attachment_image.pack

        def pack_with_extra(layout_path, archive):
            real_pack(layout_path, archive)
            with tarfile.open(archive, "a") as stored:
                info = tarfile.TarInfo("extra.txt")
                info.size = 1
                stored.addfile(info, io.BytesIO(b"x"))

        with patch.object(attachment_image, "pack", pack_with_extra):
            self.assert_refused("unexpected_archive_member", FakeDocker(self.head))

        def pack_with_unreferenced_blob_missing(layout_path, archive):
            (layout_path / "blobs/sha256" / ("f" * 64)).write_bytes(b"")  # Makes the packed inventory larger.
            real_pack(layout_path, archive)

        with patch.object(attachment_image, "pack", pack_with_unreferenced_blob_missing):
            self.assert_refused("(archive_inventory_mismatch|oci_blob_digest_mismatch)", FakeDocker(self.head))

    def test_failed_runtime_probe_leaves_no_archive(self):
        self.assert_refused("runtime_probe_failed", FakeDocker(self.head, fail="runtime_probe"))
        self.assert_refused("runtime_probe_failed", FakeDocker(self.head, probe_output=b"something else\n"))
        self.assert_refused("runtime_load_failed", FakeDocker(self.head, fail="runtime_load"))

    def test_loaded_image_is_removed_after_the_probe(self):
        for docker in (FakeDocker(self.head), FakeDocker(self.head, fail="runtime_probe")):
            removed = []
            with patch.object(attachment_image, "remove_loaded_image", removed.append):
                try:
                    self.build(docker)
                except attachment_image.Failure:
                    pass
            self.assertEqual(removed, [f"{attachment_image.IMAGE_NAME}:sha-{self.head}"])
            for path in self.output.iterdir():
                path.unlink()

    def test_existing_output_is_not_overwritten(self):
        (self.output / f"attachment-copy-{self.head[:12]}-amd64.tar").write_text("earlier")
        docker = FakeDocker(self.head)
        with self.assertRaisesRegex(attachment_image.Failure, "output_must_be_new"):
            self.build(docker)
        self.assertEqual(docker.commands, [])

    def test_digest_from_the_builder_is_validated(self):
        class BadDigest(FakeDocker):
            def __call__(self, command, phase):
                result = super().__call__(command, phase)
                if self.phase_of(command) == "image_build":
                    Path(command[command.index("--metadata-file") + 1]).write_text('{"containerimage.digest": "latest"}')
                return result

        self.assert_refused("invalid_image_digest", BadDigest(self.head))

    def test_job_renderer_accepts_the_receipt_image(self):
        workloads = ROOT / "infra/envs/target-workloads"
        with patch.object(sys, "path", [str(workloads), *sys.path]), patch.dict(sys.modules):
            for shadowed in ("render", "replication"):
                sys.modules.pop(shadowed, None)
            attachment_job = importlib.import_module("attachment_job")
            job = attachment_job.job(self.build()["image"], "r1", "Plan", 1, "10.43.0.25",
                                     "0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f")
        self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"].split("@")[0], attachment_image.IMAGE_NAME)


class OperatorGuide(unittest.TestCase):
    """The operator follows the guide on the VM, so its commands must be the ones this code measured."""

    GUIDE = (ROOT / "docs/operations/t8-attachment-copy-job.md").read_text()

    def test_guide_builds_with_this_script(self):
        self.assertIn("python scripts/attachment_image.py --source", self.GUIDE)

    def test_guide_builds_for_an_arm64_target_and_names_files_by_architecture(self):
        self.assertIn("--platform linux/arm64", self.GUIDE)
        self.assertIn("attachment-copy-<커밋 앞 12자리>-<아키텍처>.tar", self.GUIDE)

    def test_guide_imports_with_the_measured_arguments(self):
        flat = re.sub(r"\\\n\s*", "", self.GUIDE)
        self.assertIn(" ".join(attachment_image.IMPORT_ARGUMENTS), flat)

    def test_guide_runs_ctr_on_the_rke2_socket_and_namespace(self):
        self.assertIn("/var/lib/rancher/rke2/bin/ctr", self.GUIDE)
        self.assertIn("--address /run/k3s/containerd/containerd.sock --namespace k8s.io", self.GUIDE)

    def test_guide_compares_the_archive_hash_before_importing(self):
        self.assertLess(self.GUIDE.index("sha256sum --check"), self.GUIDE.index(" ".join(attachment_image.IMPORT_ARGUMENTS[:2])))

    def test_guide_checks_the_imported_digest_before_naming_it(self):
        check = self.GUIDE.index('grep "^localhost/bravo-attachment-copy:sha-$SOURCE " | grep -q " $DIGEST "')
        self.assertLess(check, self.GUIDE.index("images tag"))

    def test_guide_stops_at_the_first_failed_step(self):
        block = self.GUIDE[self.GUIDE.index("**3. VM 소유자:**"):self.GUIDE.index("## 판정")]
        self.assertIn("set -eu", block)


class RealRunner(unittest.TestCase):
    def test_failure_names_the_phase_and_drops_the_output(self):
        failing = [sys.executable, "-c", f"import sys; print({PRIVATE_OUTPUT!r}); print({PRIVATE_OUTPUT!r}, file=sys.stderr); sys.exit(3)"]
        with self.assertRaises(attachment_image.Failure) as caught:
            attachment_image.run(failing, "image_build")
        self.assertEqual(str(caught.exception), "image_build_failed")

    def test_success_returns_output_as_bytes(self):
        self.assertEqual(attachment_image.run([sys.executable, "-c", "print('ok')"], "phase"), b"ok\n")


class CommandLine(Workspace):
    def setUp(self):
        super().setUp()
        # main() tightens the process umask, which must not leak into other test modules.
        self.addCleanup(os.umask, os.umask(0o022))

    def run_main(self, *arguments, version=(3, 12)):
        output = io.StringIO()
        with patch.object(sys, "argv", ["attachment_image.py", *arguments]), patch.object(attachment_image, "PYTHON", version), \
                patch("sys.stdout", output):
            code = attachment_image.main()
        return code, output.getvalue()

    def test_success_prints_one_receipt(self):
        head = repository(self.repo)
        with patch.object(attachment_image, "run", FakeDocker(head)), patch.object(attachment_image, "REPOSITORY_ROOT", self.repo):
            code, output = self.run_main("--source", head, "--output-dir", str(self.output))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["source_sha"], head)
        self.assertEqual(json.loads(output)["platform"], "linux/amd64")

    def test_arm64_is_chosen_on_the_command_line(self):
        head = repository(self.repo)
        with patch.object(attachment_image, "run", FakeDocker(head)), patch.object(attachment_image, "REPOSITORY_ROOT", self.repo):
            code, output = self.run_main("--source", head, "--output-dir", str(self.output), "--platform", "linux/arm64")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["platform"], "linux/arm64")

    def test_other_platforms_are_refused_before_anything_runs(self):
        docker = FakeDocker(SOURCE)
        with patch.object(attachment_image, "run", docker), patch("sys.stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            self.run_main("--source", SOURCE, "--output-dir", str(self.output), "--platform", "linux/s390x")
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(docker.commands, [])

    def test_failure_prints_only_a_fixed_code_and_exits_nonzero(self):
        head = repository(self.repo)
        with patch.object(attachment_image, "run", FakeDocker(head, fail="image_scan")), \
                patch.object(attachment_image, "REPOSITORY_ROOT", self.repo):
            code, output = self.run_main("--source", head, "--output-dir", str(self.output))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {"attachment_image": "NOT_VERIFIED", "error_code": "image_scan_failed"})

    def test_unexpected_error_text_is_not_repeated(self):
        head = repository(self.repo)

        def explode(command, phase):
            if command[0] == "docker":
                raise OSError(PRIVATE_OUTPUT)
            return attachment_image_real_run(command, phase)

        with patch.object(attachment_image, "run", explode), patch.object(attachment_image, "REPOSITORY_ROOT", self.repo):
            code, output = self.run_main("--source", head, "--output-dir", str(self.output))
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE_OUTPUT, output)
        self.assertEqual(json.loads(output)["error_code"], "invalid_local_state")

    def test_older_python_is_refused(self):
        code, output = self.run_main("--source", SOURCE, "--output-dir", str(self.output), version=(3, 11))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error_code"], "python_3_12_required")

    def test_new_files_are_private_to_the_operator(self):
        head = repository(self.repo)
        os.umask(0)
        with patch.object(attachment_image, "run", FakeDocker(head)), patch.object(attachment_image, "REPOSITORY_ROOT", self.repo):
            self.run_main("--source", head, "--output-dir", str(self.output))
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0, path.name)


if __name__ == "__main__":
    unittest.main()
