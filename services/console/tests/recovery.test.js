import test from "node:test";
import assert from "node:assert/strict";
import { Model } from "../src/bravo_console/static/model.js";
import {
  canResubmit,
  recoveryCommand,
} from "../src/bravo_console/static/recovery.js";
import { now, plan, snap, events, ev, receipt } from "./fixtures.js";

function setup() {
  const m = new Model(plan);
  m.observeSnapshot(snap, now);
  events.forEach((e) => m.accept(e));
  const { approved_at: _at, ...decision } = receipt;
  m.accept(ev(4, "approval.decided", { ...decision, decision: "approved" }));
  m.connected = true;
  const pending = {
    kind: "execute",
    plan,
    snapshot: structuredClone(snap),
    approval: structuredClone(receipt),
    cursor: 4,
    body: { approval_id: receipt.approval_id },
  };
  return { m, pending };
}

test("only fresh matching complete replay permits explicit resubmission", () => {
  const { m, pending } = setup();
  assert.equal(canResubmit(m, pending, now), true);
  assert.equal(recoveryCommand(m, pending), null);
});
for (const fault of [
  "offline",
  "gap",
  "stale",
  "expired",
  "version",
  "other-plan",
  "gate",
  "approval",
  "revoked",
  "missing-decision",
  "actor",
  "history-regressed",
  "profile-id",
  "profile-version",
]) {
  test(`resubmission stays locked: ${fault}`, () => {
    const { m, pending } = setup();
    let at = now;
    if (fault === "offline") m.connected = false;
    if (fault === "gap") m.error = "event_gap";
    if (fault === "stale") at += 15000;
    if (fault === "expired") at += 600000;
    if (fault === "version") m.snapshot.version++;
    if (fault === "other-plan") pending.plan = "another";
    if (fault === "gate")
      pending.approval.binding.context_digest = "f".repeat(64);
    if (fault === "approval") pending.body.approval_id = "another";
    if (fault === "revoked") m.events.at(-1).data.decision = "expired";
    if (fault === "missing-decision") m.events.pop();
    if (fault === "history-regressed") pending.cursor = m.cursor + 1;
    if (fault === "actor") m.events.at(-1).data.actor_id = "other-operator";
    if (fault === "profile-id") pending.snapshot.profile_id = "other";
    if (fault === "profile-version") pending.snapshot.profile_version = "2.0";
    assert.equal(Boolean(canResubmit(m, pending, at)), false);
  });
}
test("matching profile pair permits recovery but changing or removing either field denies it", () => {
  const { m, pending } = setup();
  const profile = { profile_id: "fixture-profile", profile_version: "1.0" };
  Object.assign(m.snapshot, profile);
  Object.assign(pending.snapshot, profile);
  assert.equal(canResubmit(m, pending, now), true);
  for (const key of Object.keys(profile)) {
    pending.snapshot[key] = "other";
    assert.equal(canResubmit(m, pending, now), false);
    delete pending.snapshot[key];
    assert.equal(canResubmit(m, pending, now), false);
    pending.snapshot[key] = profile[key];
  }
});
test("a committed dispatch event permits lookup, never new dispatch", () => {
  const { m, pending } = setup();
  m.accept({
    ...ev(5, "state.changed", { from: "SHIFT", to: "CUTOVER" }),
    run_id: "command-one",
  });
  assert.equal(recoveryCommand(m, pending), "command-one");
  assert.equal(canResubmit(m, pending, now), false);
  m.snapshot.last_event_id = `${plan}:6`;
  assert.equal(recoveryCommand(m, pending), null);
});
