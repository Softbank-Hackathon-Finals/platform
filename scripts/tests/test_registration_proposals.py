"""Exercise proposal replacement with real Git history and a bounded GitHub fake."""

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registration_delivery as delivery


class ProposalLifecycle(unittest.TestCase):
    service = delivery.REGISTRATION
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout"
        self.root.mkdir()
        self.remote = Path(self.temp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        self.write("services/inventory/src/scan.py", "version = 1\n")
        self.write(self.service.values, "{}")
        self.old = self.commit("old source")
        self.git("branch", "-M", "develop")
        self.write("services/inventory/src/scan.py", "version = 2\n")
        self.source = self.commit("new source")
        self.git("push", "-q", "origin", "develop")
        self.prs = {}
        self.writes = []
        self.create_fails = False
        self.close_fails = False
        self.lost_create_response = False
        self.lost_close_response = False
        self.receipt = self.publication(self.source)
        self.env = {
            "GITHUB_REPOSITORY": delivery.REPOSITORY,
            "GITHUB_REF": "refs/heads/develop",
            "GITHUB_EVENT_NAME": "push",
            "RUNNER_TEMP": self.temp.name,
        }

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args],
                                       text=True, stderr=subprocess.PIPE).strip()

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def publication(self, source):
        return {"source_revision": source, "image": self.service.image,
                "image_digest": "sha256:" + "b" * 64, "run_id": "42",
                "published": True, "registry_visibility": "private", "deployed": False}

    def add_pr(self, number, source, *, extra_file=False):
        branch = ("feature/T43-" + self.service.slug + "-image-") + source[:12]
        self.git("switch", "-qc", branch, source)
        receipt = self.publication(source)
        self.write(self.service.values, json.dumps(delivery.promotion_values(receipt, self.service)))
        self.write(str(Path(self.service.values).with_name(self.service.publication)), json.dumps(receipt))
        if extra_file:
            self.write("docs/manual.md", "human work")
        head = self.commit("image proposal")
        self.git("push", "-q", "origin", branch)
        self.record_pr(number, branch, head)
        self.git("switch", "-q", "develop")

    def record_pr(self, number, branch, head):
        self.prs[number] = {
            "number": number, "title": ("[T43] 검증된 " + self.service.title + " 이미지 배포"), "body": "Refs #355\n",
            "state": "open", "merged_at": None,
            "html_url": f"https://github.com/{delivery.REPOSITORY}/pull/{number}",
            "base": {"ref": "develop"},
            "head": {"ref": branch, "sha": head, "repo": {"full_name": delivery.REPOSITORY}},
        }

    def invoke(self, args, *, check=True):
        code, result = 0, ""
        if args[0] == "git":
            return subprocess.run(["git", "-C", str(self.root), *args[1:]],
                                  capture_output=True, text=True, check=check)
        if args[:3] == ["gh", "pr", "list"]:
            branch = args[args.index("--head") + 1]
            result = [{"number": p["number"], "url": p["html_url"], "state": p["state"].upper()}
                      for p in self.prs.values() if p["head"]["ref"] == branch]
        elif args[:3] == ["gh", "pr", "create"]:
            self.writes.append("create")
            branch = args[args.index("--head") + 1]
            if not self.create_fails:
                self.record_pr(999, branch, self.git("rev-parse", branch))
            code = int(self.create_fails or self.lost_create_response)
        elif args[:2] == ["gh", "api"]:
            path = args[2].removeprefix("repos/" + delivery.REPOSITORY + "/")
            if path.startswith("pulls?"):
                # Multiple pages ensure cleanup never silently ignores older PRs.
                result = [[copy.deepcopy(p)] for p in self.prs.values() if p["state"] == "open"]
            elif path.startswith("contents/"):
                name, head = path.removeprefix("contents/").split("?ref=")
                result = {"content": base64.b64encode(self.git("show", head + ":" + name).encode()).decode()}
            elif path.startswith("pulls/"):
                number = int(path.split("/")[1])
                pr = self.prs[number]
                if "/files?" in path:
                    base = self.git("merge-base", "develop", pr["head"]["sha"])
                    names = self.git("diff", "--name-only", base, pr["head"]["sha"]).splitlines()
                    result = [[{"filename": name, "status": "modified"} for name in names]]
                elif "PATCH" in args:
                    self.writes.append(number)
                    if not self.close_fails:
                        pr["state"] = "closed"
                    code = int(self.close_fails or self.lost_close_response)
                    result = pr
                else:
                    result = copy.deepcopy(pr)
            else:
                self.fail("unexpected API endpoint: " + path)
        else:
            self.fail("unexpected command: " + repr(args))
        if code and check:
            raise delivery.Failure("fake_command_failed")
        return subprocess.CompletedProcess(args, code, json.dumps(result), "")

    def propose(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, self.env), patch.object(delivery, "run", side_effect=self.invoke):
                return delivery.propose(self.receipt, self.service)
        finally:
            os.chdir(previous)

    def test_new_proposal_closes_older_only_after_creation_and_readback(self):
        self.add_pr(10, self.old)
        result = self.propose()
        self.assertEqual(result["action"], "proposed")
        self.assertEqual(result["closed_proposals"], [10])
        self.assertEqual(self.writes, ["create", 10])
        self.assertEqual(self.prs[999]["state"], "open")

    def test_all_pages_of_older_generated_proposals_are_closed(self):
        middle = self.source
        self.write("services/inventory/src/scan.py", "version = 3\n")
        self.source = self.commit("latest source")
        self.receipt = self.publication(self.source)
        self.git("push", "-q", "origin", "develop")
        self.add_pr(10, self.old)
        self.add_pr(20, middle)
        self.assertEqual(self.propose()["closed_proposals"], [10, 20])
        self.assertEqual(self.writes, ["create", 10, 20])

    def test_failed_create_preserves_previous_proposal(self):
        self.add_pr(10, self.old)
        self.create_fails = True
        with self.assertRaisesRegex(delivery.Failure, "proposal_create_unknown"):
            self.propose()
        self.assertEqual(self.prs[10]["state"], "open")
        self.assertEqual(self.writes, ["create"])

    def test_lost_create_and_close_responses_are_resolved_without_repeating_writes(self):
        self.add_pr(10, self.old)
        self.lost_create_response = self.lost_close_response = True
        self.assertEqual(self.propose()["closed_proposals"], [10])
        self.assertEqual(self.writes, ["create", 10])
        self.assertEqual(self.propose()["closed_proposals"], [])
        self.assertEqual(self.writes, ["create", 10])

    def test_retry_existing_proposal_finishes_interrupted_cleanup(self):
        self.add_pr(10, self.old)
        self.close_fails = True
        with self.assertRaisesRegex(delivery.Failure, "proposal_close_unknown"):
            self.propose()
        self.close_fails = False
        result = self.propose()
        self.assertEqual(result["action"], "existing_proposal")
        self.assertEqual(result["closed_proposals"], [10])
        self.assertEqual(self.writes.count("create"), 1)

    def test_manual_changes_forks_and_other_base_are_not_closed(self):
        self.add_pr(10, self.old, extra_file=True)
        for number, change in ((11, "fork"), (12, "base"), (13, "title")):
            self.prs[number] = copy.deepcopy(self.prs[10])
            self.prs[number]["number"] = number
            if change == "fork":
                self.prs[number]["head"]["repo"]["full_name"] = "other/platform"
            elif change == "base":
                self.prs[number]["base"]["ref"] = "main"
            else:
                self.prs[number]["title"] = "manual promotion"
        self.assertEqual(self.propose()["closed_proposals"], [])
        self.assertTrue(all(self.prs[n]["state"] == "open" for n in (10, 11, 12, 13)))

    def test_lagging_build_is_still_proposed_with_its_lag_recorded(self):
        self.write("services/inventory/src/scan.py", "version = 3\n")
        self.commit("develop changes a build input after the build started")
        self.git("push", "-q", "origin", "develop")
        result = self.propose()
        self.assertEqual((result["action"], result["unreleased_input_commits"]), ("proposed", 1))
        self.assertIn("Unreleased build-input commits on develop at proposal time: 1",
                      (Path(self.temp.name) / (self.service.slug + "-proposal.md")).read_text())

    def test_late_build_cannot_replace_newer_proposal_with_identical_inputs(self):
        self.write("docs/new.md", "docs only")
        newer = self.commit("newer source, identical build inputs")
        self.git("push", "-q", "origin", "develop")
        self.add_pr(20, newer)
        self.assertEqual(self.propose()["action"], "superseded")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.prs[20]["state"], "open")

    def test_already_recorded_values_clean_older_proposal(self):
        self.add_pr(10, self.old)
        self.write(self.service.values, json.dumps(delivery.promotion_values(self.receipt, self.service)))
        self.write(str(Path(self.service.values).with_name(self.service.publication)), json.dumps(self.receipt))
        self.commit("already deployed")
        self.git("push", "-q", "origin", "develop")
        self.assertEqual(self.propose()["action"], "already_recorded")
        self.assertEqual(self.writes, [10])

    def test_closed_proposal_is_not_reopened_or_used_to_close_others(self):
        self.add_pr(10, self.old)
        self.add_pr(20, self.source)
        self.prs[20]["state"] = "closed"
        self.assertEqual(self.propose()["action"], "existing_proposal")
        self.assertEqual(self.writes, [])

    def test_malformed_receipt_stops_before_writes(self):
        self.add_pr(10, self.old)
        self.git("switch", "-q", self.prs[10]["head"]["ref"])
        self.write(str(Path(self.service.values).with_name(self.service.publication)), json.dumps(self.publication(self.source)))
        self.prs[10]["head"]["sha"] = self.commit("mismatched source")
        self.git("switch", "-q", "develop")
        with self.assertRaisesRegex(delivery.Failure, "proposal_identity_mismatch"):
            self.propose()
        self.assertEqual(self.writes, [])


class ReaderProposalLifecycle(ProposalLifecycle):
    service = delivery.PRODUCT_READER

    def test_reader_does_not_close_registration_proposals(self):
        self.service = delivery.REGISTRATION
        self.add_pr(10, self.old)
        self.service = delivery.PRODUCT_READER
        self.receipt = self.publication(self.source)
        self.assertEqual(self.propose()["closed_proposals"], [])
        self.assertEqual(self.prs[10]["state"], "open")


if __name__ == "__main__":
    unittest.main()
