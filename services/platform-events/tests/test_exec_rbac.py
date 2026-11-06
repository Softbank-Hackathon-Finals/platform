"""T42 RBAC: the runner may act as space A's `rehearsal-exec` account only on an exec attempt."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

SCOPED = Path(__file__).resolve().parents[3] / "infra/envs/rehearsal-access/scoped_lifecycle.py"
spec = importlib.util.spec_from_file_location("t7_scoped_lifecycle_exec", SCOPED)
scoped = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoped)

SPACE = SimpleNamespace(name="rehearsal-" + "a" * 24, uid="uid-a")


def impersonation(grants):
    role = next(item for item in grants["items"] if item["kind"] == "Role")
    return sorted(name for rule in role["rules"] if "impersonate" in rule["verbs"]
                  for name in rule["resourceNames"])


def test_exec_attempt_adds_only_the_exec_account_impersonation():
    off, on = scoped.attempt_grants(SPACE), scoped.attempt_grants(SPACE, exec_enabled=True)
    assert impersonation(off) == ["rehearsal-intruder"]
    assert impersonation(on) == ["rehearsal-exec", "rehearsal-intruder"]
    # Nothing else changes: same objects, same owner, no cluster-wide or Secret-wide growth.
    strip = [rule for rule in next(i for i in on["items"] if i["kind"] == "Role")["rules"]
             if rule.get("resourceNames") != ["rehearsal-exec"]]
    assert strip == next(i for i in off["items"] if i["kind"] == "Role")["rules"]
    assert [i for i in on["items"] if i["kind"] != "Role"] == [i for i in off["items"] if i["kind"] != "Role"]


def test_only_exactly_true_enables_exec_impersonation():
    for value in (None, 1, "true", [True]):
        assert impersonation(scoped.attempt_grants(SPACE, exec_enabled=value)) == ["rehearsal-intruder"]


class Kube:
    def __init__(self):
        self.created = None

    def run(self, *args, stdin=None):
        import json
        self.created = json.loads(stdin)
        return SimpleNamespace(returncode=0)

    def get_json(self, kind, name, *rest):
        return next(item for item in self.created["items"]
                    if item["kind"] == kind and item["metadata"]["name"] == name)


class Inner:
    def __init__(self):
        self.kube = Kube()

    def create(self, run_id):
        return SPACE

    def _require_uid(self, namespace, run_id):
        pass

    def cleanup(self, namespace):
        raise AssertionError("no cleanup on success")


def test_scoped_lifecycle_sends_exec_grants_only_for_space_a():
    space_a, grading = Inner(), Inner()
    scoped.ScopedLifecycle(space_a, exec_enabled=True).create("run-1")
    scoped.ScopedLifecycle(grading).create("run-1")
    assert impersonation(space_a.kube.created) == ["rehearsal-exec", "rehearsal-intruder"]
    assert impersonation(grading.kube.created) == ["rehearsal-intruder"]
