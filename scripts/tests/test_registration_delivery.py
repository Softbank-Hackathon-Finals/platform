"""Publication refusals and recovery decisions without network credentials."""

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
        for repo in (
            "file:///tmp/repo",
            "https://token@github.com/a/b",
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
