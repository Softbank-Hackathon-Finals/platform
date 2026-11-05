import test from "node:test";
import assert from "node:assert/strict";
import {
  Model,
  binding,
  event,
  snapshot,
} from "../src/bravo_console/static/model.js";
import { now, plan, bind, snap, events, ev, receipt } from "./fixtures.js";
function ready() {
  const m = new Model(plan);
  m.observeSnapshot(snap, now);
  events.forEach((e) => m.accept(e));
  m.connected = true;
  return m;
}
test("snapshot accepts complete profile or legacy absence and rejects partial or invalid pairs", () => {
  const profiled = {
    ...snap,
    profile_id: "fixture-profile",
    profile_version: "1.0",
  };
  assert.deepEqual(snapshot(snap, plan), snap);
  assert.deepEqual(snapshot(profiled, plan), profiled);
  for (const key of ["profile_id", "profile_version"]) {
    const partial = { ...profiled };
    delete partial[key];
    assert.throws(() => snapshot(partial, plan));
    for (const value of [null, 1, "", "../profile", "x".repeat(129)])
      assert.throws(() => snapshot({ ...profiled, [key]: value }, plan));
  }
  assert.throws(() => snapshot({ ...profiled, unknown: true }, plan));
});

test("snapshot profile identity cannot change while rereading a plan", () => {
  const profiled = {
    ...snap,
    profile_id: "fixture-profile",
    profile_version: "1.0",
  };
  for (const value of [
    { ...profiled, profile_id: "other" },
    { ...profiled, profile_version: "2.0" },
    snap,
  ]) {
    const m = new Model(plan);
    m.observeSnapshot(profiled, now);
    assert.throws(() => m.observeSnapshot(value, now), /plan_changed/);
  }
  const legacy = new Model(plan);
  legacy.observeSnapshot(snap, now);
  assert.throws(() => legacy.observeSnapshot(profiled, now), /plan_changed/);
});
test("matching snapshot/history/gate permits only a bound approval request", () => {
  const m = ready();
  assert.equal(m.eligible(now), true);
  assert.deepEqual(m.approvalRequest(now), {
    approval_kind: "cutover",
    binding: bind,
    expected_version: 1,
  });
  assert.equal(m.approval, null);
});
for (const fault of [
  "offline",
  "expired",
  "stale",
  "future-clock",
  "wrong-gate",
  "wrong-plan",
  "wrong-target",
  "missing-gate",
  "missing-request",
  "wrong-state",
  "new-version",
  "history-ahead",
])
  test(`approval locked: ${fault}`, () => {
    const m = ready();
    let at = now;
    if (fault === "offline") m.connected = false;
    if (fault === "expired") at = Date.parse(bind.expires_at);
    if (fault === "stale") at += 15000;
    if (fault === "future-clock") at--;
    if (fault === "wrong-gate") m.request.data.binding.gate_id = "other";
    if (fault === "wrong-plan")
      m.request.data.binding.plan_digest = "f".repeat(64);
    if (fault === "wrong-target")
      m.request.data.binding.target_environment_id = "other";
    if (fault === "missing-gate") m.gate = null;
    if (fault === "missing-request") m.request = null;
    if (fault === "wrong-state") m.state = "SAFE_HOLD";
    if (fault === "new-version") m.error = "version_conflict";
    if (fault === "history-ahead") m.snapshot.last_event_id = plan + ":4";
    assert.equal(Boolean(m.eligible(at)), false);
    assert.throws(() => m.approvalRequest(at));
  });
test("replay duplicates do not mutate cursor and changed/gapped replay fails", () => {
  const m = ready();
  assert.equal(m.accept(events[2]), false);
  assert.throws(
    () => m.accept(ev(5, "state.changed", { from: "SHIFT", to: "CUTOVER" })),
    /event_gap/,
  );
  assert.throws(
    () => m.accept({ ...events[2], run_id: "other" }),
    /replay_changed/,
  );
  assert.equal(m.cursor, 3);
});
test("unknown events, state mismatch and cross-plan frame fail closed", () => {
  const m = ready();
  assert.throws(() => m.accept(ev(4, "shell.exec", {})), /unknown_event/);
  assert.throws(
    () => m.accept(ev(4, "state.changed", { from: "GATE", to: "DONE" })),
    /state_history_mismatch/,
  );
  assert.throws(
    () =>
      m.accept({
        ...ev(4, "state.changed", { from: "SHIFT", to: "CUTOVER" }),
        plan_id: "another",
      }),
    /invalid_event/,
  );
});
test("approval event alone does not enable execution; server receipt survives its matching SSE", () => {
  const m = ready();
  const body = m.approvalRequest(now);
  m.recordApproval(receipt, body);
  m.accept(
    ev(4, "approval.decided", {
      approval_id: receipt.approval_id,
      approval_kind: "cutover",
      binding: bind,
      actor_id: receipt.actor_id,
      decision: "approved",
    }),
  );
  assert.equal(m.approval.approval_id, receipt.approval_id);
  const other = ready();
  other.accept(
    ev(4, "approval.decided", {
      approval_id: receipt.approval_id,
      approval_kind: "cutover",
      binding: bind,
      actor_id: receipt.actor_id,
      decision: "approved",
    }),
  );
  assert.equal(other.approval, null);
});
test("new gate and snapshot version invalidate saved approval", () => {
  const m = ready();
  m.recordApproval(receipt, m.approvalRequest(now));
  m.observeSnapshot({ ...snap, version: 2 }, now);
  assert.equal(m.approval, null);
  m.accept(ev(4, "gate.completed", { ...events[1].data, decision: "BLOCK" }));
  assert.equal(m.request, null);
  assert.equal(Boolean(m.eligible(now)), false);
});
test("server receipt binding comparison is independent of JSON key order", () => {
  const m = ready();
  const reversed = Object.fromEntries(Object.entries(bind).reverse());
  m.recordApproval({ ...receipt, binding: reversed }, m.approvalRequest(now));
  assert.equal(m.approval.approval_id, receipt.approval_id);
});
test("malformed binding and snapshot do not coerce digest or version types", () => {
  assert.throws(() => binding({ ...bind, plan_digest: [bind.plan_digest] }));
  assert.throws(() => snapshot({ ...snap, version: "1" }, plan));
  assert.throws(() => binding({ ...bind, untrusted: "x" }));
  assert.throws(() =>
    event(
      ev(4, "execution.updated", {
        execution_id: "e",
        step: "x",
        status: "succeeded",
        elapsed_ms: NaN,
      }),
      plan,
    ),
  );
});

test("bounded replay refuses large histories before accepting another record", () => {
  const m = ready();
  m.historyBytes = 8 * 1024 * 1024;
  assert.throws(
    () => m.accept(ev(4, "state.changed", { from: "SHIFT", to: "CUTOVER" })),
    /history_limit/,
  );
  assert.equal(m.state, "SHIFT");
  assert.equal(m.cursor, 3);
});
