"""T48 / ADR-0035: the whole tailnet policy is composed from Git, deterministically and without people."""
import importlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT / "infra/tailnet"))
compose_policy = importlib.import_module("compose_policy")
compose, PolicyError = compose_policy.compose, compose_policy.PolicyError

BASE = {"tagOwners": {"tag:bravo-platform": ["autogroup:admin"]}, "grants": [], "tests": []}
FRAGMENT = {"tagOwners": {"tag:bravo-e2e-db": ["tag:bravo-e2e-operator"]},
            "autoApprovers": {"services": {"svc:bravo-e2e-people": ["tag:bravo-e2e-people"]}},
            "grants": [{"src": ["autogroup:admin"], "dst": ["tag:bravo-e2e-people"], "ip": ["tcp:443"]}],
            "tests": []}
IDENTITIES = {"e2e": {"issuer": "https://issuer.example/e2e"},
              "other": {"issuer": "https://issuer.example/other", "tailnet": "workload"}}


class ComposeTests(unittest.TestCase):
    def test_fragments_and_operator_tag_owners_of_this_tailnet_only(self):
        policy = compose(BASE, [FRAGMENT], IDENTITIES, "default")
        self.assertEqual(policy["tagOwners"]["tag:bravo-e2e-operator"], ["autogroup:admin"])
        self.assertNotIn("tag:bravo-other-operator", policy["tagOwners"])
        self.assertIn("tag:bravo-platform", policy["tagOwners"])
        self.assertEqual(policy["autoApprovers"], FRAGMENT["autoApprovers"])
        self.assertEqual(policy["grants"], FRAGMENT["grants"])

    def test_same_input_same_policy_and_repeated_entries_once(self):
        once = compose(BASE, [FRAGMENT], IDENTITIES, "default")
        self.assertEqual(once, compose(BASE, [FRAGMENT, FRAGMENT], IDENTITIES, "default"))
        self.assertEqual(json.dumps(once, sort_keys=True), json.dumps(compose(BASE, [FRAGMENT], IDENTITIES,
                                                                             "default"), sort_keys=True))

    def test_a_key_with_two_values_is_refused(self):
        for other in ({"tagOwners": {"tag:bravo-platform": ["autogroup:owner"]}},
                      {"autoApprovers": {"services": {"svc:bravo-e2e-people": ["autogroup:member"]}}}):
            with self.subTest(other=other), self.assertRaisesRegex(PolicyError, "conflict at policy"):
                compose(BASE, [FRAGMENT, other], {}, "default")

    def test_a_person_is_refused_anywhere(self):
        for part in ({"groups": {"group:ops": ["someone@example.com"]}},
                     {"grants": [{"src": ["someone@example.com"], "dst": ["tag:x"], "ip": ["*"]}]}):
            with self.subTest(part=part), self.assertRaisesRegex(PolicyError, "names a person"):
                compose(BASE, [part], {}, "default")

    def test_bad_environment_names_are_refused(self):
        with self.assertRaisesRegex(PolicyError, "bad environment name"):
            compose(BASE, [], {"Bad_Env": {"issuer": "https://issuer.example/x"}}, "default")

    def test_the_committed_files_compose(self):
        load = lambda p: json.loads((ROOT / p).read_text())
        for tailnet, fragment in (("default", "routes-policy.json"), ("workload", "routes-policy.workload.json")):
            with self.subTest(tailnet=tailnet):
                compose(load("infra/tailnet/base-policy.json"), [load(f"infra/tailnet/{fragment}")],
                        load("infra/tailnet/identities.json"), tailnet)


if __name__ == "__main__":
    unittest.main()
