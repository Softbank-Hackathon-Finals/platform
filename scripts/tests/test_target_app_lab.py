"""The local lab must own what it removes: another run or a same-named container is not its to delete."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PIN = json.loads((ROOT / "infra/envs/target-workloads/app-image.json").read_text())


def load_lab(name="target_app_lab"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "infra/envs/target-workloads/testing/app_lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    """Answers the lab's docker calls and records them. A real container would never be touched."""

    def __init__(self, lab, *, created_id="c" * 64, seen_id=None, seen_run=None, image_run=None):
        self.lab, self.calls = lab, []
        self.created_id, self.seen_id, self.seen_run = created_id, seen_id or created_id, seen_run or lab.RUN_ID
        self.image_run = image_run or lab.RUN_ID

    def __call__(self, *command, stdin=None, check=True):
        self.calls.append(command)
        if command[:2] == ("docker", "run"):
            return self.created_id + "\n"
        if command[:3] == ("docker", "image", "inspect"):
            return self.image_run + "\n"
        if command[:2] == ("docker", "inspect"):
            return f"{self.seen_id} {self.seen_run}\n"
        if command[:2] == ("docker", "exec"):
            return f"{self.lab.NODE} Ready control-plane 1s v1\n"
        return ""

    def removed(self):
        return [c for c in self.calls if c[:2] in (("docker", "rm"), ("docker", "rmi"))]


class RunScopeTests(unittest.TestCase):
    def test_each_run_has_its_own_names(self):
        first, second = load_lab("lab_a"), load_lab("lab_b")
        self.assertNotEqual(first.RUN_ID, second.RUN_ID)
        self.assertNotEqual(first.K3S, second.K3S)
        self.assertIn(first.RUN_ID, first.K3S)
        self.assertIn(first.RUN_ID, first.local_image_tag("c1c74ab"))
        self.assertNotEqual(first.local_image_tag("c1c74ab"), second.local_image_tag("c1c74ab"))

    def test_starting_removes_nothing_and_records_what_it_created(self):
        lab = load_lab()
        docker = FakeDocker(lab)
        with patch.object(lab, "run", docker):
            lab.start_cluster()
        self.assertEqual(docker.removed(), [])
        created = next(c for c in docker.calls if c[:2] == ("docker", "run"))
        self.assertIn(lab.K3S, created)
        self.assertIn(f"{lab.RUN_LABEL}={lab.RUN_ID}", created)
        self.assertEqual(lab.OWNED["container"], docker.created_id)

    def test_cleanup_removes_the_created_container_by_id(self):
        lab = load_lab()
        docker = FakeDocker(lab)
        with patch.object(lab, "run", docker):
            lab.OWNED["container"] = docker.created_id
            result = lab.remove_owned()
        self.assertEqual(docker.removed(), [("docker", "rm", "-fv", docker.created_id)])
        self.assertEqual(result["container"], "removed")

    def test_cleanup_refuses_a_container_with_another_id_or_run(self):
        for kwargs in ({"seen_id": "d" * 64}, {"seen_run": "other-run"}):
            with self.subTest(kwargs=kwargs):
                lab = load_lab()
                docker = FakeDocker(lab, **kwargs)
                with patch.object(lab, "run", docker):
                    lab.OWNED["container"] = docker.created_id
                    result = lab.remove_owned()
                self.assertEqual(docker.removed(), [])
                self.assertEqual(result["container"], "not_owned_left_in_place")

    def test_cleanup_without_a_created_container_removes_nothing(self):
        lab = load_lab()
        docker = FakeDocker(lab)
        with patch.object(lab, "run", docker):
            result = lab.remove_owned()
        self.assertEqual(docker.calls, [])
        self.assertEqual(result, {"container": "none_created", "image": "none_created"})

    def test_cleanup_removes_only_the_tag_this_run_built(self):
        lab = load_lab()
        docker = FakeDocker(lab)
        tag = lab.local_image_tag("c1c74ab")
        with patch.object(lab, "run", docker):
            lab.OWNED["image"] = tag
            result = lab.remove_owned()
        self.assertEqual(docker.removed(), [("docker", "rmi", tag)])
        self.assertEqual(result["image"], "removed")
        self.assertFalse(any(c[:2] == ("docker", "images") for c in docker.calls))

    def test_cleanup_refuses_an_image_from_another_run(self):
        lab = load_lab()
        docker = FakeDocker(lab, image_run="other-run")
        with patch.object(lab, "run", docker):
            lab.OWNED["image"] = lab.local_image_tag("c1c74ab")
            result = lab.remove_owned()
        self.assertEqual(docker.removed(), [])
        self.assertEqual(result["image"], "not_owned_left_in_place")


class LocalImageInjectionTests(unittest.TestCase):
    def test_local_build_reaches_only_the_lab_manifest(self):
        lab = load_lab()
        local = "ghcr.io/softbank-hackathon-finals/demo-chat@sha256:" + "e" * 64
        rendered = lab.render("application.py")
        injected = lab.inject_local_image(rendered, local)
        pods = [i["spec"]["template"]["spec"] for i in injected if i["kind"] == "Deployment"]
        self.assertEqual(len(pods), 1)
        self.assertEqual({c["image"] for c in [*pods[0]["containers"], *pods[0]["initContainers"]]}, {local})
        original = next(i for i in rendered if i["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertIn(PIN["index_digest"], original["containers"][0]["image"])

    def test_lab_renders_for_its_own_node_architecture(self):
        # A local build runs on the k3s node's architecture; an amd64 render would never schedule on arm64.
        lab = load_lab()
        calls = []
        with patch.object(lab, "kubectl", lambda *args, **_: calls.append(args) or "arm64\n"):
            self.assertEqual(lab.node_architecture(), "arm64")
        self.assertEqual(calls, [("get", "node", lab.NODE, "-o", "jsonpath={.status.nodeInfo.architecture}")])
        local = "docker.io/library/bravo-demo-chat@sha256:" + "e" * 64
        injected = lab.inject_local_image(lab.render("application.py", "--arch", "arm64"), local)
        pod = next(i for i in injected if i["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/arch": "arm64"})
        self.assertEqual({c["image"] for c in [*pod["containers"], *pod["initContainers"]]}, {local})

    def test_the_general_render_path_refuses_the_local_build(self):
        lab = load_lab()
        self.assertFalse(lab.general_path_accepts("ghcr.io/softbank-hackathon-finals/demo-chat@sha256:" + "e" * 64))
        self.assertTrue(lab.general_path_accepts(f"ghcr.io/softbank-hackathon-finals/demo-chat@{PIN['index_digest']}"))


if __name__ == "__main__":
    unittest.main()
