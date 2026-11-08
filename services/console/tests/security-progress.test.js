import test from "node:test";
import assert from "node:assert/strict";
import { securityProgress } from "../src/bravo_console/static/security-progress.js";
import { snap, now } from "./fixtures.js";
const model = {
  plan: snap.plan_id,
  state: "PROPOSE",
  connected: true,
  snapshotAt: now,
  cursor: 3,
  snapshot: {
    ...snap,
    state: "PROPOSE",
    profile_id: "demo-gate",
    profile_version: "1",
  },
};
const controls = {
  report: {
    ...model.snapshot,
    plan_version: 1,
    plan_state: "PROPOSE",
    gate_phase: "initial",
    application_id: null,
    profile: { id: "demo-gate", version: "1" },
    decision: "BLOCK",
    queried_at: new Date(now).toISOString(),
    read_valid_until: new Date(now + 15000).toISOString(),
    rows: [
      {
        test_id: "IAM-01.1",
        source_verdict: "PASS",
        target_verdict: "FAIL",
        decision: "BLOCK",
        reason: "TARGET_FAIL",
      },
      {
        test_id: "SEC-01.1",
        source_verdict: "UNKNOWN",
        target_verdict: "UNKNOWN",
        decision: "BLOCK",
        reason: "TARGET_UNKNOWN",
      },
    ],
  },
};
test("shows target failure and missing tests without calling them isolation or permission", () => {
  const value = securityProgress(model, controls, now);
  assert.equal(value.state, "BLOCK");
  assert.equal(value.target.FAIL, 1);
  assert.equal(value.target.UNKNOWN, 1);
  assert.equal(value.source.UNKNOWN, 1);
  assert.match(value.attention[0], /IAM-01.1/);
  assert.match(value.message, /격리를 뜻하지 않습니다/);
});
test("expires, disconnects and rejects a different plan or version immediately", () => {
  for (const [m, c, t] of [
    [model, controls, now + 15000],
    [{ ...model, connected: false }, controls, now],
    [model, { report: { ...controls.report, plan_id: "other" } }, now],
    [model, { report: { ...controls.report, plan_version: 2 } }, now],
    [{ ...model, error: "forbidden" }, controls, now],
    [model, null, now],
  ])
    assert.equal(securityProgress(m, c, t).state, "UNAVAILABLE");
});
