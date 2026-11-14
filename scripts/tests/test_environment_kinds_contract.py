"""ADR-0034 contract files stay consistent with each other."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KINDS = ROOT / "docs/contracts/environment-kinds.json"
NEUTRAL = ROOT / "docs/contracts/neutral-models.schema.json"
ADR = ROOT / "docs/adr/0034-environment-kind-adapters.md"


class EnvironmentKindsContract(unittest.TestCase):
    def setUp(self):
        self.kinds = json.loads(KINDS.read_text(encoding="utf-8"))
        self.neutral = json.loads(NEUTRAL.read_text(encoding="utf-8"))

    def test_three_kinds_both_roles(self):
        self.assertEqual(set(self.kinds["kinds"]), {"aws", "gcp", "onprem"})
        self.assertEqual(self.kinds["roles"], ["source", "target"])

    def test_every_kind_declares_the_same_fields(self):
        fields = {frozenset(v) for v in self.kinds["kinds"].values()}
        self.assertEqual(len(fields), 1)
        for value in self.kinds["kinds"].values():
            self.assertTrue(value["scope_keys"], "exit scope needs a key to separate same-kind targets")

    def test_slots_are_side_prefixed(self):
        for slot in self.kinds["slots"]:
            side, _, name = slot.partition(".")
            self.assertIn(side, {"source", "target", "both"})
            self.assertRegex(name, r"^[a-z_]+$")

    def test_neutral_kind_enum_matches_contract(self):
        self.assertEqual(set(self.neutral["$defs"]["Kind"]["enum"]), set(self.kinds["kinds"]))

    def test_neutral_models_never_carry_values(self):
        text = NEUTRAL.read_text(encoding="utf-8").lower()
        for word in ('"value"', '"password"', '"token"', '"secret_value"', '"credentials"'):
            self.assertNotIn(word, text)

    def test_adr_lists_every_slot(self):
        adr = ADR.read_text(encoding="utf-8")
        for slot in self.kinds["slots"]:
            self.assertIn(f"`{slot}`", adr)


if __name__ == "__main__":
    unittest.main()
