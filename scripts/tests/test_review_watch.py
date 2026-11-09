"""Review arrivals remain read-only and cannot skip events on a failed poll."""
import contextlib
import copy
import datetime as dt
import importlib
import io
import json
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
watch = importlib.import_module("review_watch")
NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
AUTHOR = "example-author"


def pr(number=7, state="open", author=AUTHOR):
    return {"number": number, "state": state, "user": {"login": author}, "pull_request": {}}


def review(identifier=10, state="APPROVED", **kwargs):
    return {"id": identifier, "state": state, "user": {"login": "example-reviewer"},
            "submitted_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "commit_id": "a" * 40, "body": "PRIVATE_DO_NOT_RETAIN", "diff_hunk": "PRIVATE_DO_NOT_RETAIN", **kwargs}


class Fixture:
    def __init__(self):
        self.open = [pr()]
        self.updated = [pr()]
        self.records = {"pulls/7/reviews": [review()], "pulls/7/comments": []}
        self.calls = []
        self.api = Mock(return_value=pr(state="closed"))

    def pages(self, path):
        self.calls.append(path)
        if path.startswith("pulls?"):
            return copy.deepcopy(self.open)
        if path.startswith("issues?"):
            return copy.deepcopy(self.updated)
        result = self.records[path]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class ArrivalTest(unittest.TestCase):
    def test_first_poll_baseline_then_deduplicates_and_detects_new_review(self):
        client = Fixture()
        report, cursor = watch.collect(client, AUTHOR, None, NOW)
        self.assertEqual(report["status"], "BASELINE")
        self.assertEqual(report["events"][0]["arrival"], "EXISTING")
        report, cursor = watch.collect(client, AUTHOR, cursor, NOW)
        self.assertEqual(report["events"], [])
        client.records["pulls/7/reviews"].append(review(11, "CHANGES_REQUESTED"))
        report, _ = watch.collect(client, AUTHOR, cursor, NOW)
        self.assertEqual([(item["arrival"], item["state"]) for item in report["events"]], [("NEW", "CHANGES_REQUESTED")])

    def test_pending_submission_inline_edit_and_dismissal(self):
        client = Fixture()
        client.records["pulls/7/reviews"] = [review(state="PENDING", submitted_at=None)]
        client.records["pulls/7/comments"] = [review(21)]
        report, cursor = watch.collect(client, AUTHOR, None, NOW)
        self.assertEqual(len(report["events"]), 1)
        client.records["pulls/7/reviews"] = [review(state="COMMENTED")]
        client.records["pulls/7/comments"][0]["updated_at"] = "2026-01-01T00:01:00Z"
        report, cursor = watch.collect(client, AUTHOR, cursor, NOW)
        self.assertEqual({item["arrival"] for item in report["events"]}, {"NEW", "UPDATED"})
        client.records["pulls/7/reviews"][0]["state"] = "DISMISSED"
        report, _ = watch.collect(client, AUTHOR, cursor, NOW)
        self.assertEqual(report["events"][0]["state"], "DISMISSED")

    def test_merged_between_polls_and_new_already_closed_pr_are_discovered(self):
        client = Fixture()
        _, cursor = watch.collect(client, AUTHOR, None, NOW)
        client.open = []
        client.updated = [pr(state="closed"), pr(8, "closed")]
        client.records["pulls/7/reviews"].append(review(11))
        client.records["pulls/8/reviews"] = [review(12)]
        client.records["pulls/8/comments"] = []
        report, cursor = watch.collect(client, AUTHOR, cursor, NOW + dt.timedelta(minutes=1))
        self.assertEqual({item["pr"] for item in report["events"]}, {7, 8})
        self.assertEqual(cursor["open_prs"], [])
        self.assertIn("since=2025-12-31T23%3A50%3A00Z", [path for path in client.calls if path.startswith("issues?")][-1])

    def test_old_open_pr_rechecked_when_discovery_lags(self):
        client = Fixture()
        _, cursor = watch.collect(client, AUTHOR, None, NOW)
        client.open = client.updated = []
        client.records["pulls/7/reviews"].append(review(11))
        report, _ = watch.collect(client, AUTHOR, cursor, NOW)
        client.api.assert_called_once_with("pulls/7")
        self.assertEqual(report["events"][0]["id"], 11)

    def test_unrelated_issues_and_other_authors_not_collected(self):
        client = Fixture()
        client.open += [pr(9, author="another-author")]
        client.updated += [pr(10, author="another-author"), {"number": 11, "user": {"login": AUTHOR}}]
        report, _ = watch.collect(client, AUTHOR, None, NOW)
        self.assertEqual(report["checked_prs"], [7])
        self.assertFalse(any("/9/" in path or "/10/" in path or "/11/" in path for path in client.calls))

    def test_unknown_state_and_foreign_old_pr_fail_closed(self):
        client = Fixture()
        _, cursor = watch.collect(client, AUTHOR, None, NOW)
        client.records["pulls/7/reviews"][0]["state"] = "NEW_UNSUPPORTED_STATE"
        with self.assertRaises(watch.PacketError):
            watch.collect(client, AUTHOR, cursor, NOW)
        client.open = client.updated = []
        client.api.return_value = pr(author="another-author")
        with self.assertRaises(watch.PacketError):
            watch.collect(client, AUTHOR, cursor, NOW)

    def test_malformed_author_fails_closed(self):
        client = Fixture()
        client.open[0]["user"] = "unsupported"
        with self.assertRaises(watch.PacketError):
            watch.collect(client, AUTHOR, None, NOW)
        with self.assertRaises(watch.PacketError):
            watch.event(review(user="unsupported"), 7, "review")

    def test_output_has_no_bodies_diff_or_api_user_ids(self):
        client = Fixture()
        client.records["pulls/7/reviews"][0]["user"]["id"] = 987654321
        report, cursor = watch.collect(client, AUTHOR, None, NOW)
        output = json.dumps([report, cursor])
        for secret in ("PRIVATE_DO_NOT_RETAIN", "diff_hunk", "987654321"):
            self.assertNotIn(secret, output)
        self.assertFalse(any(word in path for path in client.calls for word in ("files", "check-runs", "statuses", "rules")))

    def test_partial_failure_does_not_mutate_input_cursor(self):
        client = Fixture()
        _, cursor = watch.collect(client, AUTHOR, None, NOW)
        before = copy.deepcopy(cursor)
        client.records["pulls/7/reviews"].append(review(11))
        client.records["pulls/7/comments"] = watch.PacketError("API unavailable")
        with self.assertRaises(watch.PacketError):
            watch.collect(client, AUTHOR, cursor, NOW)
        self.assertEqual(cursor, before)


class StateAndCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cursor.json"
        self.cursor = watch.collect(Fixture(), AUTHOR, None, NOW)[1]

    def test_private_atomic_cursor_and_identity_binding(self):
        watch.save_cursor(self.path, self.cursor)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(watch.read_cursor(self.path, AUTHOR), self.cursor)
        with self.assertRaises(watch.PacketError):
            watch.read_cursor(self.path, "different-user")
        self.assertEqual(list(self.path.parent.glob(".review-watch-*")), [])
        self.path.chmod(0o644)
        with self.assertRaises(watch.PacketError):
            watch.read_cursor(self.path, AUTHOR)

    def test_symlink_and_repository_state_rejected(self):
        other = self.path.parent / "target"
        other.write_text("unchanged")
        self.path.symlink_to(other)
        with self.assertRaises(watch.PacketError):
            watch.state_path(str(self.path), AUTHOR)
        with self.assertRaises(watch.PacketError):
            watch.state_path(str(watch.ROOT / "cursor.json"), AUTHOR)
        self.assertEqual(other.read_text(), "unchanged")

    def test_other_git_repository_rejected(self):
        subprocess.run(["git", "init", "-q", self.temp.name], check=True)
        with self.assertRaises(watch.PacketError):
            watch.state_path(str(self.path), AUTHOR)

    def test_private_directory_required_and_lock_exclusive(self):
        self.path.parent.chmod(0o755)
        with self.assertRaises(watch.PacketError):
            watch.state_path(str(self.path), AUTHOR)
        self.path.parent.chmod(0o700)
        self.assertEqual(watch.state_path(str(self.path), AUTHOR), self.path)
        with watch.cursor_lock(self.path):
            with self.assertRaises(watch.PacketError), watch.cursor_lock(self.path):
                pass
        self.assertEqual(stat.S_IMODE(self.path.with_suffix(".json.lock").stat().st_mode), 0o600)

    def test_cursor_unchanged_on_actual_cli_api_failure(self):
        watch.save_cursor(self.path, self.cursor)
        before = self.path.read_bytes()
        client = Fixture()
        client.viewer = Mock(return_value=AUTHOR)
        client.records["pulls/7/comments"] = watch.PacketError("private raw failure must not print")
        output = io.StringIO()
        with patch.object(watch, "ReviewGitHub", return_value=client), contextlib.redirect_stdout(output):
            result = watch.main(["--once", "--json", "--state-file", str(self.path)])
        self.assertEqual(result, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads(output.getvalue())["status"], "UNKNOWN")
        self.assertNotIn("private raw", output.getvalue())

    def test_first_cli_poll_never_notifies_and_second_event_does(self):
        client = Fixture()
        client.viewer = Mock(return_value=AUTHOR)
        with patch.object(watch, "ReviewGitHub", return_value=client), patch.object(watch.sys, "platform", "darwin"), \
                patch.object(watch, "notify") as notification, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch.main(["--notify", "--state-file", str(self.path)]), 0)
            notification.assert_not_called()
            client.records["pulls/7/reviews"].append(review(11))
            self.assertEqual(watch.main(["--notify", "--state-file", str(self.path)]), 0)
            notification.assert_called_once()

    def test_watch_opt_in_minimum_interval_and_no_repository_override(self):
        self.assertFalse(watch.parser().parse_args([]).watch)
        for args in (["--interval", "59"], ["--repo", "foreign/repository"], ["--once", "--watch"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                watch.main(args)

    def test_osascript_never_interpolates_message(self):
        payload = '" & do shell script "touch /tmp/not-executed" & "'
        with patch.object(watch.subprocess, "run", return_value=Mock(returncode=0)) as run:
            watch.notify(payload)
        argv = run.call_args.args[0]
        self.assertEqual(argv[2], watch.NOTIFY_SCRIPT)
        self.assertNotIn(payload, argv[2])
        self.assertEqual(argv[3], payload)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_transport_uses_fixed_host_and_get_only(self):
        client = watch.ReviewGitHub()
        with patch.object(client, "call", return_value={"login": AUTHOR}) as call:
            self.assertEqual(client.viewer(), AUTHOR)
            client.api("pulls/7/reviews")
        for args, _ in call.call_args_list:
            self.assertEqual(args[:5], ("api", "--hostname", "github.com", "--method", "GET"))

    def test_local_alias_install_and_overwrite_refusal(self):
        def run(*args, **kwargs):
            command = args[0]
            self.assertNotIn("--global", command)
            if "remote" in command:
                return Mock(returncode=0, stdout=f"git@github.com:{watch.REPO}.git\n")
            if "--get-all" in command:
                return Mock(returncode=1, stdout="")
            return Mock(returncode=0)
        with patch.object(watch.subprocess, "run", side_effect=run) as process:
            watch.install_alias()
            expected = "!python3 " + shlex.quote(str(watch.ROOT / "scripts/review_watch.py"))
            self.assertEqual(process.call_args.args[0][-4:], ["config", "--local", "alias.review-watch", expected])
        with patch.object(watch.subprocess, "run", side_effect=[
            Mock(returncode=0, stdout=f"https://github.com/{watch.REPO}"), Mock(returncode=0, stdout="!custom-tool")
        ]) as process:
            with self.assertRaises(watch.PacketError):
                watch.install_alias()
            self.assertEqual(process.call_count, 2)

    def test_alias_quotes_checkout_spaces_and_shell_characters(self):
        root = Path(self.temp.name) / "platform tools/'checkout;$(do-not-run)"
        with patch.object(watch, "ROOT", root), patch.object(watch.subprocess, "run", side_effect=[
            Mock(returncode=0, stdout=f"https://github.com/{watch.REPO}"),
            Mock(returncode=1, stdout=""), Mock(returncode=0)
        ]) as process:
            watch.install_alias()
        alias = process.call_args.args[0][-1]
        self.assertEqual(shlex.split(alias[1:]), ["python3", str(root / "scripts/review_watch.py")])

    def test_wrong_origin_alias_setup_never_writes(self):
        with patch.object(watch.subprocess, "run", return_value=Mock(returncode=0, stdout="https://github.com/other/repo")) as process:
            with self.assertRaises(watch.PacketError):
                watch.install_alias()
            self.assertEqual(process.call_count, 1)


if __name__ == "__main__":
    unittest.main()
