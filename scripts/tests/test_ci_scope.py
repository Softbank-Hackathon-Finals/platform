"""Regression cases for selecting too little work and accepting skipped failures."""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
scope = importlib.import_module("ci_scope")
FILES = ["services/a/pyproject.toml", "services/b/pyproject.toml", "infra/envs/a/main.tf"]


class ScopeTest(unittest.TestCase):
    def test_prose_only_still_produces_explicit_empty_plan(self):
        plan = scope.select(FILES, ["docs/tasks.md", "services/a/README.md"])
        self.assertEqual(plan["projects"], [])
        self.assertFalse(plan["infra"])
        self.assertFalse(plan["full"])

    def test_service_change_does_not_test_other_service(self):
        plan = scope.select(FILES, ["services/a/src/api.py", "services/a/uv.lock"])
        self.assertEqual(plan["projects"], [{"path": "services/a", "kind": "python"}])
        self.assertFalse(plan["infra"])

    def test_tf_checks_infra_without_services(self):
        plan = scope.select(FILES, ["infra/modules/network/aws/main.tf"])
        self.assertTrue(plan["infra"])
        self.assertEqual(plan["projects"], [])

    def test_application_html_and_assets_are_runtime_inputs(self):
        for path in ("services/a/index.html", "services/a/assets/logo.svg", "services/a/content/page.md"):
            self.assertEqual(scope.select(FILES, [path])["projects"], [{"path": "services/a", "kind": "python"}])

    def test_service_network_policy_is_also_scanned_as_infrastructure(self):
        plan = scope.select(FILES, ["services/a/templates/30-networkpolicy.yaml"])
        self.assertTrue(plan["infra"])
        self.assertEqual(plan["projects"], [{"path": "services/a", "kind": "python"}])

    def test_shared_unknown_and_deleted_manifest_select_all(self):
        for path in ["scripts/ci_scope.py", ".github/workflows/ci.yml", "schemas/proposal.json",
                     "new-system/main.py", "services/removed/pyproject.toml", "docs/examples/contract.json"]:
            with self.subTest(path=path):
                plan = scope.select(FILES, [path])
                self.assertTrue(plan["full"])
                self.assertTrue(plan["infra"])
                self.assertTrue(plan["scripts"])
                self.assertEqual(len(plan["projects"]), 2)

    def test_cross_project_rename_checks_both_old_and_new_paths(self):
        plan = scope.select(FILES, ["services/a/old.py", "services/b/new.py"])
        self.assertEqual(len(plan["projects"]), 2)

    def test_added_manifest_is_discovered_without_registry(self):
        plan = scope.select(FILES + ["services/c/pyproject.toml"], ["services/c/pyproject.toml"])
        self.assertEqual(plan["projects"], [{"path": "services/c", "kind": "python"}])

    def test_missing_history_and_initial_commit_fall_back_to_full(self):
        with patch.object(scope, "git", return_value=b"services/a/pyproject.toml\0"):
            self.assertTrue(scope.plan("")["full"])
        with patch.object(scope, "git", side_effect=[b"services/a/pyproject.toml\0", subprocess.CalledProcessError(1, "git")]):
            self.assertEqual(scope.plan("a" * 40)["reasons"], ["unavailable-history"])

    def test_nested_project_checks_parent_and_child(self):
        plan = scope.select(FILES + ["services/a/sub/pyproject.toml"], ["services/a/sub/api.py"])
        self.assertEqual(len(plan["projects"]), 2)

    def test_unsafe_matrix_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            scope.projects(["services/a\nunsafe/pyproject.toml"])

    def test_local_links_force_conservative_dependency_scope(self):
        for name, text in [("pyproject.toml", 'lib = {path = "../lib"}'),
                           ("pyproject.toml", '[tool.uv.workspace]\nmembers=["libs/*"]'),
                           ("package.json", '{"dependencies":{"lib":"workspace:*"}}'),
                           ("go.mod", 'replace example.com/lib => ../lib')]:
            with self.subTest(name=name, text=text):
                self.assertTrue(scope.local_dependencies([name], lambda _, value=text: value))
        self.assertTrue(scope.local_dependencies(["pnpm-workspace.yaml"]))
        self.assertFalse(scope.local_dependencies(["pyproject.toml"], lambda _: '[project]\nname="independent"'))


if __name__ == "__main__":
    unittest.main()
