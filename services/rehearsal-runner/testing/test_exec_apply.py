"""Apply action with T11's real Proposal check. Run from the agent-worker environment:

cd ../agent-worker
PYTHONPATH=../rehearsal-runner/src:../rehearsal-runner/tests uv run --frozen --no-default-groups --with pytest \
  --with jsonschema pytest -p no:cacheprovider ../rehearsal-runner/testing/test_exec_apply.py
"""

import hashlib

import bravo_agent.contracts as bravo
import pytest
from exec_schema import assert_valid

from rehearsal_runner.exec_apply import Deployed, ProposalApply, t11_proposal_check
from rehearsal_runner.exec_contract import args_sha256
from rehearsal_runner.exec_session import ExecSession, Space, Transient

SPACE = Space("rehearsal-a1", "uid-a")
SOURCE = "def password():\n    return boto3.client('secretsmanager')\n"
SHA = hashlib.sha256(SOURCE.encode()).hexdigest()


class Snapshot:
    class manifest:
        source_sha = "a" * 40
        files = {"app/db_secret.py": SHA, "app/main.py": "f" * 64}
        editable_paths = ["app/db_secret.py"]

    def read(self, path):
        assert path == "app/db_secret.py"
        return SOURCE


class Coordinator:
    snapshot = Snapshot()
    bundle = bravo.FailureBundle(run_id="run-1", source_sha="a" * 40, failure_code="TASK",
                                 summary="Read the injected credential file.", evidence_digest="e" * 64)


def draft(**override):
    edit = {"path": "app/db_secret.py", "before_sha256": SHA,
            "replacement": "def password():\n    return open('/run/bravo/database/credentials.json').read()\n"}
    return {"schema_version": 1, "run_id": "run-1", "source_sha": "a" * 40, "rationale": "use the file",
            "risk": "low", "edits": [edit | override.pop("edit", {})]} | override


def test_check_is_plan_propose_validation():
    check = t11_proposal_check(Coordinator())
    assert len(check(draft())) == 64
    for bad in (draft(run_id="run-2"), draft(edit={"path": "app/main.py"}),
                draft(edit={"before_sha256": "0" * 64}), draft(edit={"replacement": "rm -rf /"}),
                draft() | {"namespace": "x"}):
        with pytest.raises(ValueError):
            check(bad)


def session_with(deploy):
    apply = ProposalApply(SPACE, t11_proposal_check(Coordinator()), deploy)
    return ExecSession("run-1", 1, SPACE, {"apply": apply}, secrets=lambda: [], space_uid=lambda: "uid-a",
                       sleep=lambda _: None)


def request(arguments):
    return {"schema_version": 1, "run_id": "run-1", "attempt": 1, "request_id": "req-0001-apply",
            "args_sha256": args_sha256(arguments), "tool": "rehearsal.exec.apply", "arguments": arguments}


def test_apply_reports_template_targets_and_proposal_digest():
    seen = []

    def deploy(arguments, space, deadline):
        seen.append(space)
        return Deployed((("deployment", "chat"),), b"deployment.apps/chat configured\n")

    s = session_with(deploy)
    r = s.handle(request(draft()))
    assert r["result_code"] == "OK" and r["status"]["targets"] == [{"kind": "deployment", "name": "chat"}]
    assert_valid("response", r)
    assert s.events()[0]["proposal_digest"] == r["status"]["proposal_digest"] and seen == [SPACE]


def test_invalid_draft_never_reaches_deploy():
    calls = []
    s = session_with(lambda *a: calls.append(a))
    r = s.handle(request(draft(edit={"path": "app/main.py"})))
    assert (r["reason"], r["stage"]) == ("BLOCKED", "VALIDATE") and calls == []


def test_deploy_with_unknown_effect_closes_exec():
    def deploy(arguments, space, deadline):
        raise Transient()

    s = session_with(deploy)
    assert s.handle(request(draft()))["result_code"] == "UNKNOWN" and s.effect_unknown


def test_deployed_targets_are_template_names_only():
    with pytest.raises(ValueError):
        Deployed((("secret", "db"),))
    with pytest.raises(ValueError):
        Deployed((("pod", "-A"),))
