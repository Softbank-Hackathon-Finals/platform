import test from "node:test";
import assert from "node:assert/strict";
import {
  Model,
  event,
  failbackBinding,
  failbackRecord,
  failbackSummary,
} from "../src/bravo_console/static/model.js";
import { plan, snap, now, ev, events, receipt } from "./fixtures.js";
import { fb, record, timeline } from "./failback-fixtures.js";
for (const status of ["edge_pending", "rolled_back", "aborted"]) {
  test(`failback ${status} survives reconnect and remains read only`, () => {
    const rows = timeline(status),
      state = rows.at(-1).data.to ?? "FAILBACK";
    const m = new Model(plan);
    m.observeSnapshot(
      { ...snap, state, version: 5, last_event_id: `${plan}:${rows.length}` },
      now,
    );
    rows.forEach((x) => m.accept(x));
    m.connected = true;
    assert.equal(m.state, state);
    assert.equal(m.failback.data.status, status);
    assert.equal(m.accept(rows.at(-1)), false);
    assert.equal(Boolean(m.eligible(now)), false);
    assert.equal(m.approval, null);
    assert.throws(() => m.approvalRequest(now), /approval_not_ready/);
    assert.ok(!failbackSummary(m.failback.data).includes("undefined"));
  });
}
test("failback approval cannot preserve a previous cutover execution receipt", () => {
  const m = new Model(plan);
  m.observeSnapshot(snap, now);
  events.forEach((x) => m.accept(x));
  m.connected = true;
  m.recordApproval(receipt, m.approvalRequest(now));
  m.accept(
    ev(4, "approval.decided", {
      approval_id: receipt.approval_id,
      approval_kind: "failback",
      binding: fb,
      decision: "approved",
      actor_id: receipt.actor_id,
    }),
  );
  assert.equal(m.approval, null);
});
for (const patch of [
  { plan_id: "other" },
  { source_environment_id: "wrong" },
  { target_environment_id: "wrong" },
  { plan_digest: "b".repeat(64) },
]) {
  test(`reject other failback scope ${Object.keys(patch)[0]} without cursor advance`, () => {
    const m = new Model(plan);
    m.observeSnapshot(snap, now);
    assert.throws(() =>
      m.accept(
        ev(1, "approval.requested", {
          approval_id: "one",
          approval_kind: "failback",
          binding: { ...fb, ...patch },
        }),
      ),
    );
    assert.equal(m.cursor, 0);
    assert.equal(m.historyBytes, 0);
  });
}
for (const patch of [
  { cutover_epoch: 0 },
  { cutover_epoch: "10" },
  { cutover_epoch: 2 ** 53 },
  { source_environment_id: fb.target_environment_id },
  { extra: "x" },
  { expires_at: "never" },
]) {
  test(`reject malformed failback binding ${JSON.stringify(patch)}`, () =>
    assert.throws(() => failbackBinding({ ...fb, ...patch }, plan)));
}
for (const patch of [
  { finished_at: "2026-11-04T00:01:01Z" },
  { recovery_ms: 0 },
  { status: "rolled_back" },
  { execution_id: null },
  { integrity_receipt_id: null },
  { status: "anything" },
  { extra: "x" },
]) {
  test(`reject incomplete failback evidence ${JSON.stringify(patch)}`, () =>
    assert.throws(() => failbackRecord({ ...record, ...patch })));
}
test("negative recovery, reversed time and run mismatch cannot show success", () => {
  const done = {
    ...record,
    status: "rolled_back",
    finished_at: "2026-11-04T00:01:09Z",
    recovery_ms: 9000,
  };
  for (const patch of [
    { recovery_ms: -1 },
    { recovery_ms: "9000" },
    { finished_at: "2026-11-04T00:00:59Z" },
  ])
    assert.throws(() => failbackRecord({ ...done, ...patch }));
  assert.throws(
    () => event(ev(1, "failback.recorded", done), plan),
    /failback_run_mismatch/,
  );
});
