"""Replay retained source observations through T10 storage; never enroll production evidence.

The plan, principal and authorization policy are isolated qualification fixtures.
No target ControlRun is created. Missing target/context must prevent evaluation.
This is a local interoperability tool, not a runtime evidence upload endpoint.
"""

import argparse
import importlib.util
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.plans import PlanSpec, PlanStore
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.store import EventError

from bravo_gate.contract import T5Contract, canonical, decode, fingerprint
from bravo_gate.models import GateContext, RunSnapshot


def rejected(operation):
    try:
        operation()
    except EventError as error:
        return str(error)
    raise AssertionError("unconfirmed source-only operation was accepted")


def exercise(store, raw):
    source = RunSnapshot("retained-source", raw)
    document = source.document
    contract = T5Contract()
    contract.validate(document, "control_run")
    plan = PlanSpec("qualification-source-only", "qualification-tenant", document["app"],
                    document["environment"]["id"], "qualification-missing-target", "a" * 64, "b" * 64)
    actor = Principal("qualification-operator", plan.tenant_id)
    store.register_plan(plan, run_id="qualification", occurred_at=datetime.now(UTC).isoformat())
    # Deliberately local fixture policy; not production IdP/provenance authentication.
    def authorize(_connection, principal, row, action):
        return (principal == actor and row["plan_id"] == plan.plan_id
                and action in {"gate:record", "gate:bind", "gate:read", "gate:evaluate"})
    records = GateRecords(store, authorize=authorize)
    first = records.record_run(actor, plan.plan_id, "source", source)
    assert first == records.record_run(actor, plan.plan_id, "source", source)
    with store._transaction(write=False, plan_id=plan.plan_id) as connection:
        current = dict(connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan.plan_id,)).fetchone())
        read = records.read_run(connection, actor, current, source.ref)
        assert read.raw == raw and read.fingerprint == source.fingerprint
        row_count = connection.execute("SELECT count(*) AS n FROM plan_commands WHERE plan_id=?", (plan.plan_id,)).fetchone()["n"]
        assert row_count == 1
    context = GateContext(plan.tenant_id, plan.workload_id, "qualification-gate", plan.plan_digest,
                          "full", "1", contract.catalog_digest,
                          document["environment"]["id"], document["environment"]["ref"],
                          plan.target_environment_id, "qualification:no-target", source.ref, "absent-target",
                          source.fingerprint, "0" * 64)
    missing_target = rejected(lambda: records.bind(actor, plan.plan_id, "source-only", context, expected_revision=0))
    assert missing_target == "gate_run_unavailable_or_changed"
    factory_calls = []
    def factory(view):
        factory_calls.append(view)
        raise AssertionError("source-only storage must stop before evaluator construction")
    missing_context = rejected(lambda: records.evaluate(actor, plan.plan_id, service_factory=factory))
    assert missing_context == "gate_context_unavailable" and not factory_calls
    denied = rejected(lambda: GateRecords(store, authorize=lambda *_: False).record_run(actor, plan.plan_id, "source", source))
    assert denied == "forbidden"
    # Source-as-target reuse is rejected without editing the actual source bytes.
    wrong_side = rejected(lambda: records.record_run(actor, plan.plan_id, "target", source))
    assert wrong_side == "gate_run_scope_mismatch"
    return dict(source_raw_preserved=True, source_fingerprint=source.fingerprint,
                source_run_id=document["run_id"], stored_source_rows=row_count,
                retry_deduplicated=True, missing_target=missing_target, missing_context=missing_context,
                denied_writer=denied, source_as_target=wrong_side, evaluator_calls=len(factory_calls),
                target_document_created=False, authorizes_cutover=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--expected-fingerprint", required=True,
                        help="operator-verified retained input; integrity only, never producer authority")
    parser.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    root = args.private_output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    with args.source_run.open("rb") as stream:
        raw = stream.read(1048577)
    if fingerprint(decode(raw)) != args.expected_fingerprint:
        raise ValueError("retained_source_changed")
    if args.backend == "sqlite":
        with tempfile.TemporaryDirectory(prefix="bravo-source-staging-") as directory:
            result = exercise(PlanStore(Path(directory) / "plans.db"), raw)
    else:
        fixture = Path(__file__).parents[2] / "platform-events/tests/postgres_fixture.py"
        spec = importlib.util.spec_from_file_location("qualification_postgres", fixture)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cluster = module.TemporaryPostgres()
        try:
            result = exercise(PostgresPlanStore(cluster.conninfo(cluster.database(), "fixture_runtime")), raw)
        finally:
            cluster.close()
    result.update(scope="retained actual source; local T10 plan, principal and grants",
                  backend=args.backend, operational_registration=False, production_resources_changed=False)
    (root / "summary.json").write_bytes(canonical(result))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
