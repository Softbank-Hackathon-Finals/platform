"""Recoverability and ownership boundaries for the operator suite adapter."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from team_runtime import journal, recovery


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = "team-" + "a" * 32
        self.ref = {
            "kind": "Namespace",
            "name": "rehearsal-test",
            "namespace": "",
            "uid": "original-uid",
        }
        journal.save(
            self.root / "journal.json",
            {"run_id": self.run, "suite": "isolation", "resources": [self.ref]},
        )

    def obj(self, uid="original-uid", run=None):
        return {
            "kind": "Namespace",
            "metadata": {
                "name": "rehearsal-test",
                "uid": uid,
                "labels": {journal.LABEL: run or self.run},
            },
        }

    def test_replacement_uid_is_never_deleted(self):
        with (
            patch.object(recovery, "read", return_value=self.obj(uid="replacement")),
            patch.object(recovery, "kubectl") as command,
        ):
            with self.assertRaisesRegex(journal.Failure, "identity_changed"):
                recovery.delete_one(self.ref, self.run)
            command.assert_not_called()

    def test_another_run_is_never_adopted(self):
        ref = {k: v for k, v in self.ref.items() if k != "uid"}
        with self.assertRaisesRegex(journal.Failure, "identity_changed"):
            journal.match(self.obj(run="different"), ref, self.run)

    def test_lost_create_response_recovers_only_matching_marker(self):
        journal.save(
            self.root / "journal.json",
            {
                "run_id": self.run,
                "resources": [{k: v for k, v in self.ref.items() if k != "uid"}],
            },
        )
        with patch.object(recovery, "read", return_value=self.obj()):
            recovery.inspect(self.root)
        value = json.loads((self.root / "journal.json").read_text())
        self.assertEqual(value["resources"][0]["uid"], "original-uid")

    def test_delete_uses_uid_and_confirms_absence(self):
        with (
            patch.object(recovery, "read", side_effect=[self.obj(), None]),
            patch.object(
                recovery, "kubectl", return_value=subprocess.CompletedProcess([], 0)
            ) as command,
        ):
            recovery.delete_one(self.ref, self.run)
        options = json.loads(command.call_args.args[1])
        self.assertEqual(options["preconditions"], {"uid": "original-uid"})
        self.assertEqual(options["propagationPolicy"], "Foreground")

    def test_unavailable_read_is_not_clean(self):
        with (
            patch.object(recovery, "read", side_effect=journal.Failure("unavailable")),
            self.assertRaises(journal.Failure),
        ):
            recovery.cleanup(self.root, {})
        self.assertNotIn(
            "cleanup", json.loads((self.root / "journal.json").read_text())
        )

    def test_global_foreign_namespaces_are_not_queried(self):
        with patch.object(recovery, "read", return_value=None) as read:
            self.assertEqual(recovery.cleanup(self.root, {}), "CLEAN")
        self.assertTrue(
            all(
                call.args[0]["name"] == "rehearsal-test" for call in read.call_args_list
            )
        )

    def test_journal_never_persists_secret_body(self):
        # Use JSON, which is also YAML. No parser or Kubernetes process dependency.
        import types

        yaml = types.SimpleNamespace(safe_load=json.loads)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "t15-cred-rehearsal-test",
                "namespace": "bravo-rehearsal-control",
            },
            "stringData": {"password": "must-not-persist"},
        }
        result = {
            **secret,
            "metadata": {
                **secret["metadata"],
                "uid": "secret-uid",
                "labels": {journal.LABEL: self.run},
            },
        }
        with (
            patch.dict(sys.modules, yaml=yaml),
            patch.dict(os.environ, BRAVO_RUN_ROOT=str(self.root)),
            patch("sys.stdin", io.StringIO(json.dumps(secret))),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
            patch.object(
                journal,
                "kubectl",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(result), ""),
            ),
        ):
            self.assertEqual(journal.shim(["create", "-f", "-", "-o", "json"]), 0)
        text = (self.root / "journal.json").read_text()
        self.assertNotIn("must-not-persist", text)
        self.assertNotIn("password", text)
        self.assertIn("secret-uid", text)

    def test_duplicate_intent_stops_before_mutation(self):
        import types

        obj = self.obj()
        with (
            patch.dict(sys.modules, yaml=types.SimpleNamespace(safe_load=json.loads)),
            patch.dict(os.environ, BRAVO_RUN_ROOT=str(self.root)),
            patch("sys.stdin", io.StringIO(json.dumps(obj))),
            patch.object(journal, "kubectl") as command,
        ):
            with self.assertRaisesRegex(journal.Failure, "intent_already_exists"):
                journal.shim(["create", "-f", "-", "-o", "json"])
            command.assert_not_called()

    def execute_cleanup(self, failure=False, subject="member"):
        import importlib.util
        import types

        spec = importlib.util.spec_from_file_location(
            "runtime_cli", Path(__file__).resolve().parents[1] / "team_runtime.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        root = self.root / self.run
        root.mkdir()
        record = {
            "run_id": self.run,
            "suite": "isolation",
            "subject": "member",
            "cluster_uid": "cluster",
            "execution": "RUNNING",
            "cleanup": "CLEAN",
            "resources": [],
        }
        journal.save(root / "journal.json", record)
        journal.save(root / "environment.json", {"cluster_uid": "cluster"})
        args = types.SimpleNamespace(
            command="cleanup", state=self.root, run_id=self.run
        )

        def clean(*_):
            if failure:
                raise journal.Failure("read_unavailable")
            with journal.locked(root) as value:
                value["cleanup"] = "CLEAN"

        with (
            patch.object(cli, "identity", return_value=subject),
            patch.object(cli, "activate"),
            patch.object(
                cli,
                "read_environment",
                side_effect=AssertionError("must use saved environment"),
            ),
            patch.object(
                cli,
                "kubectl",
                return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({"metadata": {"uid": "cluster"}})
                ),
            ),
            patch.object(cli, "cleanup", side_effect=clean) as cleanup,
            patch("sys.stdout", io.StringIO()),
        ):
            try:
                code = cli.execute(args)
            except journal.Failure:
                cleanup.assert_not_called()
                raise
        return code, json.loads((root / "journal.json").read_text())

    def test_cleanup_survives_missing_central_profile_and_marks_interruption(self):
        code, record = self.execute_cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(record["execution"], "INTERRUPTED")
        self.assertEqual(record["cleanup"], "CLEAN")
        self.assertNotIn("verdict", record)

    def test_prior_clean_does_not_mask_current_read_failure(self):
        code, record = self.execute_cleanup(failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(record["cleanup"], "UNCONFIRMED")

    def test_cleanup_cannot_switch_personal_subject(self):
        with self.assertRaisesRegex(journal.Failure, "owner_or_cluster"):
            self.execute_cleanup(subject="different-member")

    def test_modified_frozen_renderer_is_rejected(self):
        from team_runtime.recipes import write_delivery

        config = {"images": {"bao_image": "registry.invalid/bao@sha256:" + "a" * 64}}
        write_delivery(self.root, config)
        (self.root / "target-render.py").write_text("changed")
        with self.assertRaisesRegex(
            journal.Failure, "frozen_renderer_missing_or_changed"
        ):
            write_delivery(self.root, config)


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(RuntimeTest):
    """#466 review: bounded recovery attempts, capped node image lists, lock-holder shim writes."""

    def test_confirmed_failed_attempts_move_to_new_names_up_to_the_limit(self):
        seen = []

        def attempt(work):
            seen.append(work)
            if len(seen) < 3:
                raise recovery.Retry("recovery_input_missing")
            return recovery.DELETED

        self.assertEqual(recovery.bounded_recovery(self.root, "t15-cred-ns-recover", attempt), recovery.DELETED)
        self.assertEqual(seen, ["t15-cred-ns-recover", "t15-cred-ns-recover-r2", "t15-cred-ns-recover-r3"])
        self.assertEqual(json.loads((self.root / "journal.json").read_text())["recovery_attempt"], 3)

    def test_attempts_are_bounded_and_resume_from_the_journal(self):
        def failing(work):
            raise recovery.Retry("recovery_job_failed")

        with self.assertRaisesRegex(journal.Failure, "recovery_attempts_exhausted_recovery_job_failed"):
            recovery.bounded_recovery(self.root, "base", failing)
        seen = []
        recovery.bounded_recovery(self.root, "base", lambda work: seen.append(work) or recovery.DELETED)
        self.assertEqual(seen, ["base-r3"])  # restart resumes the recorded attempt, never attempt 1

    def test_pending_attempt_is_never_replaced(self):
        def pending(work):
            raise journal.Failure("recovery_job_pending_inspect_same_run")

        with self.assertRaisesRegex(journal.Failure, "pending"):
            recovery.bounded_recovery(self.root, "base", pending)
        self.assertNotIn("recovery_attempt", json.loads((self.root / "journal.json").read_text()))

    def test_capped_node_image_list_is_unconfirmed_not_missing(self):
        from team_runtime import recipes

        def node(images):
            return {"spec": {}, "status": {"nodeInfo": {"architecture": "arm64"},
                                           "images": [{"names": [f"big/{i}"]} for i in range(images)]}}

        config = {"cluster_uid": "u", "architecture": "arm64", "images": {"isolation_image": "localhost/probe@sha256:x"}}
        for images, reason in ((recipes.NODE_STATUS_MAX_IMAGES, "unconfirmed_list_capped"), (3, "local_image_missing")):
            replies = iter([{"metadata": {"uid": "u"}}, {"items": [node(images)]}])

            def kubectl(_args, *_a, replies=replies, **_k):
                return subprocess.CompletedProcess([], 0, json.dumps(next(replies)), "")
            with self.subTest(images=images), patch.object(recipes, "kubectl", kubectl), \
                    self.assertRaisesRegex(journal.Failure, reason):
                recipes.preflight(config, False)

    def test_status_under_contention_never_rewrites_the_shim(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "team_runtime_cli", Path(__file__).resolve().parents[1] / "team_runtime.py")
        runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runtime)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "kubectl").write_text("running execution shim")
        with patch.object(runtime.shutil, "which", return_value="/usr/bin/kubectl"), \
                patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            os.environ.pop("BRAVO_ORIGINAL_PATH", None)
            runtime.activate(self.root, write=False)
            self.assertEqual((self.root / "bin" / "kubectl").read_text(), "running execution shim")
            runtime.activate(self.root)
            self.assertIn("exec", (self.root / "bin" / "kubectl").read_text())
            self.assertFalse(list((self.root / "bin").glob(".*.tmp")))
