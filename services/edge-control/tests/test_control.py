import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from bravo_observability.sli import Record, build_window, judge

from bravo_edge_control.control import (
    Command,
    Controller,
    FailbackEdge,
    apply_sli,
    failback_operation_id,
    sli_rollback,
)
from bravo_edge_control.provider import Binding, Cloudflare, EdgeError

BINDING = Binding("a" * 32, "b" * 32, "edge.example.test", "c" * 32, "d" * 32)


class Provider:
    def __init__(self):
        self.result = {
            "id": BINDING.lb_id, "name": BINDING.hostname, "enabled": True, "proxied": True,
            "steering_policy": "random", "session_affinity": "none",
            "default_pools": [BINDING.source_pool, BINDING.target_pool],
            "fallback_pool": BINDING.source_pool,
            "random_steering": {"default_weight": 0,
                                "pool_weights": {BINDING.source_pool: 1, BINDING.target_pool: 0}},
        }
        self.requests = []
        self.patch_mode = "ok"
        self.read_mode = "ok"
        self.after_patch_read_mode = "ok"

    def __call__(self, request):
        self.requests.append(request)
        assert str(request.url).startswith("https://api.cloudflare.com/client/v4/zones/")
        if request.method == "PATCH":
            if self.patch_mode == "crash":
                raise KeyboardInterrupt
            if self.patch_mode == "timeout_before":
                raise httpx.ReadTimeout("sensitive-provider-body", request=request)
            self.result.update(json.loads(request.content))
            self.read_mode = self.after_patch_read_mode
            if self.patch_mode == "timeout_after":
                raise httpx.ReadTimeout("sensitive-provider-body", request=request)
            if self.patch_mode == "bad_ack":
                return httpx.Response(200, json={"success": False, "errors": ["sensitive"]})
        if request.method == "GET" and self.read_mode == "error":
            return httpx.Response(503, text="sensitive-provider-body")
        return httpx.Response(200, json={"success": True, "result": deepcopy(self.result)})

    @property
    def patches(self):
        return [r for r in self.requests if r.method == "PATCH"]


@pytest.fixture
def setup(tmp_path):
    upstream = Provider()
    client = Cloudflare(BINDING, lambda: "test-token", transport=httpx.MockTransport(upstream))
    control = Controller(client, tmp_path / "journal.db", authorize=lambda _: True)
    yield upstream, client, control
    client.close()


def command(weight=10, revision=0, previous=0, operation="step-1", **kwargs):
    return Command("plan-test", "run-test", operation, revision, previous, weight, **kwargs)


def test_happy_path_receipts_and_superseded_retry(setup):
    upstream, _, control = setup
    for revision, (before, after) in enumerate(((0, 10), (10, 50), (50, 100), (100, 0))):
        receipt = control.execute(command(after, revision, before, f"step-{revision + 1}"))
        assert receipt["status"] == "CONFIRMED"
        assert receipt["readback_weights"] == {"source": 100 - after, "target": after}
        assert receipt["request_at"] <= receipt["response_at"] <= receipt["confirmed_at"]
        assert receipt["lb_ref"] == BINDING.lb_id
    assert len(upstream.patches) == 4
    assert control.execute(command())["status"] == "SUPERSEDED"
    assert len(upstream.patches) == 4
    with pytest.raises(EdgeError, match="run_closed"):
        control.execute(command(10, 4, 0, "late-start"))
    assert len(upstream.patches) == 4


def test_duplicate_concurrent_calls_and_restart_send_once(setup):
    upstream, client, control = setup
    other = Controller(client, control.path, authorize=lambda _: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.execute(command()), [control, other]))
    assert all(result["status"] == "CONFIRMED" for result in results)
    restarted = Controller(client, control.path, authorize=lambda _: True)
    assert restarted.execute(command())["status"] == "CONFIRMED"
    assert len(upstream.patches) == 1


@pytest.mark.parametrize("mode", ["timeout_before", "timeout_after", "bad_ack", "crash"])
def test_ambiguous_write_never_resends_or_advances_even_when_get_matches(setup, mode):
    upstream, client, control = setup
    upstream.patch_mode = mode
    if mode == "crash":
        with pytest.raises(KeyboardInterrupt):
            control.execute(command())
    else:
        assert control.execute(command())["status"] == "UNCERTAIN"
    restarted = Controller(client, control.path, authorize=lambda _: True)
    for _ in range(2):
        receipt = restarted.execute(command())
        assert receipt["status"] == "UNCERTAIN"
        assert receipt["confirmed_at"] is None
        assert "sensitive" not in json.dumps(receipt)
    with pytest.raises(EdgeError, match="pending_operation"):
        restarted.execute(command(50, 1, 10, "step-2"))
    assert len(upstream.patches) == 1


