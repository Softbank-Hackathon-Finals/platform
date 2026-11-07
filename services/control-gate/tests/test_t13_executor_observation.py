"""The Executor's pre-cutover observation (bravo_cutover.readiness) packaged for T19 and judged.

The Executor code runs for real against fake DB connections (the PostgreSQL paths are tested in
services/cutover-executor/tests/test_readiness.py); packaging, the server policy and the judgment are
the production ones.
"""

import copy
from datetime import timedelta

import pytest
from bravo_cutover.db_fingerprint import HASH_RULE, SequenceState
from bravo_cutover.readiness import Readiness, ReadinessRequest
from bravo_cutover.remote.db import Subscription
from bravo_cutover.sites import Config, Settings, Site
from bravo_platform_events.gate_readiness import POLICY
from bravo_platform_events.readiness_observer import request_for

from bravo_gate.contract import GateError
from bravo_gate.readiness import assess_readiness
from bravo_gate.t13 import snapshot_from_observation


class FakeDb:
    def __init__(self, world, side):
        self.w, self.side = world, side

    def schema_fingerprint(self, table):
        return {"schema_hash": self.w["schema"][self.side].get(table, "a" * 64), "primary_key": ["id"]}

    def subscription(self, name):
        return self.w["sub"]

    def received_lsn(self, name):
        return 900

    def current_wal_lsn(self):
        return 1000

    def can_login(self, role):
        return self.w["login"]

    def sessions(self, role):
        return 0

    def sequence_state(self, sequence, table, column):
        return self.w["seq"][self.side]

    def raise_sequence(self, sequence, next_value):
        s = self.w["seq"][self.side]
        self.w["seq"][self.side] = SequenceState(next_value, False, s.max_id, s.maximum)
        return next_value


def site(env, dsn):
    return Site(environment_id=env, resource_id=f"cnpg-{dsn}/chat", connection_ref=f"secret/{dsn}",
                kube_context="fake", namespace=dsn, subscription=f"sub_{dsn}", pgbouncer_admin_dsn="fake",
                pgbouncer_app_dsn="fake", db_control_dsn=dsn, db_app_dsn="fake", db_host=f"{dsn}-db")


@pytest.fixture
def observe(service):
    _, state = service
    ctx = state["context"]
    world = {"schema": {"src": {}, "dst": {}}, "sub": Subscription(enabled=True, workers=1), "login": False,
             "seq": {"src": SequenceState(20, True, 20, 2**63 - 1), "dst": SequenceState(3, True, 20, 2**63 - 1)}}
    config = Config(settings=Settings(), origin=ctx.source_environment_id,
                    sites={ctx.source_environment_id: site(ctx.source_environment_id, "src"),
                           ctx.target_environment_id: site(ctx.target_environment_id, "dst")})
    pair = {side: {"environment_id": getattr(ctx, side + "_environment_id")} for side in ("source", "target")}

    def run():
        executor = Readiness(config, db=lambda dsn, control_functions: FakeDb(world, dsn),
                             clock=lambda: state["now"])
        return executor.observe(ReadinessRequest(**request_for(pair)))

    return ctx, state["now"], world, run


def judged(ctx, now, observation):
    binding, snapshot, diagnostics = snapshot_from_observation(ctx, POLICY, observation, snapshot_ref="executor-1")
    result = assess_readiness(ctx, binding, snapshot, now=now + timedelta(seconds=5))
    return {r["test_id"]: r["reason"] for r in result["rows"]}, result["decision"], diagnostics


def test_the_server_policy_is_t13s_rule_and_the_executors_scope():
    assert POLICY.schema_rule == HASH_RULE
    settings = Settings()
    assert set(POLICY.tables) == set(settings.tables) and POLICY.max_lag_bytes == settings.max_lag_bytes
    assert {tuple(pair) for pair in settings.sequences.values()} == set(POLICY.sequence_columns)


def test_a_closed_target_with_matching_schema_passes(observe):
    ctx, now, world, run = observe
    rows, decision, diagnostics = judged(ctx, now, run())
    assert decision == "PASS" and rows["public.messages:id:sequence"] == "READY"
    assert rows["replication:lag"] == "READY" and world["seq"]["dst"].next_value == 21
    assert diagnostics == {"source": {}, "target": {}, "replication": None, "sequences": {}}


@pytest.mark.parametrize(("change", "row", "reason"), [
    (lambda w: w["schema"]["dst"].update({"public.messages": "c" * 64}), "public.messages:schema", "SCHEMA_MISMATCH"),
    (lambda w: w.update(sub=Subscription(enabled=False, workers=0)), "replication:lag", "UNKNOWN"),
    (lambda w: w.update(login=True), "public.messages:id:sequence", "MISSING_SEQUENCE"),
])
def test_what_the_executor_could_not_confirm_blocks(observe, change, row, reason):
    ctx, now, world, run = observe
    change(world)
    rows, decision, _ = judged(ctx, now, run())
    assert decision == "BLOCK" and rows[row] == reason


def test_a_stale_observation_blocks(observe):
    ctx, now, _, run = observe
    binding, snapshot, _ = snapshot_from_observation(ctx, POLICY, run(), snapshot_ref="executor-1")
    late = assess_readiness(ctx, binding, snapshot, now=now + timedelta(seconds=31))
    assert late["decision"] == "BLOCK"
    assert {r["reason"] for r in late["rows"] if r["test_id"].endswith("freshness")} == {"STALE_OR_FUTURE"}


def test_diagnostics_never_enter_the_signed_snapshot(observe):
    ctx, now, world, run = observe
    world["login"] = True
    observation = run()
    binding, snapshot, diagnostics = snapshot_from_observation(ctx, POLICY, observation, snapshot_ref="executor-1")
    assert diagnostics["sequences"] == {"public.messages:id": "target_open"}
    assert "errors" not in snapshot.document["source"] and "sequence_errors" not in snapshot.document
    assert snapshot.document["source"]["environment_ref"] == ctx.source_environment_ref
    assert binding.source_database_ref == "cnpg-src/chat"


@pytest.mark.parametrize(("change", "code"), [
    (lambda o: o.update(kind="data-readiness.v1"), "READINESS_OBSERVATION_INVALID"),
    (lambda o: o.update(schema_rule="v1:pg-types-collation"), "READINESS_OBSERVATION_INVALID"),
    (lambda o: o["source"].update(extra=True), "READINESS_OBSERVATION_INVALID"),
    (lambda o: o["target"].update(environment_id="env-other"), "READINESS_RESOURCE_MISMATCH"),
    (lambda o: o["source"]["tables"].pop(), "READINESS_OBSERVATION_SCOPE"),
    (lambda o: o["source"]["tables"].reverse(), "READINESS_OBSERVATION_SCOPE"),
    (lambda o: o.update(sequences={}), "READINESS_OBSERVATION_INVALID"),
])
def test_an_observation_outside_the_context_or_policy_is_refused(observe, change, code):
    ctx, _, _, run = observe
    observation = copy.deepcopy(run())
    change(observation)
    with pytest.raises(GateError, match=code):
        snapshot_from_observation(ctx, POLICY, observation, snapshot_ref="executor-1")
