import test from "node:test";
import assert from "node:assert/strict";
import { Model, changeBinding } from "../src/bravo_console/static/model.js";
import { plan, snap, now, ev, bind, receipt } from "./fixtures.js";
import { canRepeatApproval } from "../src/bravo_console/static/recovery.js";

const { gate_id, context_digest, ...candidate } = bind;
function setup(binding = candidate) {
  const m = new Model(plan);
  m.observeSnapshot(
    { ...snap, state: "REHEARSE", last_event_id: `${plan}:2` },
    now,
  );
  m.accept(ev(1, "state.changed", { from: null, to: "REHEARSE" }));
  m.accept({
    ...ev(2, "approval.requested", {
      approval_id: "change-request",
      approval_kind: "change",
      binding,
    }),
    run_id: "rehearsal-one",
  });
  m.connected = true;
  return m;
}

for (const fault of [
  "none",
  "version",
  "run",
  "new-request",
  "expired",
  "offline",
  "gap",
  "body",
  "profile-id",
  "profile-version",
]) {
  test(`approval repeat requires explicit frozen current context: ${fault}`, () => {
    const m = setup(),
      pending = {
        kind: "approve-change",
        plan,
        body: m.changeApprovalRequest(now),
        snapshot: structuredClone(m.snapshot),
        cursor: m.cursor,
        requestEventId: m.request.event_id,
      };
    let at = now;
    if (fault === "version") m.snapshot.version++;
    if (fault === "run") m.request.run_id = "other-run";
    if (fault === "new-request") m.request.event_id = `${plan}:9`;
    if (fault === "expired") at += 600000;
    if (fault === "offline") m.connected = false;
    if (fault === "gap") m.snapshot.last_event_id = `${plan}:3`;
    if (fault === "body") pending.body.expected_version++;
    if (fault === "profile-id") pending.snapshot.profile_id = "other";
    if (fault === "profile-version") pending.snapshot.profile_version = "2.0";
    assert.equal(canRepeatApproval(m, pending, at), fault === "none");
  });
}

for (const binding of [candidate, bind]) {
  test(`change approval preserves exact run and optional gate: ${"gate_id" in binding}`, () => {
    const m = setup(binding);
    assert.equal(m.changeEligible(now), true);
    assert.equal(Boolean(m.eligible(now)), false);
    const sent = m.changeApprovalRequest(now);
    assert.deepEqual(sent, {
      approval_kind: "change",
      run_id: "rehearsal-one",
      binding,
      expected_version: snap.version,
    });
    const saved = {
      ...receipt,
      approval_kind: "change",
      run_id: sent.run_id,
      binding,
    };
    assert.deepEqual(m.recordChangeApproval(saved, sent), saved);
    assert.equal(m.approval, null);
    assert.throws(() => m.recordApproval(saved, sent));
    m.accept({
      ...ev(3, "approval.decided", {
        approval_id: saved.approval_id,
        approval_kind: "change",
        binding,
        decision: "approved",
        actor_id: saved.actor_id,
      }),
      run_id: sent.run_id,
    });
    assert.equal(m.changeEligible(now), false);
    assert.equal(m.approval, null);
  });
}
for (const fault of [
  "offline",
  "error",
  "stale",
  "future",
  "state",
  "snapshot-state",
  "gap",
  "plan",
  "target",
  "digest",
  "expired",
  "cutover",
  "failback",
]) {
  test(`change approval remains locked: ${fault}`, () => {
    const m = setup();
    let at = now;
    if (fault === "offline") m.connected = false;
    if (fault === "error") m.error = "forbidden";
    if (fault === "stale") at += 15000;
    if (fault === "future") at -= 1;
    if (fault === "state") m.state = "SHIFT";
    if (fault === "snapshot-state") m.snapshot.state = "SHIFT";
    if (fault === "gap") m.snapshot.last_event_id = `${plan}:3`;
    if (fault === "plan") m.request.data.binding.plan_id = "another";
    if (fault === "target")
      m.request.data.binding.target_environment_id = "another";
    if (fault === "digest") m.request.data.binding.plan_digest = "f".repeat(64);
    if (fault === "expired") at += 600000;
    if (fault === "cutover" || fault === "failback")
      m.request.data.approval_kind = fault;
    assert.equal(m.changeEligible(at), false);
    assert.throws(
      () => m.changeApprovalRequest(at),
      /change_approval_not_ready/,
    );
  });
}
for (const patch of [
  { image_digest: "latest" },
  { diff_hash: "bad" },
  { expires_at: "never" },
  { plan_id: "../wrong" },
  { gate_id: "only-one" },
  { context_digest: "e".repeat(64) },
  { gate_id: null, context_digest: null },
  { extra: "x" },
]) {
  test(`malformed change binding is rejected: ${JSON.stringify(patch)}`, () =>
    assert.throws(() => changeBinding({ ...candidate, ...patch })));
}
for (const patch of [
  { approval_kind: "cutover" },
  { run_id: "other-run" },
  { actor_id: "" },
  { approval_id: "" },
  { approved_at: "never" },
  { binding: { ...candidate, image_digest: "sha256:" + "f".repeat(64) } },
  { extra: true },
]) {
  test(`mismatched change receipt cannot authorize execution: ${JSON.stringify(patch)}`, () => {
    const m = setup(),
      sent = m.changeApprovalRequest(now);
    assert.throws(() =>
      m.recordChangeApproval(
        {
          ...receipt,
          approval_kind: "change",
          run_id: sent.run_id,
          binding: candidate,
          ...patch,
        },
        sent,
      ),
    );
    assert.equal(m.approval, null);
  });
}
