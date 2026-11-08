import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("render.py")
SPEC = importlib.util.spec_from_file_location("platform_security_review_storage", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RenderTest(unittest.TestCase):
    def test_renders_retained_volume_bound_to_exact_claim(self):
        volume = MODULE.render("ip-10-81-0-191")

        self.assertEqual(volume["metadata"]["name"], "security-review-audit")
        self.assertEqual(volume["spec"]["local"]["path"], "/var/lib/bravo-security-review-audit")
        self.assertEqual(volume["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(
            volume["spec"]["claimRef"],
            {"namespace": "platform-state", "name": "security-review-audit"},
        )
        self.assertEqual(
            volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]
            ["matchExpressions"][0]["values"],
            ["ip-10-81-0-191"],
        )

    def test_rejects_invalid_node_name(self):
        with self.assertRaisesRegex(ValueError, "invalid_node"):
            MODULE.render("node; touch /tmp/unsafe")


if __name__ == "__main__":
    unittest.main()
