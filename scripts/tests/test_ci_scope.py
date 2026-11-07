"""Regression cases for selecting too little work and accepting skipped failures."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
scope = importlib.import_module("ci_scope")
FILES = ["services/a/pyproject.toml", "services/b/pyproject.toml", "infra/envs/a/main.tf"]
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def workflow_shell(step: str) -> str:
    """Execute the actual workflow shell, without a second copy of its policy."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step}")
    start = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |") + 1
    end = start
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith("          ")):
        end += 1
    return textwrap.dedent("\n".join(lines[start:end]))


class MergeGroupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "CI test")
        self.git("config", "user.email", "ci@example.invalid")
        for project in ("a", "b"):
            path = self.root / f"services/{project}/pyproject.toml"
            path.parent.mkdir(parents=True)
            path.write_text('[project]\nname="example"\nversion="0.0.0"\n')
        script = self.root / "scripts/ci_scope.py"
        script.parent.mkdir()
        script.write_text(Path(scope.__file__).read_text())
        self.base = self.commit()
        (self.root / "services/a/api.py").write_text("# earlier queued PR\n")
        self.first = self.commit()
        (self.root / "services/b/api.py").write_text("# later queued PR\n")
        self.head = self.commit()
        self.git("branch", "develop", self.base)  # the queue's target branch, before both queued PRs

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL).decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def run_plan(self, **overrides):
        env = dict(os.environ, EVENT="merge_group", GROUP_ACTION="checks_requested", BASE=self.base,
                   GROUP_HEAD=self.head, TARGET_BRANCH="refs/heads/develop", GITHUB_STEP_SUMMARY=str(self.root / "summary"),
                   GITHUB_OUTPUT=str(self.root / "output"))
        env.update(overrides)
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", workflow_shell("Plan affected checks")],
                              cwd=self.root, env=env, capture_output=True, text=True, timeout=10, check=False)

    def test_group_checks_earlier_and_later_pr_together(self):
        for base in (self.base, self.first):
            with self.subTest(base=base):
                result = self.run_plan(BASE=base)
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = json.loads((self.root / "ci-plan.json").read_text())
                # The whole group from the target branch, whatever base_sha says; not the full suite.
                self.assertEqual([p["path"] for p in plan["projects"]], ["services/a", "services/b"])
                self.assertFalse(plan["full"])

    def test_previous_group_base_still_runs_combination_failure(self):
        self.git("checkout", "-qb", "candidate-a", self.base)
        test = self.root / "services/a/test_combination.py"
        test.write_text(textwrap.dedent("""\
            import unittest
            from pathlib import Path

            class CombinationTest(unittest.TestCase):
                def test_prior_service_accepts_later_change(self):
                    self.assertFalse((Path(__file__).parents[2] / "scripts/later.txt").exists())
            """))
        candidate_a = self.commit()
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "services/a",
                   "-p", "test_combination.py"]
        healthy = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                 timeout=10, check=False)
        self.assertEqual(healthy.returncode, 0, healthy.stderr)

        self.git("checkout", "-qb", "candidate-b", self.base)
        (self.root / "scripts/later.txt").write_text("later queued change\n")
        candidate_b = self.commit()
        self.git("checkout", "-qb", "queue", self.base)
        self.git("merge", "--no-ff", "-qm", "group A", candidate_a)
        group_a = self.git("rev-parse", "HEAD")
        self.git("merge", "--no-ff", "-qm", "group A+B", candidate_b)
        group_ab = self.git("rev-parse", "HEAD")
        result = self.run_plan(BASE=group_a, GROUP_HEAD=group_ab)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads((self.root / "ci-plan.json").read_text())
        self.assertIn({"path": "services/a", "kind": "python"}, plan["projects"])
        combined = subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                                  timeout=10, check=False)
        self.assertEqual(combined.returncode, 1, combined.stderr)
        self.assertIn("AssertionError", combined.stderr)
        env = dict(os.environ, SCOPE_RESULT="success", PROJECT_RESULT="failure",
                   PROJECTS=json.dumps(plan["projects"]))
        aggregate = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", workflow_shell("Require all selected projects to succeed")],
            env=env, capture_output=True, timeout=10, check=False)
        self.assertNotEqual(aggregate.returncode, 0)

    def test_malformed_missing_stale_and_unrelated_group_inputs_fail(self):
        # Build an existing, well-formed commit that is not an ancestor of the checkout.
        self.git("checkout", "-q", "--orphan", "unrelated")
        (self.root / "unrelated.txt").write_text("not the queue base")
        unrelated = self.commit()
        self.git("checkout", "-q", self.head)
        for values in ({"BASE": ""}, {"BASE": "not-a-sha"}, {"GROUP_HEAD": ""},
                       {"GROUP_HEAD": self.first}, {"BASE": "f" * 40},
                       {"BASE": unrelated}, {"GROUP_ACTION": "destroyed"},
                       {"TARGET_BRANCH": "refs/heads/missing"}, {"TARGET_BRANCH": "refs/heads/$(id)"}):
            with self.subTest(values=values):
                self.assertNotEqual(self.run_plan(**values).returncode, 0)

    def test_pull_request_and_push_paths_remain_supported(self):
        self.assertEqual(self.run_plan(EVENT="pull_request").returncode, 0)
        self.assertFalse(json.loads((self.root / "ci-plan.json").read_text())["full"])
        self.assertEqual(self.run_plan(EVENT="push", BASE="").returncode, 0)
        self.assertTrue(json.loads((self.root / "ci-plan.json").read_text())["full"])

    def test_selected_project_failure_cancellation_or_missing_scope_is_never_green(self):
        shell = workflow_shell("Require all selected projects to succeed")
        for scope_result in ("success", "failure", "cancelled", "skipped"):
            for project_result in ("success", "failure", "cancelled", "skipped"):
                for projects in ("[]", '[{"path":"services/a","kind":"python"}]'):
                    env = dict(os.environ, SCOPE_RESULT=scope_result,
                               PROJECT_RESULT=project_result, PROJECTS=projects)
                    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", shell], env=env,
                                            capture_output=True, timeout=10, check=False)
                    expected = scope_result == "success" and project_result == (
                        "skipped" if projects == "[]" else "success")
                    self.assertEqual(result.returncode == 0, expected, (scope_result, project_result, projects))


