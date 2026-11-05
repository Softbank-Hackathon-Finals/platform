"""Real journals/mailboxes/encryption with synthetic GitHub and model results."""

import hashlib
import io
import json
import zipfile
from copy import deepcopy

import pytest
from bravo_agent.contracts import Proposal, digest
from bravo_agent.product_execution_contracts import (
    ARTIFACT,
    REPOSITORY,
    WORKFLOW,
    ExecutionIntent,
    ExecutionPending,
    ExecutionUnknown,
)
from bravo_agent.subscription_product_seal import seal_bound_receipt, seal_receipt
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from test_cutover import SPEC
from test_initial_proposal import ACTOR, RUNTIME, SOURCE, Fixture

from bravo_platform_events.initial_proposal import InitialIntent
from bravo_platform_events.initial_proposal_transport import (
    FileInitialProposal,
    OperatorInitialProposal,
    initial_receipt_binding,
    open_initial_receipt,
    seal_initial_receipt,
)
from bravo_platform_events.store import EventError


class GitHub:
    """Synthetic provider, never invokes gh or a network client."""

    def __init__(self, intent, receipt):
        self.intent, self.posts, self.lose_response = intent, [], False
        self.run = dict(id=1234, event="workflow_dispatch", head_branch="develop", head_sha=RUNTIME,
            run_attempt=1, workflow_id=42, actor={"id": 7}, head_repository={"full_name": REPOSITORY},
            display_title="product-" + intent.request_id, status="completed", conclusion="success")
        self.runs = [self.run]
        self.artifact_for(receipt)

    def artifact_for(self, receipt):
        # Malformed receipts can be sealed by an untrusted producer; the reader must reject them.
        sealed = seal_bound_receipt(self.intent.recipient_public_key,
                                   initial_receipt_binding(self.intent, 1234), receipt)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("product-result.json", json.dumps(sealed))
        self.raw = output.getvalue()
        self.artifact = dict(id=88, name=ARTIFACT, expired=False, size_in_bytes=len(self.raw),
            workflow_run={"id": 1234, "head_sha": RUNTIME},
            digest="sha256:" + hashlib.sha256(self.raw).hexdigest())

    def get(self, path):
        if path == "user":
            return {"id": 7}
        if path.endswith("git/ref/heads/develop"):
            return {"object": {"sha": RUNTIME}}
        if path.endswith("/workflows/" + WORKFLOW):
            return dict(id=42, path=".github/workflows/" + WORKFLOW, state="active")
        if "/workflows/42/runs?" in path:
            return {"workflow_runs": deepcopy(self.runs)}
        if path.endswith("/runs/1234"):
            return deepcopy(self.run)
        if path.endswith("/runs/1234/artifacts?per_page=100"):
            return {"total_count": 1, "artifacts": [deepcopy(self.artifact)]}
        raise AssertionError(path)

    def dispatch(self, workflow_id, inputs):
        assert workflow_id == 42 and inputs == self.intent.model_dump()
        self.posts.append(inputs)
        if self.lose_response:
            raise OSError("synthetic response loss")

    def download(self, artifact_id):
        assert artifact_id == 88
        return self.raw


