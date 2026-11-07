"""Happy-path driver: follows design 7.6/8.2, never approves, never repeats an unknown step."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import happy_path as hp


def git(*_a, **_k):
    return SimpleNamespace(stdout="a" * 40 + "\n")


class Driver(unittest.TestCase):
    def setUp(self):
        self.record = Path(tempfile.mkdtemp()) / "run.jsonl"
        self.calls, self.codes, self.timeouts, self.state = [], {}, [], "COLLECT"

    def fake_run(self, argv, **kwargs):
        self.timeouts.append(kwargs["timeout"])
        if argv[:len(hp.INITIAL)] == list(hp.INITIAL):
            self.calls.append("initial:" + argv[len(hp.INITIAL)])
        else:
            self.calls.append(argv[1].rsplit("/", 1)[-1] + ":" + (argv[2] if argv[2] in ("check", "inject", "clear", "prepare-change", "prepare-cutover")
                                                                  else "-"))
        code = self.codes.get(self.calls[-1], 0)
        if code == "lost":
            raise OSError("lost")
        return SimpleNamespace(returncode=code, stdout=json.dumps({"ok": code == 0}) + "\n")

    def run_driver(self, *extra, read_state="default", wall_clock=lambda: 0):
        args = hp.main.__globals__["argparse"].Namespace(
            plan_id="plan-1", record=str(self.record), profile="mock", plan_state_command=None, kubeconfig="/k",
            application_id="app-1", receipts="/r", fault_record="/f", wait=0, interval=1, step_timeout=5,
            initial_args=["--from", "/i.json"], platform_context="platform", patch_preflight="/patch.json",
            patch_postflight="/post.json", migration_preflight="/migration.json", demo_budget=270)
        for k, v in extra:
            setattr(args, k, v)
        reader = (lambda _p: self.state) if read_state == "default" else read_state
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hp.cmd_run(args, run=self.fake_run, sleep=lambda _s: None, clock=lambda: 0,
                              wall_clock=wall_clock, read_state=reader, git=git)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def confirm(self, step):
        with contextlib.redirect_stdout(io.StringIO()):
            return hp.main(["confirm", "--record", str(self.record), "--step", step, "--by", "tester"])

    def test_sequence_follows_design_states_and_human_points(self):
        reaches = [s.reach for s in hp.STEPS if s.kind == "observe"]
        self.assertEqual(reaches, sorted(reaches, key=hp.ORDER.index))
        humans = [s.id for s in hp.STEPS if s.kind == "human"]
        ids = [s.id for s in hp.STEPS]
        self.assertEqual(humans, ["s2-candidate-import", "s2-change-approval", "s3-load",
                                  "s3-attachment-copy", "s3-shift-approval", "s3-cutover-approval", "s3-reverse-replication", "s4-alert-seen",
                                  "s4-failback-approval", "wrap-map"])
        self.assertLess(ids.index("s2-ai-collect"), ids.index("s2-candidate-import"))
        self.assertLess(ids.index("s2-candidate-import"), ids.index("s2-proposed"))
        self.assertLess(ids.index("s4-fault-clear"), ids.index("s4-failback-approval"))

    def test_header_records_run_identity(self):
        self.run_driver()
        head = json.loads(self.record.read_text().splitlines()[0])
        self.assertEqual((head["plan_id"], head["profile"], head["develop_sha"]), ("plan-1", "mock", "a" * 40))
        self.assertEqual(set(head["gitops_pins"]), {"registration", "product-reader", "rehearsal-host"})

    def test_waits_for_plan_state_then_human_without_approving(self):
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["reason"]), (10, "s1-analyzed", "plan_not_yet"))
        self.state = "PROPOSE"
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["reason"]), (10, "s2-candidate-import", "awaiting_human"))
        self.assertEqual(self.calls, ["demo_reset.py:check", "initial:start", "initial:collect"])
        self.assertEqual(self.confirm("s2-candidate-import"), 0)
        self.state = "REHEARSE"
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["reason"]), (10, "s2-change-approval", "awaiting_human"))
        self.assertIn("run.py:prepare-change", self.calls)
        self.assertEqual(self.calls.count("demo_reset.py:check"), 1)  # preflight ran once only
        self.assertEqual(self.confirm("s2-change-approval"), 0)
        self.state = "AWAIT_APPROVAL"
        code, line = self.run_driver()
        self.assertEqual(line["step"], "s3-load")
        self.assertIn("open_after_apply.py:-", self.calls)

    def test_load_and_attachment_preparation_precede_transition_approvals(self):
        ids = [step.id for step in hp.STEPS]
        self.assertLess(ids.index("s3-load"), ids.index("s3-shift-approval"))
        self.assertLess(ids.index("s3-attachment-copy"), ids.index("s3-cutover-approval"))

    def test_migration_never_runs_fault_or_failback(self):
        ids = [step.id for step in hp.selected_steps("migration")]
        self.assertIn("s3-cutover-done", ids)
        self.assertEqual(ids[-1], "s3-reverse-replication")
        self.assertFalse(any(x.startswith("s4-") for x in ids))

    def test_mock_record_cannot_be_resumed_as_real(self):
        self.run_driver()
        code, result = self.run_driver(("profile", "real"))
        self.assertEqual(code, 2)
        self.assertIn("profile", result["reason"])

    def test_migration_requires_current_watch_or_done(self):
        self.run_driver(("scenario", "migration"))
        for step in hp.selected_steps("migration"):
            hp.append(self.record, {"step": step.id, "status": "done"})
        self.state = "DONE_ROLLED_BACK"
        code, result = self.run_driver(("scenario", "migration"))
        self.assertEqual((code, result["reason"]), (1, "forward_migration_not_current"))
        self.state = "WATCH"
        code, result = self.run_driver(("scenario", "migration"))
        self.assertEqual((code, result["scenario"], result["final_state"]), (0, "migration", "WATCH"))

    def test_done_is_forward_terminal_but_never_failback_evidence(self):
        self.assertTrue(hp.reached("DONE", "WATCH"))
        self.assertFalse(hp.reached("DONE", "DONE_ROLLED_BACK"))
        self.run_driver()
        for step in hp.STEPS:
            hp.append(self.record, {"step": step.id, "status": "done"})
        self.state = "WATCH"
        code, result = self.run_driver()
        self.assertEqual((code, result["reason"]), (1, "failback_not_current"))

    def test_record_cannot_switch_to_a_shorter_scenario(self):
        self.run_driver()
        self.assertEqual(self.run_driver(("scenario", "migration"))[0], 2)

    def test_unwired_steps_stop_closed(self):
        self.state = "PROPOSE"
        code, line = self.run_driver(("initial_args", None))
        self.assertEqual((code, line["step"], line["reason"]), (3, "s2-ai-start", "initial_args_unwired"))
        self.assertEqual(self.run_driver(read_state=None)[0], 3)

    def test_default_plan_reader_uses_the_platform_context(self):
        self.assertIn("bravo_platform_events.plan_state", hp.PLAN_STATE)
        self.assertEqual(hp.PLAN_STATE[:3], ("kubectl", "--context", "{context}"))

    def test_security_program_is_required_before_change_approval(self):
        self.state = "PROPOSE"
        self.run_driver()
        self.confirm("s2-candidate-import")
        self.state = "REHEARSE"
        code, line = self.run_driver(("patch_preflight", None))
        self.assertEqual((code, line["step"], line["reason"]),
                         (3, "s2-security-prepare", "security_program_unwired"))

    def test_whole_demo_stops_after_270_second_budget(self):
        code, line = self.run_driver(("demo_budget", 270), wall_clock=lambda: 0)
        self.assertEqual((code, line["step"]), (10, "s1-analyzed"))
        code, line = self.run_driver(("demo_budget", 270), wall_clock=lambda: 271)
        self.assertEqual((code, line["reason"], line["budget_seconds"]),
                         (1, "demo_budget_exceeded", 270))
        # #939 review 3: the real plan state is read once (read-only) and kept in the record
        self.assertEqual(line["plan_state_at_budget"], "COLLECT")
        last = json.loads(self.record.read_text().splitlines()[-1])
        self.assertEqual((last["reason"], last["plan_state_at_budget"]), ("demo_budget_exceeded", "COLLECT"))

    def test_budget_overrun_after_every_step_still_records_the_real_plan_state(self):
        self.run_driver()
        for step in hp.STEPS:
            hp.append(self.record, {"step": step.id, "status": "done"})
        self.state = "DONE_ROLLED_BACK"
        code, line = self.run_driver(wall_clock=lambda: 271)
        self.assertEqual((code, line["reason"], line["plan_state_at_budget"]),
                         (1, "demo_budget_exceeded", "DONE_ROLLED_BACK"))
        last = json.loads(self.record.read_text().splitlines()[-1])
        self.assertEqual((last["step"], last["plan_state_at_budget"]), ("demo-budget", "DONE_ROLLED_BACK"))

    def test_an_unreadable_plan_at_the_budget_is_reported_not_assumed(self):
        def broken(_plan):
            raise RuntimeError("plan read failed")
        self.run_driver()
        code, line = self.run_driver(wall_clock=lambda: 271, read_state=broken)
        self.assertEqual((code, line["plan_state_at_budget"]), (1, "unconfirmed"))

    def test_cutover_security_is_prepared_before_the_cutover_approval(self):
        # #939 review 2: the cutover authority needs patch/postflight and migration/preflight registrations
        ids = [step.id for step in hp.STEPS]
        self.assertLess(ids.index("s3-shift-approval"), ids.index("s3-security-prepare"))
        self.assertLess(ids.index("s3-security-prepare"), ids.index("s3-cutover-approval"))
        self.assertIn("s3-security-prepare", [step.id for step in hp.selected_steps("migration")])
        step = hp.STEPS[ids.index("s3-security-prepare")]
        args = hp.main.__globals__["argparse"].Namespace(
            plan_id="plan-1", kubeconfig="/k", application_id="app-1", receipts="/r", fault_record="/f",
            platform_context="platform", patch_preflight="/patch.json", patch_postflight=None,
            migration_preflight="/migration.json", step_timeout=5, initial_args=None)
        code, detail = hp.run_step(step, args, None, self.fake_run, lambda _s: None, lambda: 0)
        self.assertEqual((code, detail["reason"], detail["missing"]),
                         (3, "security_program_unwired", ["patch_postflight"]))
        self.assertEqual(self.calls, [])
        args.patch_postflight = "/post.json"
        code, _ = hp.run_step(step, args, None, self.fake_run, lambda _s: None, lambda: 0)
        self.assertEqual((code, self.calls), (0, ["run.py:prepare-cutover"]))

    def test_each_command_timeout_is_capped_by_remaining_demo_budget(self):
        self.run_driver(("demo_budget", 3), wall_clock=lambda: 0)
        self.assertEqual(self.timeouts[0], 3)

    def test_demo_budget_cannot_be_disabled(self):
        with self.assertRaises(SystemExit):
            hp.main(["run", "--plan-id", "p", "--record", str(self.record),
                     "--profile", "mock", "--demo-budget", "0"])

    def test_unknown_or_failed_step_is_never_repeated(self):
        self.codes["demo_reset.py:check"] = "lost"
        self.assertEqual(self.run_driver()[0], 3)
        self.assertEqual(self.run_driver()[0], 3)
        self.assertEqual(self.calls, ["demo_reset.py:check"])
        with contextlib.redirect_stdout(io.StringIO()):
            hp.main(["resolve", "--record", str(self.record), "--step", "preflight", "--by", "t", "--status", "done"])
        self.assertEqual(self.run_driver()[1]["step"], "s1-analyzed")

    def test_stop_states_fail_and_confirm_only_human_steps(self):
        self.run_driver()
        self.state = "SAFE_HOLD"
        self.assertEqual(self.run_driver()[0], 1)
        self.assertEqual(self.confirm("preflight"), 2)

    def test_other_plan_record_is_refused(self):
        self.run_driver()
        self.assertEqual(self.run_driver(("plan_id", "plan-2"))[0], 2)


    def test_running_job_waits_and_hold_stops_for_a_person(self):
        self.state = "PROPOSE"
        self.codes["initial:start"] = 10
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["result"]), (10, "s2-ai-start", "waiting"))  # not failed
        self.codes["initial:start"] = 0
        self.codes["initial:collect"] = 10
        code, line = self.run_driver()
        self.assertEqual((code, line["step"]), (10, "s2-ai-collect"))
        self.assertEqual(self.calls.count("initial:start"), 2)  # rerun of the idempotent step only
        self.codes["initial:collect"] = 3
        code, line = self.run_driver()
        self.assertEqual((code, line["step"]), (3, "s2-ai-collect"))  # hold: a person resolves it


if __name__ == "__main__":
    unittest.main()


class InitialArgs(unittest.TestCase):
    """kth4778 11/7: run.py arguments start with "--"; both spellings must reach it unchanged."""

    def parsed(self, *extra):
        seen = {}
        original = hp.cmd_run
        hp.cmd_run = lambda args: seen.setdefault("args", args) and 0
        try:
            hp.main(["run", "--plan-id", "p", "--record", "/r.jsonl", "--profile", "mock", *extra])
        finally:
            hp.cmd_run = original
        return seen["args"].initial_args

    def test_spaced_and_joined_forms_pass_through(self):
        self.assertEqual(self.parsed("--initial-args", "--from", "initial.json"), ["--from", "initial.json"])
        self.assertEqual(self.parsed("--initial-args=--from=initial.json"), ["--from=initial.json"])
        self.assertEqual(self.parsed("--initial-args", "--from", "i.json", "--legacy-intent", "x.json"),
                         ["--from", "i.json", "--legacy-intent", "x.json"])

    def test_empty_initial_args_is_refused(self):
        with self.assertRaises(SystemExit):
            self.parsed("--initial-args")
