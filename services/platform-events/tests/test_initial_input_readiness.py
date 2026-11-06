"""The real input compiler must succeed before consuming a durable execution slot."""

import hashlib

import pytest
from bravo_agent.contracts import digest
from test_initial_proposal import ACTOR, EXPIRES, SOURCE, SOURCE_PATH, SPEC, Fixture
from test_workflow_recovery import ANALYSIS

from bravo_platform_events.cutover import _get, _put
from bravo_platform_events.initial_proposal import InitialInput
from bravo_platform_events.initial_proposal_runtime import model_input
from bravo_platform_events.store import EventError


def source(rationale):
    content = "VALUE = 1\n"
    return dict(schema_version=2,
        analysis=dict(inventory_digest=ANALYSIS.inventory_digest, item_id="item-001",
                      grade="code_change", rationale=rationale),
        manifest=dict(source_sha=SOURCE,
            files={SOURCE_PATH: hashlib.sha256(content.encode()).hexdigest()},
            editable_paths=[SOURCE_PATH]), files={SOURCE_PATH: content})


@pytest.mark.parametrize("length", [3791, 4096])
def test_unrepresentable_input_is_rejected_before_admission(backend, tmp_path, length):
    with pytest.raises(EventError, match="^initial_model_input_invalid$"):
        Fixture(backend, tmp_path, source_value=source("x" * length))
    with backend.store._transaction() as c:
        rows = c.execute("SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                         ("initial-proposal:%",)).fetchone()["n"]
    assert rows == 0
    assert backend.store.snapshot(SPEC.plan_id)["state"] == "PROPOSE"


def test_exact_boundary_preserves_original_and_executes_once(backend, tmp_path):
    original = source("한" * 3790)
    f = Fixture(backend, tmp_path, source_value=original)
    value = InitialInput.model_validate(f.records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref))
    assert len(model_input(value).bundle.summary) == 4096
    assert value.analysis.rationale == original["analysis"]["rationale"]
    result = f.execute()
    assert f.execution().execute(SPEC.plan_id, 3, **f.ref) == result
    assert len(f.client.submits) == 1


@pytest.mark.parametrize("entrypoint", ["preparation_fields", "execute"])
def test_legacy_admitted_invalid_input_never_prepares_key_or_claims(backend, tmp_path, entrypoint):
    f = Fixture(backend, tmp_path, source_value=source("x" * 3791), admit=False)
    # Import the previous release's valid stored format; production rows remain immutable.
    with backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        _, value = f.records._current(c, plan_id=SPEC.plan_id, tenant_id=SPEC.tenant_id,
            task_id="credential", version=3, principal=ACTOR)
        request = dict(principal=ACTOR, expires_at=EXPIRES, input=value.model_dump(mode="json"))
        f.ref = dict(input_ref="initial-legacy", input_digest=digest(value))
        _put(c, SPEC.plan_id, "initial-proposal:3", request, f.ref)
    with pytest.raises(EventError, match="^initial_model_input_invalid$"):
        getattr(f.port, entrypoint)(SPEC.plan_id, 3, **f.ref)
    assert not f.client.keys and not f.client.submits and not f.client.reads
    with backend.store._transaction() as c:
        assert _get(c, SPEC.plan_id, "initial-proposal:3")[1] == f.ref
        assert _get(c, SPEC.plan_id, "initial-proposal:3:execution") is None