def test_acknowledged_change_recovers_readback_without_second_patch(setup):
    upstream, _, control = setup
    upstream.after_patch_read_mode = "error"
    assert control.execute(command())["status"] == "AWAITING_READBACK"
    upstream.read_mode = "ok"
    assert control.execute(command())["status"] == "CONFIRMED"
    assert len(upstream.patches) == 1


def test_drift_after_confirmation_does_not_report_old_receipt_as_current(setup):
    upstream, _, control = setup
    assert control.execute(command())["status"] == "CONFIRMED"
    upstream.result["random_steering"]["pool_weights"] = {
        BINDING.source_pool: 1, BINDING.target_pool: 0
    }
    assert control.execute(command())["status"] == "AWAITING_READBACK"
    assert len(upstream.patches) == 1


def test_conflicting_key_and_stale_revision_never_patch(setup):
    upstream, _, control = setup
    control.execute(command())
    with pytest.raises(EdgeError, match="idempotency_conflict"):
        control.execute(command(0))
    with pytest.raises(EdgeError, match="stale_revision"):
        control.execute(command(10, 0, 0, "different-key"))
    assert len(upstream.patches) == 1


def test_missing_authority_and_wrong_current_weight_never_patch(setup):
    upstream, _, control = setup
    control.authorize = lambda _: False
    with pytest.raises(EdgeError, match="authority"):
        control.execute(command())
    assert not upstream.requests
    control.authorize = lambda _: True
    with pytest.raises(EdgeError, match="current_weight_mismatch"):
        control.execute(command(50, 0, 10))
    assert not upstream.patches


def test_journal_cannot_be_reused_for_another_binding(setup):
    _, _, control = setup
    client = Cloudflare(replace(BINDING, lb_id="e" * 32), lambda: "test-token")
    try:
        with pytest.raises(EdgeError, match="journal_binding_mismatch"):
            Controller(client, control.path, authorize=lambda _: True)
    finally:
        client.close()


@pytest.mark.parametrize("field,value", [
    ("id", "e" * 32), ("name", "wrong.example.test"), ("enabled", False),
    ("enabled", 1), ("proxied", False), ("default_pools", [BINDING.source_pool]),
    ("fallback_pool", BINDING.target_pool), ("session_affinity", "cookie"),
    ("steering_policy", "dynamic_latency"), ("rules", [{"disabled": False}]),
    ("country_pools", {"US": [BINDING.target_pool]}),
    ("random_steering", {"default_weight": 1, "pool_weights": {}}),
])
def test_identity_or_steering_drift_prevents_mutation(setup, field, value):
    upstream, _, control = setup
    upstream.result[field] = value
    with pytest.raises(EdgeError):
        control.execute(command())
    assert not upstream.patches


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, "1",
                                   pytest.param(10 ** 1000, id="oversized-integer")])
def test_ambiguous_weight_types_rejected(value):
    result = Provider().result
    result["random_steering"]["pool_weights"][BINDING.source_pool] = value
    with pytest.raises(EdgeError):
        BINDING.weight(result)


def pool_sets():
    return [{"name": "all-target", "match": {"default": True}, "overrides": {
        "pools": [BINDING.target_pool], "fallback_pool": BINDING.target_pool,
        "steering_policy": "off",
    }}]


@pytest.mark.parametrize("phase", ["before_patch", "ack", "readback"])
def test_pool_sets_cannot_report_false_weight_confirmation(tmp_path, phase):
    upstream = Provider()
    if phase == "before_patch":
        upstream.result["pool_sets"] = pool_sets()

    def respond(request):
        response = upstream(request)
        inject = ((phase == "ack" and request.method == "PATCH") or
                  (phase == "readback" and request.method == "GET" and upstream.patches))
        if inject:
            body = response.json()
            body["result"]["pool_sets"] = pool_sets()
            return httpx.Response(200, json=body)
        return response

    provider = Cloudflare(BINDING, lambda: "test-token", transport=httpx.MockTransport(respond))
    try:
        control = Controller(provider, tmp_path / "journal.db", authorize=lambda _: True)
        if phase == "before_patch":
            with pytest.raises(EdgeError, match="steering_override_present"):
                control.execute(command())
            assert not upstream.patches
        else:
            result = control.execute(command())
            assert result["status"] == ("UNCERTAIN" if phase == "ack" else "AWAITING_READBACK")
            assert result["confirmed_at"] is None
            assert result["error"] == "steering_override_present"
            assert len(upstream.patches) == 1
    finally:
        provider.close()


