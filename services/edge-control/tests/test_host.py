"""Synthetic authority/provider with real T10 SQLite and canonical T36 decisions.

No live plan, approval, HTTP request, or Cloudflare resource is created here.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from bravo_observability.sli import Record, build_window
from bravo_platform_events.plans import PlanSpec, PlanStore
from test_control import BINDING, Provider

from bravo_edge_control.host import CurrentShift, ShiftHost
from bravo_edge_control.provider import Cloudflare, EdgeError

PLAN = "plan-host-fixture"
SPEC = PlanSpec(PLAN, "tenant-test", "chat", "source", "target", "a" * 64, "b" * 64,
                "demo-gate", "1")
START = datetime(2026, 11, 6, tzinfo=UTC)
PROFILE = json.loads((Path(__file__).resolve().parents[3]
    / "docs/observability/threshold-profiles.json").read_text())["profiles"][0]


class Fixture:
    def __init__(self, path):
        self.store = PlanStore(path / "plans.sqlite")
        self.store.register_plan(SPEC, run_id="unit-registration", occurred_at=START.isoformat())
        self.now = START
        self.allowed = True
        self.context = CurrentShift("approved-fixture", "run-fixture", "grant-gate-revision-1",
                                    deepcopy(PROFILE), asdict(BINDING))
        self.proof = dict(plan_id=PLAN, version=6, plan_digest=SPEC.plan_digest,
            target_environment_id=SPEC.target_environment_id, receipt_id="fixture-proof",
            result=dict(approval_id=self.context.approval_id, approved=True,
                expires_at=(START + timedelta(hours=1)).isoformat(), gate={"verdict": "PASS"}))
        with self.store._transaction(write=True, plan_id=PLAN) as c:
            c.execute("UPDATE plans SET state='SHIFT',version=7 WHERE plan_id=?", (PLAN,))
            c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                      (PLAN, "workflow:6", "{}", json.dumps(self.proof)))
        self.upstream = Provider()
        self.provider = Cloudflare(BINDING, lambda: "fixture-token",
                                   transport=httpx.MockTransport(self.upstream))
        self.journal = path / "edge.sqlite"
        self.host = self.reopen()

    def authority(self, c, principal, plan):
        assert c.execute("SELECT state FROM plans WHERE plan_id=?", (PLAN,)).fetchone()[0] == "SHIFT"
        if not self.allowed or principal != "authenticated-fixture" or plan["tenant_id"] != "tenant-test":
            raise EdgeError("fixture_authority_revoked")
        return self.context

    def reopen(self):
        return ShiftHost(PlanStore(self.store.path), self.provider, self.journal,
                         authority=self.authority, clock=lambda: self.now.isoformat())

    def start(self, operation="stage-10"):
        return self.host.start("authenticated-fixture", PLAN, operation)

    def window(self, weight, *, fail=False):
        start = self.now
        self.now += timedelta(seconds=10)
        document = build_window(
            [Record("source", 200, 100)] * (2000 * (100 - weight) // 100)
            + [Record("target", 500 if fail else 200, 100)] * (2000 * weight // 100),
            plan_id=PLAN, start=start.isoformat(), end=self.now.isoformat(),
            pools={"source": "source", "target": "target"})
        with self.store._transaction(write=True, plan_id=PLAN) as c:
            c.execute("INSERT INTO sli_windows VALUES (?,?,?,?)",
                      (PLAN, int(start.timestamp() * 1e6), int(self.now.timestamp() * 1e6),
                       json.dumps(document)))
        return document


@pytest.fixture
def fixture(tmp_path):
    f = Fixture(tmp_path)
    yield f
    f.provider.close()


def test_real_store_happy_path_and_restart(fixture):
    f = fixture
    assert f.start()["weight_target"] == 10
    for before, after in [(10, 50), (50, 100)]:
        f.window(before)
        f.host = f.reopen()
        receipt = f.host.advance("authenticated-fixture", PLAN, f"stage-{after}")
        assert receipt["status"] == "CONFIRMED"
        assert receipt["weight_target"] == after
    f.window(100)
    assert f.host.observe("authenticated-fixture", PLAN)["judgement"]["verdict"] == "PASS"
    final = f.host.restore("authenticated-fixture", PLAN, "restore-origin")
    assert final["readback_weights"] == {"source": 100, "target": 0}
    assert len(f.upstream.patches) == 4
    assert f.start()["status"] == "SUPERSEDED"
    assert len(f.upstream.patches) == 4
    with pytest.raises(EdgeError, match="run_closed"):
        f.start("new-key-same-run")


def test_no_direct_skip_and_no_unobserved_advance(fixture):
    f = fixture
    with pytest.raises(EdgeError, match="confirmed_current_stage"):
        f.host.advance("authenticated-fixture", PLAN, "advance-early")
    f.start()
    with pytest.raises(EdgeError, match="advance_not_allowed"):
        f.host.advance("authenticated-fixture", PLAN, "advance-no-window")
    assert len(f.upstream.patches) == 1


def test_real_stored_failure_automatically_restores_once(fixture):
    f = fixture
    f.start()
    f.window(10, fail=True)
    outcome = f.host.observe("authenticated-fixture", PLAN)
    assert outcome["judgement"]["action"] == "SET_WEIGHT_0"
    assert outcome["rollback"]["status"] == "CONFIRMED"
    assert len(f.upstream.patches) == 2
    f.host = f.reopen()
    assert f.host.observe("authenticated-fixture", PLAN)["rollback"]["status"] == "CONFIRMED"
    assert len(f.upstream.patches) == 2


def test_missing_samples_count_once_per_server_window(fixture):
    f = fixture
    f.start()
    for _ in range(4):
        result = f.host.observe("authenticated-fixture", PLAN)
        assert result["judgement"]["prior_unknown"] == 0
        assert result["rollback"] is None
    f.now += timedelta(seconds=10)
    assert f.host.observe("authenticated-fixture", PLAN)["judgement"]["prior_unknown"] == 1
    f.now += timedelta(seconds=10)
    outcome = f.host.observe("authenticated-fixture", PLAN)
    assert outcome["judgement"]["prior_unknown"] == 2
    assert outcome["rollback"]["weight_target"] == 0
    assert len(f.upstream.patches) == 2


@pytest.mark.parametrize("mutation", ["state", "approval", "expiry", "checkpoint", "lb", "principal"])
def test_ineligible_current_authority_never_reaches_provider(fixture, mutation):
    f = fixture
    principal = "authenticated-fixture"
    with f.store._transaction(write=True, plan_id=PLAN) as c:
        if mutation == "state":
            c.execute("UPDATE plans SET state='COLLECT' WHERE plan_id=?", (PLAN,))
        elif mutation == "checkpoint":
            c.execute("DELETE FROM plan_commands WHERE plan_id=?", (PLAN,))
        elif mutation in {"approval", "expiry"}:
            changed = deepcopy(f.proof)
            if mutation == "approval":
                changed["result"]["approval_id"] = "different-approval"
            else:
                changed["result"]["expires_at"] = START.isoformat()
            c.execute("UPDATE plan_commands SET receipt_json=? WHERE plan_id=?",
                      (json.dumps(changed), PLAN))
        elif mutation == "lb":
            f.context = replace(f.context, lb_binding={})
        else:
            principal = "client-asserted-identity"
    with pytest.raises(EdgeError):
        f.host.start(principal, PLAN, "start")
    assert not f.upstream.requests


def test_revocation_between_durable_intent_and_dispatch(fixture):
    f = fixture
    intent = f.host._prepare("authenticated-fixture", PLAN, "start", "start")
    f.allowed = False
    with pytest.raises(EdgeError, match="revoked"):
        f.host._dispatch("authenticated-fixture", PLAN, intent)
    assert not f.upstream.requests


def test_stale_pass_between_prepare_and_dispatch_is_rejected(fixture):
    f = fixture
    f.start()
    f.window(10)
    intent = f.host._prepare("authenticated-fixture", PLAN, "stage-50", "advance")
    f.now += timedelta(minutes=1)
    with pytest.raises(EdgeError, match="current_authority_required"):
        f.host._dispatch("authenticated-fixture", PLAN, intent)
    assert len(f.upstream.patches) == 1


def test_provider_crash_reopens_same_command_without_repatch(fixture):
    f = fixture
    f.upstream.patch_mode = "crash"
    with pytest.raises(KeyboardInterrupt):
        f.start()
    f.upstream.patch_mode = "ok"
    f.host = f.reopen()
    receipt = f.start()
    assert receipt["status"] == "UNCERTAIN"
    assert len(f.upstream.patches) == 1
    with pytest.raises(EdgeError, match="confirmed_current_stage"):
        f.host.advance("authenticated-fixture", PLAN, "stage-50")


def test_readback_retry_does_not_invalidate_stage(fixture):
    f = fixture
    f.start()
    f.now += timedelta(seconds=1)
    f.start()
    f.window(10)
    assert f.host.advance("authenticated-fixture", PLAN, "stage-50")["weight_target"] == 50
    f.now += timedelta(seconds=1)
    assert f.host.advance("authenticated-fixture", PLAN, "stage-50")["status"] == "CONFIRMED"
    assert len(f.upstream.patches) == 2


def test_changed_policy_after_start_cannot_spend_old_stage(fixture):
    f = fixture
    f.start()
    f.window(10)
    f.context = replace(f.context, revision="revoked-and-regranted")
    with pytest.raises(EdgeError, match="current_stage_binding_changed"):
        f.host.advance("authenticated-fixture", PLAN, "stage-50")
    assert len(f.upstream.patches) == 1


def test_two_prepared_starts_cannot_patch_twice(fixture):
    f = fixture
    first = f.host._prepare("authenticated-fixture", PLAN, "start-1", "start")
    second = f.host._prepare("authenticated-fixture", PLAN, "start-2", "start")
    assert f.host._dispatch("authenticated-fixture", PLAN, first)["status"] == "CONFIRMED"
    with pytest.raises(EdgeError, match="stale_revision"):
        f.host._dispatch("authenticated-fixture", PLAN, second)
    assert len(f.upstream.patches) == 1


def test_recovery_after_provider_success_before_t10_commit(fixture, monkeypatch):
    import bravo_edge_control.host as module

    f = fixture
    original = module._put

    def fail_receipt(c, plan_id, key, request, receipt):
        if key.startswith("edge-host-receipt-"):
            raise RuntimeError("simulated_plan_storage_failure")
        return original(c, plan_id, key, request, receipt)

    monkeypatch.setattr(module, "_put", fail_receipt)
    with pytest.raises(RuntimeError, match="simulated_plan_storage_failure"):
        f.start()
    assert len(f.upstream.patches) == 1
    monkeypatch.setattr(module, "_put", original)
    f.host = f.reopen()
    assert f.start()["status"] == "CONFIRMED"
    assert len(f.upstream.patches) == 1
    f.window(10)
    assert f.host.advance("authenticated-fixture", PLAN, "stage-50")["status"] == "CONFIRMED"


def test_expiry_during_provider_get_rechecked_before_patch(fixture, monkeypatch):
    f = fixture
    original = f.provider.read

    def delayed_read():
        value = original()
        f.now += timedelta(hours=2)
        return value

    monkeypatch.setattr(f.provider, "read", delayed_read)
    with pytest.raises(EdgeError, match="shift_approval_unavailable"):
        f.start()
    assert not f.upstream.patches


def test_authority_boolean_is_not_a_verified_context(fixture):
    f = fixture
    f.host.authority = lambda *_: True
    with pytest.raises(EdgeError, match="current_shift_authority_required"):
        f.start()
    assert not f.upstream.requests