class ScopeTest(unittest.TestCase):
    def test_queue_does_not_widen_by_local_links(self):
        # PR runs widen scope for local project links; the queue checks the group's own changes.
        files = FILES
        with patch.object(scope, "git", side_effect=lambda *a: (
                "\0".join(files).encode() if a[0] == "ls-files" else
                b"base" if a[0] == "merge-base" else b"services/a/api.py")), \
             patch.object(scope, "local_dependencies", return_value=True):
            self.assertTrue(scope.plan("a" * 40)["full"])
            queued = scope.plan("a" * 40, queue=True)
        self.assertFalse(queued["full"])
        self.assertEqual(queued["projects"], [{"path": "services/a", "kind": "python"}])

    def test_other_workflow_runs_infra_only(self):
        plan = scope.select(FILES, [".github/workflows/dependency-watch.yml"])
        self.assertEqual((plan["full"], plan["infra"], plan["projects"]), (False, True, []))

    def test_ci_definition_runs_one_project_per_language(self):
        files = FILES + ["services/web/package.json", "services/tool/go.mod", "services/web2/package.json"]
        for changed in ([".github/workflows/ci.yml"], ["scripts/ci_scope.py"], ["scripts/tests/test_ci_scope.py"]):
            with self.subTest(changed=changed):
                plan = scope.select(files, changed)
                self.assertFalse(plan["full"])
                self.assertTrue(plan["infra"] and plan["scripts"])
                self.assertEqual(sorted(p["kind"] for p in plan["projects"]), ["go", "node", "python"])
        mixed = scope.select(files, [".github/workflows/ci.yml", "schemas/x.json"])
        self.assertTrue(mixed["full"])
        with patch.object(scope, "git", side_effect=lambda *a: (
                "\0".join(files).encode() if a[0] == "ls-files" else
                b"base" if a[0] == "merge-base" else b".github/workflows/ci.yml")), \
             patch.object(scope, "local_dependencies", return_value=True):
            self.assertFalse(scope.plan("a" * 40)["full"])  # local links widen project changes, not a canary

    def test_pin_only_change_runs_only_the_gitops_contract(self):
        pins = ["infra/gitops/environments/development/registration-image.json",
                "infra/gitops/environments/development/registration-publication.json"]
        plan = scope.select(FILES, pins)
        self.assertEqual((plan["pins"], plan["infra"], plan["full"], plan["projects"]), (True, False, False, []))
        self.assertTrue(plan["scripts"])
        for mixed in (pins + ["infra/gitops/argocd/values.yaml"], pins + ["services/a/api.py"],
                      ["infra/gitops/environments/development/runtime.json"]):
            with self.subTest(mixed=mixed):
                self.assertFalse(scope.select(FILES, mixed)["pins"])

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
        self.assertFalse(plan["scripts"])
        self.assertEqual(plan["projects"], [])

    def test_infra_python_consumers_run_and_reject_broken_implementation(self):
        for implementation in ("infra/envs/cost-aws/verify.py", "infra/envs/source-aws/preflight.py"):
            with self.subTest(implementation=implementation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                module = root / implementation
                module.parent.mkdir(parents=True)
                module.write_text("def ready():\n    return True\n")
                tests = root / "scripts/tests"
                tests.mkdir(parents=True)
                (tests / "test_consumer.py").write_text(textwrap.dedent(f"""\
                    import importlib.util
                    import unittest
                    from pathlib import Path

                    spec = importlib.util.spec_from_file_location(
                        "infra_consumer", Path(__file__).parents[2] / {implementation!r})
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    class ConsumerTest(unittest.TestCase):
                        def test_ready(self):
                            self.assertTrue(module.ready())
                    """))
                plan = scope.select(FILES + [implementation], [implementation])
                self.assertTrue(plan["infra"])
                self.assertFalse(plan["full"])
                self.assertEqual(plan["projects"], [])
                self.assertTrue(plan["scripts"], "platform Repository tools would be skipped")

                # The selected workflow step runs unittest discovery, including infra importers.
                command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "scripts/tests", "-v"]
                healthy = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=10, check=False)
                self.assertEqual(healthy.returncode, 0, healthy.stderr)
                module.write_text("def ready(:\n    return True\n")
                broken = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=10, check=False)
                self.assertNotEqual(broken.returncode, 0)
                self.assertIn("SyntaxError", broken.stderr)

    def test_deleted_infra_python_still_selects_consumers(self):
        plan = scope.select(FILES, ["infra/envs/source-aws/preflight.py"])
        self.assertTrue(plan["scripts"])

    def test_application_html_and_assets_are_runtime_inputs(self):
        for path in ("services/a/index.html", "services/a/assets/logo.svg", "services/a/content/page.md"):
            self.assertEqual(scope.select(FILES, [path])["projects"], [{"path": "services/a", "kind": "python"}])

    def test_service_network_policy_is_also_scanned_as_infrastructure(self):
        plan = scope.select(FILES, ["services/a/templates/30-networkpolicy.yaml"])
        self.assertTrue(plan["infra"])
        self.assertEqual(plan["projects"], [{"path": "services/a", "kind": "python"}])

    def test_shared_unknown_and_deleted_manifest_select_all(self):
        # The CI definition itself (ci.yml, scripts/ci_*) now runs a per-language canary instead; see below.
        for path in ["schemas/proposal.json",
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

    def test_tool_and_infra_projects_keep_their_own_dependency_checks(self):
        for directory in ("scripts/iam-console", "infra/validator"):
            with self.subTest(directory=directory):
                manifest = directory + "/pyproject.toml"
                files = FILES + [manifest]
                for changed in (manifest, directory + "/client.py", directory + "/tests/test_client.py"):
                    plan = scope.select(files, [changed])
                    self.assertEqual(plan["projects"], [{"path": directory, "kind": "python"}])
                    self.assertEqual(plan["scripts"], directory.startswith("scripts/") or changed.endswith(".py"))
                    self.assertEqual(plan["infra"], directory.startswith("infra/"))

    def test_removing_tool_manifest_still_checks_all_consumers(self):
        plan = scope.select(FILES, ["scripts/iam-console/pyproject.toml"])
        self.assertTrue(plan["full"])

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
