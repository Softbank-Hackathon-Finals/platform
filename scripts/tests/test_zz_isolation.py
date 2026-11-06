"""Runs last in `unittest discover -s scripts/tests`: no earlier test left a shared module patched.

A test that assigns e.g. `module.time.sleep = ...` changes the stdlib for every later test in the same
process; #565 lost three CI rounds to that (a later SIGTERM test saw a no-op sleep). Patch with
`mock.patch.object(...)` scoped to the test instead.
"""

import os
import subprocess
import time
import unittest

# Captured at discovery (import), before any test runs.
ORIGINAL = {"time.sleep": time.sleep, "time.time": time.time, "time.monotonic": time.monotonic,
            "subprocess.run": subprocess.run, "subprocess.Popen": subprocess.Popen,
            "os.environ": os.environ.__class__}


class SharedModulesUntouched(unittest.TestCase):
    def test_no_test_left_a_shared_module_patched(self):
        now = {"time.sleep": time.sleep, "time.time": time.time, "time.monotonic": time.monotonic,
               "subprocess.run": subprocess.run, "subprocess.Popen": subprocess.Popen,
               "os.environ": os.environ.__class__}
        self.assertEqual([k for k in ORIGINAL if now[k] is not ORIGINAL[k]], [],
                         "an earlier test replaced these without restoring them")


if __name__ == "__main__":
    unittest.main()
