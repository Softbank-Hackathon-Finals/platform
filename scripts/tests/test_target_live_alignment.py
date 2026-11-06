"""What the live target cluster runs and the T30 render must say too, so a re-apply does not undo it.

Both values were found by a server-side diff on 2026-11-06: the pooler's backend CA and the object store image.
"""
import importlib
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKLOADS = ROOT / "infra/envs/target-workloads"
with patch.object(sys, "path", [str(WORKLOADS), *sys.path]), patch.dict(sys.modules):
    for shadowed in ("render", "objects", "pooler"):
        sys.modules.pop(shadowed, None)
    pooler = importlib.import_module("pooler")
    objects = importlib.import_module("objects")

# The cutover executor's own patch, loaded from its file: it imports nothing.
_spec = importlib.util.spec_from_file_location(
    "bravo_cutover_prepare", ROOT / "services/cutover-executor/src/bravo_cutover/prepare.py")
prepare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prepare)

SERVICE_UID = "0b7c2a5e-5f1d-4c3a-9e8b-1a2b3c4d5e6f"
# Children of the SeaweedFS 4.52 index, read from Docker Hub on 2026-11-06. Either alone runs on one architecture.
SINGLE_PLATFORM = {"amd64": "sha256:a54ba35b44895a449f5cdd415999062201b17b9835ec0ffc19ea605b72ffa3d1",
                   "arm64": "sha256:18a805c81cd574a24571cfbc23ff1cb0bb2b875b1ac028a58c065c15f0c7b3ad"}


def kind(items, wanted):
    return next(item for item in items if item["kind"] == wanted)


class PoolerTrustTests(unittest.TestCase):
    def rendered(self):
        deployment = kind(pooler.pooler("10.0.0.10", SERVICE_UID), "Deployment")
        deployment["metadata"]["resourceVersion"] = "1"
        return deployment

    def test_executor_trust_patch_has_nothing_left_to_change(self):
        # Only the resourceVersion test remains, so re-applying the render keeps the executor's CA bundle.
        self.assertEqual(prepare.pooler_trust_patch(self.rendered(), side="target"),
                         [{"op": "test", "path": "/metadata/resourceVersion", "value": "1"}])

    def test_backend_ca_is_the_bundle_secret_with_the_same_key(self):
        volume = next(v for v in self.rendered()["spec"]["template"]["spec"]["volumes"] if v["name"] == "ca")
        self.assertEqual(volume["secret"], {"secretName": prepare.TRUST_SECRET,
                                            "items": [{"key": "ca.crt", "path": "ca.crt"}]})


class ObjectStoreImageTests(unittest.TestCase):
    def test_object_store_runs_the_release_index_not_one_architecture(self):
        container = kind(objects.objects("node-a"), "Deployment")["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], objects.IMAGE)
        self.assertRegex(objects.IMAGE, r"\Adocker\.io/chrislusf/seaweedfs:4\.52@sha256:[0-9a-f]{64}\Z")
        self.assertNotIn(objects.IMAGE.split("@")[1], SINGLE_PLATFORM.values())


if __name__ == "__main__":
    unittest.main()
