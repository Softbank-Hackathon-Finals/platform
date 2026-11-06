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
        self.calls, self.codes, self.state = [], {}, "COLLECT"

    def fake_run(self, argv, **_k):
        self.calls.append(argv[1].rsplit("/", 1)[-1] + ":" + (argv[2] if argv[2] in ("check", "inject", "clear") else "-"))
        code = self.codes.get(self.calls[-1], 0)
        if code == "lost":
            raise OSError("lost")
        return SimpleNamespace(returncode=code, stdout=json.dumps({"ok": code == 0}) + "\n")

    def run_driver(self, *extra, read_state="default"):
        args = hp.main.__globals__["argparse"].Namespace(
            plan_id="plan-1", record=str(self.record), profile="mock", plan_state_command=None, kubeconfig="/k",
            application_id="app-1", receipts="/r", fault_record="/f", wait=0, interval=1, step_timeout=5)
        for k, v in extra:
            setattr(args, k, v)
        reader = (lambda _p: self.state) if read_state == "default" else read_state
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = hp.cmd_run(args, run=self.fake_run, sleep=lambda _s: None, clock=lambda: 0,
                              read_state=reader, git=git)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def confirm(self, step):
        with contextlib.redirect_stdout(io.StringIO()):
            return hp.main(["confirm", "--record", str(self.record), "--step", step, "--by", "tester"])

    def test_sequence_follows_design_states_and_human_points(self):
        reaches = [s.reach for s in hp.STEPS if s.kind == "observe"]
        self.assertEqual(reaches, sorted(reaches, key=hp.ORDER.index))
        humans = [s.id for s in hp.STEPS if s.kind == "human"]
        self.assertEqual(humans, ["s2-change-approval", "s3-cutover-approval", "s4-alert-seen", "s4-failback-approval"])
        ids = [s.id for s in hp.STEPS]
        self.assertLess(ids.index("s4-fault-clear"), ids.index("s4-failback-approval"))

    def test_header_records_run_identity(self):
        self.run_driver()
        head = json.loads(self.record.read_text().splitlines()[0])
        self.assertEqual((head["plan_id"], head["profile"], head["develop_sha"]), ("plan-1", "mock", "a" * 40))
        self.assertEqual(set(head["gitops_pins"]), {"registration", "product-reader"})

    def test_waits_for_plan_state_then_human_without_approving(self):
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["reason"]), (10, "s1-analyzed", "plan_not_yet"))
        self.state = "REHEARSE"
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["reason"]), (10, "s2-change-approval", "awaiting_human"))
        self.assertEqual(self.calls, ["demo_reset.py:check"])  # preflight ran once only
        self.assertEqual(self.confirm("s2-change-approval"), 0)
        self.state = "AWAIT_APPROVAL"
        code, line = self.run_driver()
        self.assertEqual(line["step"], "s3-cutover-approval")
        self.assertIn("open_after_apply.py:-", self.calls)

    def test_unwired_steps_stop_closed(self):
        self.state = "AWAIT_APPROVAL"
        self.run_driver()
        self.confirm("s2-change-approval")
        self.run_driver()
        self.confirm("s3-cutover-approval")
        code, line = self.run_driver()
        self.assertEqual((code, line["step"], line["reason"]), (3, "s3-load", "unwired"))
        self.assertEqual(self.run_driver(read_state=None)[0], 3)

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


if __name__ == "__main__":
    unittest.main()
