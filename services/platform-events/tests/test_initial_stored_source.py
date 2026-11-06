"""The deployed reader consumes admission without an operator checkout or writes."""

import json

import pytest
from test_cutover import SPEC
from test_initial_proposal import ACTOR, Fixture

from bravo_platform_events.initial_proposal import InitialProposalRecords
from bravo_platform_events.initial_stored_source import stored_initial_source
from bravo_platform_events.store import EventError


def reader(f, backend):
    return InitialProposalRecords(backend.store, catalog=f.catalog, source=stored_initial_source,
                                  authorize=f.access.require, clock=lambda: f.now)


def test_stored_source_survives_operator_snapshot_loss_and_obeys_revocation(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    expected = f.records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
    f.source_value.clear()
    records = reader(f, backend)
    assert records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref) == expected
    f.grant(False, 1)
    with pytest.raises(EventError):
        records.read_input(ACTOR, SPEC.plan_id, 3, **f.ref)


@pytest.mark.parametrize("mutation", ["missing", "source", "digest", "task"])
def test_missing_or_changed_admission_is_not_a_source(backend, tmp_path, mutation):
    f = Fixture(backend, tmp_path)
    with backend.admin() as raw:
        def execute(query, args):
            return raw.execute(query.replace("?", "%s") if backend.kind == "postgres" else query, args)
        if mutation == "missing":
            execute("DELETE FROM plan_commands WHERE plan_id=? AND command_key=?",
                      (SPEC.plan_id, "initial-proposal:3"))
        else:
            row = execute("SELECT request_json FROM plan_commands WHERE plan_id=? AND command_key=?",
                            (SPEC.plan_id, "initial-proposal:3")).fetchone()
            data = json.loads(row[0])
            if mutation == "source":
                data["input"]["files"][next(iter(data["input"]["files"]))] = "changed source\n"
            elif mutation == "task":
                data["input"]["binding"]["task_id"] = "another-task"
            else:
                data["input"]["analysis"]["rationale"] = "changed analysis"
            execute("UPDATE plan_commands SET request_json=? WHERE plan_id=? AND command_key=?",
                      (json.dumps(data), SPEC.plan_id, "initial-proposal:3"))
    with pytest.raises(EventError):
        reader(f, backend).read_input(ACTOR, SPEC.plan_id, 3, **f.ref)
