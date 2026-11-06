"""Real input/schema/policy checks and durable journal; synthetic model/cloud ports."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from bravo_agent.contracts import Proposal, digest
from bravo_agent.subscription_product import ProductAccess, ProposalJournal
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.secret_task import prepare_credential_migration

from bravo_platform_events.initial_proposal import InitialInput, InitialIntent
from bravo_platform_events.initial_proposal_runtime import (
    InitialBrokerRequest,
    InitialProposalService,
    model_input,
)

RUNTIME = "c" * 40
PRINCIPAL = "a" * 64


@pytest.fixture
def runtime(tmp_path):
    content = "VALUE = 1\n"
    binding = dict(plan_id="plan-a", tenant_id="tenant-a", plan_digest="d" * 64,
        target_environment_id="target-a", plan_version=3, task_id="task-a",
        task_digest=logical_task_digest(prepare_credential_migration("proposal-a", "b" * 40)),
        source_sha="b" * 40, collection_scope_digest="f" * 64, inventory_digest="1" * 64,
        comparison_digest="2" * 64, analysis_receipt_id="analysis-a")
    value = InitialInput(schema_version=2, binding=binding, run_id="proposal-a",
        analysis=dict(inventory_digest="1" * 64, item_id="item-001", grade="code_change",
                      rationale="Use the target configuration"),
        manifest=dict(source_sha="b" * 40, files={"app/config.py": hashlib.sha256(content.encode()).hexdigest()},
                      editable_paths=["app/config.py"]), files={"app/config.py": content})
    fields = dict(plan_id="plan-a", plan_version=3, input_ref="initial-a", input_digest=digest(value),
                  binding_digest=digest(value.binding), runtime_source_sha=RUNTIME, purpose="t38-initial-proposal")
    intent = InitialIntent(**fields, request_id="t10p-" + digest(fields | {"principal": PRINCIPAL}),
        recipient_public_key=X25519PrivateKey.generate().public_key().public_bytes_raw().hex())
    request = InitialBrokerRequest(intent=intent, github_run_id="1234", web_identity_token="synthetic-identity")
    counts = {"invoke": 0, "reads": 0}

    def read(lookup):
        counts["reads"] += 1
        assert lookup.principal == PRINCIPAL and lookup.intent == intent
        return value

    def invoke(access, prepared, root, snapshot):
        counts["invoke"] += 1
        assert not hasattr(prepared, "admission_digest")
        proposal = Proposal(run_id=value.run_id, source_sha=value.binding.source_sha, risk="low",
            rationale="Synthetic proposal", edits=[dict(path="app/config.py",
                before_sha256=value.manifest.files["app/config.py"], replacement="VALUE = 2\n")])
        return dict(state="PROPOSAL_READY", input_kind="product", attempt=1,
            run_id=value.run_id, source_sha=value.binding.source_sha, input=prepared.bundle.model_dump(),
            input_digest=digest(prepared.bundle), task=prepared.bundle.model_dump(), task_digest=digest(prepared.bundle),
            snapshot_digest=digest(value.manifest), proposal=proposal.model_dump(), proposal_digest=digest(proposal))

    service = InitialProposalService(ProposalJournal(tmp_path / "journal"),
        lambda token: ProductAccess(PRINCIPAL, {}) if token == request.web_identity_token else None,
        lambda token: read, invoke, RUNTIME)
    return service, request, value, counts


def test_v2_mapping_real_schema_lint_and_saved_same_result(runtime):
    service, request, value, counts = runtime
    bundle = model_input(value).bundle
    assert bundle.failure_code == "CODE_CHANGE_REQUIRED" and value.analysis.rationale in bundle.summary
    target = prepare_credential_migration(value.run_id, value.binding.source_sha).as_model_input()
    assert target["target_credential_file"] in bundle.summary
    assert "preserving cache and authentication-failure refresh" in bundle.summary
    assert "Fail closed" in bundle.summary and "never log credentials" in bundle.summary
    assert bundle.evidence_digest == value.binding.inventory_digest and bundle.stage == "REHEARSE"
    assert value.binding.stage == "PROPOSE"
    status, first = service.run(request.model_dump())
    assert status == 200 and first["schema_valid"] is True and first["lint_passed"] is True
    restarted = replace(service, journal=ProposalJournal(service.journal.root))
    assert restarted.run(request.model_dump()) == (200, first)
    assert counts == {"invoke": 1, "reads": 3}
    assert not list(service.journal.root.glob("initial-*"))
    assert request.web_identity_token not in service.journal.path.read_bytes().decode(errors="replace")


def test_model_target_contract_must_match_the_registered_task_digest(runtime):
    _, _, value, _ = runtime
    original = value.model_dump_json()
    model_input(value)
    assert value.model_dump_json() == original  # The original inventory is unchanged.
    wrong = value.model_copy(update={"binding": value.binding.model_copy(update={"task_digest": "0" * 64})})
    with pytest.raises(ValueError, match="initial_task_contract_unavailable"):
        model_input(wrong)


@pytest.mark.parametrize("fault", ["schema", "policy", "source", "task", "snapshot", "after-model-authority"])
def test_real_validation_failure_never_becomes_true_and_never_reinvokes(runtime, fault):
    service, request, value, counts = runtime
    original = service.invoke
    current = service.reader(request.web_identity_token)
    revoked = [False]

    def read(lookup):
        if revoked[0]:
            raise ValueError("private source detail")
        return current(lookup)

    service.reader = lambda _: read

    def invalid(*args):
        raw = original(*args)
        if fault == "schema":
            raw["proposal"]["unknown"] = True
        elif fault == "policy":
            raw["proposal"]["edits"][0]["replacement"] = "# rm -rf /tmp/data\n"
            raw["proposal_digest"] = digest(raw["proposal"])
        elif fault == "source":
            raw["proposal"]["edits"][0]["before_sha256"] = "f" * 64
            raw["proposal_digest"] = digest(raw["proposal"])
        elif fault == "task":
            raw["task"]["summary"] = "different input"
        elif fault == "snapshot":
            raw["snapshot_digest"] = "0" * 64
        else:
            revoked[0] = True
        return raw

    service.invoke = invalid
    assert service.run(request.model_dump())[0] == 409
    assert service.run(request.model_dump())[0] != 200
    assert counts["invoke"] == 1
    assert "private source detail" not in service.journal.path.read_bytes().decode(errors="replace")


def test_response_loss_and_new_provider_run_do_not_call_model_twice(runtime):
    service, request, _, counts = runtime
    original = service.invoke

    def lost(*args):
        original(*args)
        raise OSError("private transport detail")

    service.invoke = lost
    assert service.run(request.model_dump())[0] == 409
    assert service.run(request.model_dump())[0] == 409
    assert service.run(request.model_dump() | {"github_run_id": "9999"})[0] == 409
    assert counts["invoke"] == 1


def test_claim_survives_termination_without_a_result(runtime):
    service, request, value, counts = runtime
    from bravo_platform_events.initial_proposal_api import InitialRead

    lookup = InitialRead(principal=PRINCIPAL, intent=request.intent, github_run_id=request.github_run_id)
    service.journal.claim("initial:" + value.run_id, digest(lookup))
    assert service.run(request.model_dump())[0] == 409
    assert counts["invoke"] == 0


def test_concurrent_same_request_invokes_once(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    service, request, _, counts = runtime
    entered, finish = Event(), Event()
    original = service.invoke

    def delayed(*args):
        entered.set()
        assert finish.wait(5)
        return original(*args)

    service.invoke = delayed
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.run, request.model_dump())
        assert entered.wait(5)
        second = executor.submit(service.run, request.model_dump())
        assert second.result(timeout=5)[0] == 409
        finish.set()
        assert first.result(timeout=5)[0] == 200
    assert counts["invoke"] == 1


@pytest.mark.parametrize("field", ["schema_version", "analysis"])
def test_legacy_input_is_not_silently_upgraded(runtime, field):
    raw = runtime[2].model_dump()
    del raw[field]
    with pytest.raises(ValueError):
        InitialInput.model_validate(raw)










@pytest.mark.parametrize("fault", ["none", "plain", "wrong_ca", "wrong_name", "expired"])
def test_actual_initial_broker_tls_before_any_body(runtime, tmp_path, fault):
    import importlib.util
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from bravo_platform_events.initial_proposal_job import InitialTunnelCall

    path = Path(__file__).parents[2] / "agent-worker/tests/tls_support.py"
    spec = importlib.util.spec_from_file_location("initial_socket_tls", path)
    support = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(support)
    tls, ca = support.identity(tmp_path / "server", correct_name=fault != "wrong_name", expired=fault == "expired")
    if fault == "wrong_ca":
        _, ca = support.identity(tmp_path / "trusted")
    service, request, _, counts = runtime
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            status, result = service.run(json.loads(received[-1]))
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with HTTPServer(("127.0.0.1", 18762), Handler) as server:
        if fault != "plain":
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, result = InitialTunnelCall(ca, url="https://127.0.0.1:18762/run")(request.model_dump())
        finally:
            server.shutdown()
            thread.join(5)
            assert not thread.is_alive()
    if fault == "none":
        assert status == 200 and len(received) == counts["invoke"] == 1
    else:
        assert status == 502 and received == [] and counts["invoke"] == 0
    assert request.web_identity_token not in json.dumps(result)


def test_initial_installer_requires_reader_and_selects_exact_module():
    import importlib.util
    path = Path(__file__).parents[3] / "operations/subscription/install.py"
    spec = importlib.util.spec_from_file_location("initial_installer", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    settings = {k: "synthetic" for k in ("secret_arn", "runtime_host_id", "broker_role_arn")}
    model = {"provider": "chatgpt-plan", "model": "synthetic-model", "reasoning_effort": "medium"}
    settings["region"] = "ap-northeast-2"
    with pytest.raises(ValueError):
        installer.installation(RUNTIME, "d" * 64, settings, model, initial_proposal=True)
    script = installer.installation(RUNTIME, "d" * 64, settings, model, initial_proposal=True,
        product_reader={"origin": "https://reader.invalid", "ca_file": "/etc/reader-ca.pem"})
    assert f"platform-{RUNTIME}/services/platform-events/.venv/bin/python -m bravo_platform_events.initial_proposal_server" in script
    assert "--frozen --no-dev --group product-inputs" in script
    assert "systemctl start" not in script and "systemctl enable" not in script