def test_pool_set_added_before_failback_blocks_false_zero_confirmation(setup):
    upstream, _, control = setup
    control.execute(command())
    upstream.result["pool_sets"] = pool_sets()
    with pytest.raises(EdgeError, match="steering_override_present"):
        control.execute(command(0, 1, 10, "restore", reason="FAILBACK"))
    assert len(upstream.patches) == 1


@pytest.mark.parametrize("invalid", [False, 0, "", "active"])
def test_malformed_override_field_is_not_absence(setup, invalid):
    upstream, _, control = setup
    upstream.result["pool_sets"] = invalid
    with pytest.raises(EdgeError, match="steering_override_present"):
        control.execute(command())
    assert not upstream.patches


@pytest.mark.parametrize("status", [301, 307, 401, 429, 500])
def test_http_errors_redacted_no_redirect_or_retry(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example.test"},
                              text="private response and credential")

    client = Cloudflare(BINDING, lambda: "test-token", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(EdgeError, match="^provider_http_error$"):
            client.read()
        assert len(requests) == 1
    finally:
        client.close()


def judgement(*, state="SHIFT", unknown=False, prior=0, fail=False):
    # Synthetic measurements use the unchanged canonical proposed profile.
    profiles = Path(__file__).resolve().parents[3] / "docs/observability/threshold-profiles.json"
    profile = next(p for p in json.loads(profiles.read_text())["profiles"]
                   if p["id"] == "demo-shift" and p["version"] == "1")
    start = datetime(2026, 11, 5, tzinfo=UTC)
    end = start + timedelta(seconds=10)
    window = build_window(
        [Record("source", 200, 100)] * 900 +
        [Record("target", 500 if fail else 200, 100)] * 100,
        plan_id="plan-test", start=start.isoformat(), end=end.isoformat(),
        pools={"source": "source", "target": "target"},
    )
    return judge(profile, None if unknown else window, plan_id="plan-test", state=state,
                 weight_target=10, observe_from=start.isoformat(), decided_at=end.isoformat(),
                 prior_unknown=prior)


@pytest.mark.parametrize("values", [{"fail": True}, {"unknown": True, "prior": 2}])
def test_canonical_fail_and_unknown_limit_restore_zero_once(setup, values):
    upstream, _, control = setup
    control.execute(command())
    decision = judgement(**values)
    current = dict(plan_id="plan-test", run_id="run-test", revision=1, weight=10,
                   observe_from=decision["observe_from"])
    assert apply_sli(control, decision, **current)["status"] == "CONFIRMED"
    assert apply_sli(control, decision, **current)["status"] == "CONFIRMED"
    assert len(upstream.patches) == 2
    assert BINDING.weight(upstream.result) == 0


@pytest.mark.parametrize("values", [{}, {"unknown": True}, {"state": "WATCH", "fail": True}])
def test_pass_hold_watch_make_no_provider_calls(setup, values):
    upstream, _, control = setup
    decision = judgement(**values)
    assert apply_sli(control, decision, plan_id="plan-test", run_id="run-test",
                     revision=1, weight=10, observe_from=decision["observe_from"]) is None
    assert not upstream.requests


def test_stale_sli_cannot_rollback_another_stage():
    decision = judgement(fail=True)
    with pytest.raises(EdgeError, match="stale_judgement"):
        sli_rollback(decision, plan_id="plan-test", run_id="run-test", revision=2,
                     weight=50, observe_from=decision["observe_from"])


def test_failback_contract_binds_all_identity_fields_and_requeries(setup):
    upstream, _, control = setup
    control.execute(command())
    args = dict(plan_id="plan-test", command_id="cmd-test", execution_id="exec-test", epoch=1)
    restore = command(0, 1, 10, failback_operation_id(**args), reason="FAILBACK")
    edge = FailbackEdge(control, resolve=lambda **_: restore)
    assert edge.restore_origin(**args)
    assert edge.restore_origin(**args)
    with pytest.raises(EdgeError, match="failback_binding_mismatch"):
        edge.restore_origin(**{**args, "epoch": 2})
    assert len(upstream.patches) == 2
