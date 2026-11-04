import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "infra/envs/onprem/verify-node.sh"
# The script runs its checks from the first `check '` line on. Only the definitions are loaded here.
DEFINITIONS = VERIFY.read_text()[: VERIFY.read_text().index("\ncheck '")]

# Stands in for the API server: it answers the dry-run and writes what the audit backend would write.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
name=$3
[ "$3" = generic ] && name=$4
value=
for arg in "$@"; do
  case $arg in --from-literal=probe=*) value=${arg#--from-literal=probe=} ;; esac
done
ref="\"objectRef\":{\"namespace\":\"default\",\"name\":\"$name\"}"
log=$LOGS/audit.log
case $SCENARIO in
  none) ;;
  metadata) echo "{\"level\":\"Metadata\",\"stage\":\"ResponseComplete\",$ref}" >>"$log" ;;
  request-body) echo "{\"level\":\"Request\",\"stage\":\"ResponseComplete\",$ref,\"requestObject\":\"$value\"}" >>"$log" ;;
  encoded-body)
    encoded=$(printf %s "$value" | base64)
    echo "{\"level\":\"Metadata\",\"stage\":\"ResponseComplete\",$ref}" >>"$log"
    echo "{\"level\":\"Request\",\"stage\":\"ResponseComplete\",\"requestObject\":\"$encoded\"}" >>"$log"
    ;;
  metadata-and-exposed)
    echo "{\"level\":\"Metadata\",\"stage\":\"ResponseComplete\",$ref}" >>"$log"
    echo "{\"level\":\"Request\",\"stage\":\"ResponseComplete\",\"requestObject\":\"$value\"}" >>"$log"
    ;;
esac
"""

# Runs after each grep that opened a log file, so the event happens right between two reads.
AFTER_FIRST_READ = """
grep() {{
  command grep "$@"
  local status=$?
  case " $* " in
    *"$LOGS/"*)
      if [ ! -e "$LOGS/.done" ]; then
        : >"$LOGS/.done"
        {action}
      fi
      ;;
  esac
  return $status
}}
"""
ROTATE = 'mv "$LOGS/audit.log" "$LOGS/audit-rotated.log"; : >"$LOGS/audit.log"'
DELETE = 'rm -f "$LOGS/audit.log"'


class AuditCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        (self.logs / "audit.log").write_text("")
        self.kubectl = self.root / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL)
        self.kubectl.chmod(self.kubectl.stat().st_mode | stat.S_IXUSR)

    def run_check(self, function, scenario, between_reads="", after_request=""):
        kubectl = self.kubectl
        if after_request:
            kubectl = self.root / "kubectl-then-change"
            kubectl.write_text(f'#!/usr/bin/env bash\n"{self.kubectl}" "$@"\n{after_request}\n')
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
        driver = "\n".join((
            DEFINITIONS,
            f'KUBECTL=("{kubectl}")',
            f'AUDIT_LOG_DIR="{self.logs}"',
            "sleep() { :; }",
            AFTER_FIRST_READ.format(action=between_reads) if between_reads else "",
            f"{function} >/dev/null 2>&1",
        ))
        return subprocess.run(
            ["bash", "-c", driver], capture_output=True, text=True,
            env={**os.environ, "LOGS": str(self.logs), "SCENARIO": scenario},
        ).returncode

    def test_passes_when_recorded_as_metadata_without_the_value(self):
        self.assertEqual(self.run_check("audit_omits_secret_value", "metadata"), 0)
        self.assertEqual(self.run_check("audit_records_rbac_change", "metadata"), 0)

    def test_fails_when_nothing_is_recorded(self):
        self.assertNotEqual(self.run_check("audit_omits_secret_value", "none"), 0)
        self.assertNotEqual(self.run_check("audit_records_rbac_change", "none"), 0)

    def test_fails_when_the_value_is_logged(self):
        self.assertNotEqual(self.run_check("audit_omits_secret_value", "request-body"), 0)
        self.assertNotEqual(self.run_check("audit_omits_secret_value", "metadata-and-exposed"), 0)

    def test_fails_when_the_base64_value_is_logged(self):
        self.assertNotEqual(self.run_check("audit_omits_secret_value", "encoded-body"), 0)

    def test_fails_when_no_log_file_exists(self):
        (self.logs / "audit.log").unlink()
        self.assertNotEqual(self.run_check("audit_omits_secret_value", "none"), 0)
        self.assertNotEqual(self.run_check("audit_records_rbac_change", "none"), 0)

    # A read error used to count as "value not found", the reported defect.
    def test_fails_when_the_log_is_deleted_during_the_check_and_the_value_was_logged(self):
        self.assertNotEqual(
            self.run_check("audit_omits_secret_value", "metadata-and-exposed", between_reads=DELETE), 0)

    def test_fails_when_any_log_in_range_cannot_be_read(self):
        (self.logs / "audit-old.log").symlink_to(self.root / "missing")
        self.assertNotEqual(self.run_check("audit_omits_secret_value", "metadata"), 0)
        self.assertNotEqual(self.run_check("audit_records_rbac_change", "metadata"), 0)

    # Rotation between two reads used to move the value out of what the second read looked at.
    def test_fails_when_rotation_during_the_check_moves_the_value_away(self):
        self.assertNotEqual(
            self.run_check("audit_omits_secret_value", "metadata-and-exposed", between_reads=ROTATE), 0)

    def test_fails_when_rotation_before_the_check_moves_the_value_away(self):
        rotate = f'mv "{self.logs}/audit.log" "{self.logs}/audit-rotated.log"; : >"{self.logs}/audit.log"'
        self.assertNotEqual(
            self.run_check("audit_omits_secret_value", "metadata-and-exposed", after_request=rotate), 0)

    def test_passes_when_rotation_moves_the_event_away_before_the_check(self):
        rotate = f'mv "{self.logs}/audit.log" "{self.logs}/audit-rotated.log"; : >"{self.logs}/audit.log"'
        self.assertEqual(self.run_check("audit_omits_secret_value", "metadata", after_request=rotate), 0)
        self.assertEqual(self.run_check("audit_records_rbac_change", "metadata", after_request=rotate), 0)


if __name__ == "__main__":
    unittest.main()
