import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "infra/envs/onprem/render-user-data.sh"
POLICY = ROOT / "services/control-runner/templates/rke2-audit-policy.yaml"
BOOTSTRAP = ROOT / "infra/shared/rke2/bootstrap.sh"
POLICY_BLOCK = re.compile(
    r"install -m 600 /dev/stdin /etc/rancher/rke2/audit-policy\.yaml <<'AUDIT_POLICY'\n(.*?)^AUDIT_POLICY$",
    re.DOTALL | re.MULTILINE,
)


def render(script=RENDER):
    return subprocess.run(["bash", str(script)], capture_output=True, text=True)


class OnPremUserDataTests(unittest.TestCase):
    def setUp(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.user_data = result.stdout

    def test_output_is_valid_bash(self):
        subprocess.run(["bash", "-n"], input=self.user_data, text=True, check=True)

    def test_policy_is_the_template_byte_for_byte(self):
        block = POLICY_BLOCK.search(self.user_data)
        self.assertIsNotNone(block, "user-data does not write the audit policy")
        self.assertEqual(block.group(1), POLICY.read_text())

    # RKE2 writes its own level: None policy on first start when the file is missing.
    def test_policy_is_written_before_rke2_starts(self):
        block = POLICY_BLOCK.search(self.user_data)
        self.assertIsNotNone(block)
        self.assertLess(block.end(), self.user_data.index("bash /run/bravo-rke2-bootstrap.sh"))

    def test_common_bootstrap_is_embedded_unchanged(self):
        self.assertIn(BOOTSTRAP.read_text(), self.user_data)

    def test_missing_template_stops_rendering(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "infra/envs/onprem/render-user-data.sh"
            copy.parent.mkdir(parents=True)
            shutil.copy(RENDER, copy)
            (Path(temp) / "infra/shared/rke2").mkdir(parents=True)
            shutil.copy(BOOTSTRAP, Path(temp) / "infra/shared/rke2/bootstrap.sh")
            self.assertNotEqual(render(copy).returncode, 0)


class AuditPolicyTests(unittest.TestCase):
    # A body-recording level would put Secret values in the log, whichever rule it is on.
    def test_no_rule_records_request_or_response_bodies(self):
        levels = re.findall(r"^\s*-\s*level:\s*(\S+)", POLICY.read_text(), re.MULTILINE)
        self.assertTrue(levels)
        self.assertLessEqual(set(levels), {"None", "Metadata"})

    # Rules are first-match, so an unconditional rule must be last and must not be None.
    def test_requests_no_rule_matches_are_still_recorded(self):
        self.assertTrue(POLICY.read_text().endswith("  - level: Metadata\n"))


if __name__ == "__main__":
    unittest.main()
