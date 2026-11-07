# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""T44 platform tailnet routes on the platform Argo CD: off by default, pinned to a reviewed commit,
and a project that allows exactly what the generated chart contains."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from test_registration_chart import ArgoBoundary

ROOT = Path(__file__).resolve().parents[2]
NETWORK_CHART = ROOT / "infra/iac/charts/platform"
ON = {"enabled": True, "targetRevision": "a" * 40, "tailnet": "sample.ts.net",
      "tailnets": {"workload": "workload-sample.ts.net"}}


def docs(result):
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def network_chart_objects():
    with tempfile.TemporaryDirectory() as directory:
        values = Path(directory) / "values.json"
        values.write_text(json.dumps({"tailnet": ON["tailnet"], "tailnets": ON["tailnets"]}))
        result = subprocess.run(["helm", "template", "iac-network", str(NETWORK_CHART), "-f", str(values)],
                                check=True, capture_output=True, text=True, timeout=30)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


class IacNetworkApplication(unittest.TestCase):
    def render(self, value):
        return ArgoBoundary().render({"iacNetwork": value})

    def test_off_by_default(self):
        result = ArgoBoundary().render({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([d for d in docs(result) if d["metadata"]["name"] == "platform-iac-network"])

    def test_a_merge_alone_never_applies_and_the_tailnets_stay_out_of_git(self):
        for change in ({"targetRevision": "develop"}, {"targetRevision": ""}, {"tailnet": ""},
                       {"tailnet": "sample.example.com"}, {"tailnets": {"workload": ""}}):
            with self.subTest(change=change):
                self.assertNotEqual(self.render(ON | change).returncode, 0)

    def test_the_application_pins_the_commit_and_never_prunes(self):
        result = self.render(ON)
        self.assertEqual(result.returncode, 0, result.stderr)
        app = next(d for d in docs(result) if d["kind"] == "Application" and d["metadata"]["name"] == "platform-iac-network")
        source = app["spec"]["source"]
        self.assertEqual((source["path"], source["targetRevision"]), ("infra/iac/charts/platform", "a" * 40))
        self.assertEqual(source["helm"]["valuesObject"], {"tailnet": ON["tailnet"], "tailnets": ON["tailnets"]})
        self.assertFalse(app["spec"]["syncPolicy"]["automated"]["prune"])
        self.assertIn("ServerSideApply=true", app["spec"]["syncPolicy"]["syncOptions"])

    def test_the_project_allows_exactly_what_the_generated_chart_contains(self):
        project = next(d for d in docs(self.render(ON)) if d["kind"] == "AppProject"
                       and d["metadata"]["name"] == "platform-iac-network")["spec"]
        objects = network_chart_objects()
        group = lambda o: o["apiVersion"].rpartition("/")[0]  # noqa: E731
        namespaced = {(group(o), o["kind"]) for o in objects if "namespace" in o["metadata"]}
        cluster = {(group(o), o["kind"]) for o in objects if "namespace" not in o["metadata"]}
        self.assertEqual({(k["group"], k["kind"]) for k in project["namespaceResourceWhitelist"]}, namespaced)
        self.assertEqual({(k["group"], k["kind"]) for k in project["clusterResourceWhitelist"]}, cluster)
        self.assertEqual({d["namespace"] for d in project["destinations"]},
                         {o["metadata"]["namespace"] for o in objects if "namespace" in o["metadata"]})


if __name__ == "__main__":
    unittest.main()
