"""Publication refusals and recovery decisions without network credentials."""

import ast
import fnmatch
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import registration_delivery as delivery

spec = importlib.util.spec_from_file_location(
    "registration_checkout",
    ROOT / "services/platform-events/deploy/registration/checkout.py",
)
checkout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkout)
SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64
ENV = {
    "GITHUB_REPOSITORY": delivery.REPOSITORY,
    "GITHUB_REF": "refs/heads/develop",
    "GITHUB_EVENT_NAME": "push",
    "GITHUB_RUN_ID": "42",
    "GITHUB_RUN_ATTEMPT": "1",
    "GITHUB_WORKFLOW_SHA": SOURCE,
}
RECEIPT = {"source_revision": SOURCE, "image_digest": DIGEST, "image": delivery.IMAGE}


class SourceCoverage(unittest.TestCase):
    def test_edge_sources_trigger_publication_and_supersede_old_images(self):
        service = delivery.SERVICES["edge-control"]
        workflow = (ROOT / ".github/workflows/publish-edge-control-image.yml").read_text()
        for path in ("services/edge-control", "services/control-gate"):
            self.assertIn(path, service.build_inputs)
            self.assertIn(path, service.inputs)
            self.assertIn("      - " + path + "/**", workflow)

    def test_every_current_package_is_detected_by_publication_and_supersession(self):
        builder = ast.parse((ROOT / "services/platform-events/deploy/product-read/build.py").read_text())
        assignments = {
            node.targets[0].id: node.value
            for node in builder.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        }
        packages = ast.literal_eval(assignments["PACKAGES"])
        # Observability is deliberately copied from a fixed historical revision.
        self.assertIn("OBSERVABILITY_REVISION", assignments)
        paths = []
        for service, package in packages.items():
            if service != "observability":
                paths.extend((f"services/{service}/src/{package}/module.py",
                              f"services/{service}/pyproject.toml"))
        paths.extend(("services/platform-events/uv.lock",
                      "services/platform-events/deploy/registration/gitops.Dockerfile",
                      "services/platform-events/deploy/product-read/build.py"))
        for path in paths:
            with self.subTest(build_input=path):
                self.assertTrue(any(path == p or path.startswith(p + "/") for p in delivery.BUILD_INPUTS),
                                "build input missing from promotion source check")
        paths.extend(("scripts/registration_delivery.py", "scripts/image_delivery/oci.py",
                      ".github/workflows/publish-registration-image.yml"))
        for service in delivery.SERVICES.values():
            workflow = (ROOT / (".github/workflows/publish-" + service.slug + "-image.yml")).read_text()
            section = workflow.split("    paths:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
            globs = [line.strip()[2:] for line in section.splitlines() if line.strip().startswith("- ")]
            service_paths = [p for p in paths if p != ".github/workflows/publish-registration-image.yml"]
            service_paths.append(".github/workflows/publish-" + service.slug + "-image.yml")
            for path in service_paths:
                with self.subTest(service=service.slug, path=path):
                    self.assertTrue(any(path == p or path.startswith(p + "/") for p in service.inputs),
                                    "build input missing from source freshness check")
                    self.assertTrue(any(fnmatch.fnmatchcase(path, pattern) for pattern in globs),
                                    "build input missing from publication trigger")

    def test_inventory_change_supersedes_old_build_before_any_pr_write(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(["git", "-C", directory, *args], text=True).strip()
            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            source_file = Path(directory) / "services/inventory/src/bravo_inventory/scan.py"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("version = 1\n")
            git("add", ".")
            git("commit", "-qm", "initial")
            source = git("rev-parse", "HEAD")
            source_file.write_text("version = 2\n")
            git("commit", "-qam", "inventory changed")
            git("update-ref", "refs/remotes/origin/develop", "HEAD")
            called = []
            def invoke(args, **kwargs):
                called.append(args)
                if args[:2] == ["git", "fetch"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                self.assertEqual(args[0], "git", "superseded build must not call GitHub")
                return subprocess.run(["git", "-C", directory, *args[1:]],
                                      capture_output=True, text=True, check=True)
            receipt = RECEIPT | {"source_revision": source, "published": True,
                                 "registry_visibility": "private", "deployed": False}
            with patch.dict(os.environ, ENV), patch.object(delivery, "run", side_effect=invoke):
                self.assertEqual(delivery.propose(receipt), {"action": "superseded", "deployed": False})
            self.assertFalse(any(args[1] in {"switch", "push", "commit"} for args in called))


class PromotionSource(unittest.TestCase):
    service = delivery.REGISTRATION

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("services/inventory/src/bravo_inventory/scan.py", "version = 1\n")
        self.write(self.service.values, "{}")
        self.source = self.commit("source")
        self.git("update-ref", "refs/remotes/origin/develop", self.source)
        self.receipt = RECEIPT | {"image": self.service.image,"source_revision": self.source, "published": True,
                                  "registry_visibility": "private", "deployed": False}
        self.calls = []

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def promote(self):
        self.write(self.service.values, json.dumps(delivery.promotion_values(self.receipt, self.service)))
        self.write(str(Path(self.service.values).with_name(self.service.publication)),
                   json.dumps(self.receipt))
        return self.commit("promote")

    def check(self):
        def invoke(args, **kwargs):
            self.calls.append(args)
            self.assertEqual(args[0], "git")
            self.assertIn(args[1], {"merge-base", "diff", "show"})
            return subprocess.run(["git", "-C", str(self.root), *args[1:]],
                                  capture_output=True, text=True, check=True)
        with patch.object(delivery, "run", side_effect=invoke):
            return delivery.check_promotion(self.service)

    def test_matching_image_and_unrelated_later_change_pass_without_writes(self):
        self.promote()
        self.write("docs/unrelated.md", "documentation")
        self.commit("docs")
        self.assertEqual(self.check()["action"], "current_source_verified")

    def test_source_only_pr_can_merge_and_trigger_next_build(self):
        self.write("services/inventory/src/bravo_inventory/scan.py", "version = 2\n")
        self.commit("source changed")
        self.assertEqual(self.check()["action"], "no_promotion")

    def test_delivery_validator_change_does_not_stale_unchanged_image_contents(self):
        self.promote()
        self.write("scripts/registration_delivery.py", "# updated delivery validator\n")
        self.commit("later queue entry updates delivery validation")
        self.assertEqual(self.check()["action"], "current_source_verified")

    def test_prior_unmerged_queue_promotion_is_checked_against_combined_source(self):
        previous_group = self.promote()
        self.write("services/inventory/src/bravo_inventory/scan.py", "version = 2\n")
        self.commit("later queue entry")
        # The immediate queue base already contains the promotion, but develop does not.
        self.assertEqual(self.git("diff", "--name-only", previous_group, "HEAD", "--", self.service.values), "")
        with self.assertRaisesRegex(delivery.Failure, "promotion_source_superseded"):
            self.check()

    def test_publication_and_values_must_match(self):
        self.promote()
        values = delivery.promotion_values(self.receipt, self.service)
        values["image"]["digest"] = "sha256:" + "c" * 64
        self.write(self.service.values, json.dumps(values))
        self.commit("mismatched digest")
        with self.assertRaisesRegex(delivery.Failure, "publication_values_mismatch"):
            self.check()

    def test_missing_develop_history_fails_closed(self):
        self.promote()
        self.git("update-ref", "-d", "refs/remotes/origin/develop")
        with self.assertRaises(subprocess.CalledProcessError):
            self.check()

    def test_source_and_image_rollback_together_passes(self):
        first_promotion = self.promote()
        self.write("services/inventory/src/bravo_inventory/scan.py", "version = 2\n")
        second_source = self.commit("second source")
        self.receipt = self.receipt | {"source_revision": second_source,
                                       "image_digest": "sha256:" + "c" * 64}
        deployed = self.promote()
        self.git("update-ref", "refs/remotes/origin/develop", deployed)
        self.git("restore", "--source", first_promotion, "--", ".")
        self.commit("revert source and verified image")
        self.assertEqual(self.check()["source_revision"], self.source)


class ReaderPromotionSource(PromotionSource):
    service = delivery.PRODUCT_READER


class Publication(unittest.TestCase):
    def test_rejects_untrusted_event_or_branch(self):
        for key, value in (
            ("GITHUB_EVENT_NAME", "pull_request_target"),
            ("GITHUB_REF", "refs/heads/feature/T43-test"),
            ("GITHUB_REPOSITORY", "other/platform"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, ENV | {key: value}, clear=True),
                self.assertRaises(delivery.Failure),
            ):
                delivery.workflow_boundary()

    def test_public_package_and_changed_tag_never_copy(self):
        for visibility, digest in (("public", None), ("private", "sha256:" + "c" * 64)):
            with (
                patch.dict(os.environ, ENV),
                patch.object(delivery, "package_visibility", return_value=visibility),
                patch.object(delivery, "remote_digest", return_value=digest),
                patch.object(delivery, "run") as run,
            ):
                with self.assertRaises(delivery.Failure):
                    delivery.publish(Path("unused"), RECEIPT)
                run.assert_not_called()

    def test_unknown_copy_response_reads_same_tag_without_republish(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(delivery, "package_visibility", return_value="private"),
            patch.object(
                delivery, "remote_digest", side_effect=[None, DIGEST, DIGEST]
            ) as read,
            patch.object(
                delivery, "run", return_value=subprocess.CompletedProcess([], 1)
            ) as copy,
        ):
            result = delivery.publish(Path("unused"), RECEIPT)
        self.assertTrue(result["published"])
        self.assertFalse(result["deployed"])
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(
            {call.args[0] for call in read.call_args_list}, {"sha-" + SOURCE + "-42-1"}
        )

    def test_existing_exact_image_is_not_pushed_again(self):
        with (
            patch.dict(os.environ, ENV),
            patch.object(delivery, "package_visibility", return_value="private"),
            patch.object(delivery, "remote_digest", return_value=DIGEST),
            patch.object(delivery, "run") as copy,
        ):
            self.assertTrue(delivery.publish(Path("unused"), RECEIPT)["published"])
            copy.assert_not_called()

    def test_unpublished_or_foreign_receipt_cannot_promote(self):
        valid = RECEIPT | {
            "published": True,
            "registry_visibility": "private",
            "deployed": False,
        }
        self.assertEqual(delivery.promotion_values(valid)["image"]["digest"], DIGEST)
        for field, value in (
            ("published", False),
            ("registry_visibility", "public"),
            ("image", "ghcr.io/other/image"),
            ("image_digest", "latest"),
        ):
            with self.subTest(field=field), self.assertRaises(delivery.Failure):
                delivery.promotion_values(valid | {field: value})

    def test_scan_for_another_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "source-manifest.json").write_text(
                json.dumps({"source_revision": SOURCE})
            )
            blobs = root / "image-layout/blobs/sha256"
            blobs.mkdir(parents=True)
            platform_digest = "sha256:" + "c" * 64
            (blobs / DIGEST[7:]).write_text(
                json.dumps(
                    {
                        "manifests": [
                            {
                                "platform": {"os": "linux", "architecture": "amd64"},
                                "digest": platform_digest,
                            }
                        ]
                    }
                )
            )
            (blobs / platform_digest[7:]).write_text(
                json.dumps({"config": {"digest": "sha256:" + "d" * 64}})
            )
            (root / "image-scan.json").write_text(
                json.dumps(
                    {"ArtifactType": "container_image", "Metadata": {"ImageID": DIGEST}}
                )
            )
            with (
                patch.object(delivery, "verify_layout", return_value={}),
                self.assertRaisesRegex(
                    delivery.Failure, "scan_image_identity_mismatch"
                ),
            ):
                delivery.verify(root, SOURCE, DIGEST)


class Checkout(unittest.TestCase):
    def catalog(self, repo="https://github.com/example/public"):
        return {
            "releases": [
                {"repo_url": repo, "revision": SOURCE, "checkout": "/checkout/repo"}
            ]
        }

    def test_rejects_credentials_local_paths_and_commands(self):
        credential = "dummy"
        for repo in (
            "file:///tmp/repo",
            f"https://{credential}@github.com/a/b",
            "ext::command",
            "https://github.com/a/b?token=x",
            "https://example.invalid/a/b",
        ):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                checkout.approved_release(self.catalog(repo))

    def test_interrupted_init_can_resume_without_replacing_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "repo"
            subprocess.run(
                ["git", "init", str(target)], check=True, capture_output=True
            )
            original = subprocess.run

            def interrupted(args, **kwargs):
                if "fetch" in args:
                    raise subprocess.TimeoutExpired(args, 60)
                return original(args, **kwargs)

            with (
                patch.object(checkout.subprocess, "run", side_effect=interrupted),
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                checkout.fetch(self.catalog(), target)
            origin = original(
                ["git", "-C", str(target), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                origin.stdout.strip(), self.catalog()["releases"][0]["repo_url"]
            )
            marker = target / "unrelated"
            marker.write_text("preserve")
            with self.assertRaises(ValueError):
                checkout.fetch(self.catalog("https://github.com/other/repo"), target)
            self.assertEqual(marker.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