@pytest.fixture
def setup(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    fields = f.port.preparation_fields(SPEC.plan_id, 3, **f.ref)
    operator = OperatorInitialProposal(tmp_path / "operator")
    intent = InitialIntent(**fields, recipient_public_key=operator.recipient(fields["request_id"]))
    value = f.records.read_input(ACTOR, intent.plan_id, intent.plan_version,
                                input_ref=intent.input_ref, input_digest=intent.input_digest)
    proposal = Proposal(run_id=value["run_id"], source_sha=SOURCE, risk="low",
        rationale="Synthetic initial proposal", edits=[dict(path="app/config.py",
            before_sha256=value["manifest"]["files"]["app/config.py"], replacement="VALUE = 2\n")])
    result = dict(request_id=intent.request_id, input_digest=intent.input_digest,
        runtime_source_sha=RUNTIME, execution_receipt_id="github-1234-1", proposal=proposal.model_dump(),
        proposal_digest=digest(proposal), schema_valid=True, lint_passed=True)
    receipt = initial_receipt_binding(intent, 1234) | dict(status=200, cleanup_confirmed=True, result=result)
    root = tmp_path / "mailbox"
    root.mkdir(mode=0o700)
    mailbox = FileInitialProposal(root)
    mailbox.prepare(intent)
    f.client, f.port = mailbox, None
    f.port = f.execution()
    github = GitHub(intent, receipt)
    operator.github = github
    return f, mailbox, operator, github, intent, receipt, value


def test_one_delivery_then_durable_result_has_no_client_calls(setup, monkeypatch):
    f, mailbox, operator, gh, intent, receipt, _ = setup
    with pytest.raises(ExecutionPending):
        f.execute()
    assert gh.posts == [] and mailbox.claimed() == intent
    mailbox.deliver(intent, operator.submit(mailbox.claimed()))
    result = f.port.read(SPEC.plan_id, 3, **f.ref)
    assert result == receipt["result"] and len(gh.posts) == 1
    monkeypatch.setattr(mailbox, "validate", lambda *_: pytest.fail("stored result called client"))
    monkeypatch.setattr(mailbox, "read", lambda *_: pytest.fail("stored result read provider"))
    assert f.execution().execute(SPEC.plan_id, 3, **f.ref) == result
    assert f.execution().stored(SPEC.plan_id, 3, **f.ref) == result
    f.publish()
    assert f.resume() == "REHEARSE"
    assert f.execution().stored(SPEC.plan_id, 3, **f.ref) == result
    with f.backend.store._transaction() as c:
        saved = c.execute("SELECT receipt_json FROM plan_commands WHERE command_key LIKE ?",
                          ("initial-proposal:%:execution:result",)).fetchone()["receipt_json"]
    assert json.loads(saved) == result and "ciphertext" not in saved and "github_run_id" not in saved


def test_lost_dispatch_response_restarts_as_same_run_read(setup):
    f, mailbox, operator, gh, intent, receipt, _ = setup
    with pytest.raises(ExecutionPending):
        f.execute()
    gh.lose_response = True
    with pytest.raises(ExecutionUnknown, match="DISPATCH_RESPONSE_UNCONFIRMED"):
        operator.submit(intent)
    restarted = OperatorInitialProposal(operator.root, gh)
    mailbox.deliver(intent, restarted.submit(intent))
    assert f.execute() == receipt["result"] and len(gh.posts) == 1


@pytest.mark.parametrize("key,value", [
    ("actor", {"id": 8}), ("run_attempt", 2), ("head_sha", "d" * 40),
    ("workflow_id", 43), ("head_repository", {"full_name": "another/repository"}),
    ("display_title", "other-request"), ("conclusion", "failure"),
])
def test_other_provider_run_never_delivers_or_retries(setup, key, value):
    f, mailbox, operator, gh, intent, _, _ = setup
    with pytest.raises(ExecutionPending):
        f.execute()
    gh.run[key] = value
    for _ in range(2):
        with pytest.raises((ExecutionUnknown, ExecutionPending)):
            operator.submit(intent)
    assert len(gh.posts) == 1 and not (mailbox.root / "receipt.json").exists()


@pytest.mark.parametrize("key,value", [
    ("plan_version", 4), ("purpose", "t38-product-proposal"), ("binding_digest", "e" * 64),
    ("cleanup_confirmed", False), ("status", True), ("github_run_attempt", True),
])
def test_authenticated_wrong_receipt_binding_is_rejected(setup, key, value):
    _, _, operator, gh, intent, receipt, _ = setup
    changed = deepcopy(receipt)
    changed[key] = value
    gh.artifact_for(changed)
    with pytest.raises(ExecutionUnknown, match="ARTIFACT_RECEIPT_INVALID"):
        operator.submit(intent)


@pytest.mark.parametrize("key,value", [
    ("schema_valid", False), ("lint_passed", False), ("lint_passed", 1),
    ("execution_receipt_id", "github-999-1"), ("proposal_digest", "e" * 64),
    ("metadata", {"untrusted": True}),
])
def test_failed_or_invented_model_checks_never_become_t10_result(setup, key, value):
    _, _, operator, gh, intent, receipt, _ = setup
    changed = deepcopy(receipt)
    changed["result"][key] = value
    gh.artifact_for(changed)
    with pytest.raises(ExecutionUnknown, match="ARTIFACT_RECEIPT_INVALID"):
        operator.submit(intent)


def test_local_validate_requires_operator_delivery_and_exact_input(setup):
    f, mailbox, operator, _, intent, receipt, value = setup
    with pytest.raises(ExecutionPending):
        f.execute()
    with pytest.raises(ExecutionPending):
        mailbox.validate(intent, value, receipt)
    mailbox.deliver(intent, operator.submit(intent))
    wrong = deepcopy(value)
    wrong["binding"]["plan_version"] += 1
    with pytest.raises(ExecutionUnknown, match="INITIAL_INPUT_CONFLICT"):
        mailbox.validate(intent, wrong, receipt)
    assert mailbox.validate(intent, value, receipt) == receipt["result"]


def test_receipt_ciphertext_is_bound_to_initial_purpose_and_plan_version(setup):
    _, _, operator, _, intent, receipt, _ = setup
    sealed = seal_initial_receipt(intent, 1234, receipt)
    private = operator._private_key(intent.request_id)
    assert open_initial_receipt(intent, 1234, sealed, private) == receipt
    wrong = intent.model_copy(update={"plan_version": 4})
    with pytest.raises(InvalidTag):
        open_initial_receipt(wrong, 1234, sealed, private)
    legacy = ExecutionIntent(request_id=intent.request_id, input_ref=intent.input_ref,
        input_digest=intent.input_digest, admission_digest=intent.binding_digest,
        runtime_source_sha=RUNTIME, recipient_public_key=intent.recipient_public_key, mode="propose")
    with pytest.raises(InvalidTag):
        open_initial_receipt(intent, 1234, seal_receipt(legacy, 1234, receipt), private)


@pytest.mark.parametrize("change", [{"purpose": "t38-product-proposal"}, {"plan_version": True}])
def test_constructed_invalid_intent_is_revalidated_before_send(setup, change):
    _, mailbox, operator, gh, intent, _, _ = setup
    invalid = intent.model_copy(update=change)
    with pytest.raises(ValidationError):
        mailbox.prepare(invalid)
    with pytest.raises(ValidationError):
        operator.submit(invalid)
    assert gh.posts == [] and not (mailbox.root / "intent.json").exists()


def test_permission_revocation_blocks_result_without_new_dispatch(setup):
    f, mailbox, operator, gh, intent, _, _ = setup
    with pytest.raises(ExecutionPending):
        f.execute()
    mailbox.deliver(intent, operator.submit(intent))
    f.grant(False, 1)
    with pytest.raises(EventError):
        f.port.read(SPEC.plan_id, 3, **f.ref)
    assert len(gh.posts) == 1 and f.result_rows() == 0
