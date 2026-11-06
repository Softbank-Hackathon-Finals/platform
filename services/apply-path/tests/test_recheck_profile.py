"""The T5 profile the host uses for the post-apply recheck asks exactly what T31 requests."""

import json
from pathlib import Path

from bravo_apply.path import GATE_RECHECK_TESTS, POLICY_GATE_RECHECK_TESTS

CATALOG = json.loads((Path(__file__).resolve().parents[3] / "docs/controls/profiles.json").read_text())


def _profile(profile_id, version):
    found = [p for p in CATALOG["profiles"] if (p["id"], p["version"]) == (profile_id, version)]
    assert len(found) == 1
    return found[0]


def test_apply_recheck_profile_is_the_requested_recheck():
    tests = _profile("apply-recheck", "1")["required_test_ids"]
    assert tests == GATE_RECHECK_TESTS
    assert set(POLICY_GATE_RECHECK_TESTS) <= set(tests)
    assert all(test in CATALOG["tests"] for test in tests)


def test_first_gate_profile_is_unchanged():
    assert _profile("demo-gate", "1")["required_test_ids"] == ["IAM-01.1", "SEC-01.1", "AUD-01.1"]
