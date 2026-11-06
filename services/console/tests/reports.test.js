import test from "node:test";
import assert from "node:assert/strict";
import {
  controlReport,
  controlReportMatches,
} from "../src/bravo_console/static/model.js";
import { snap as base, now } from "./fixtures.js";
const snap = { ...base, profile_id: "demo-gate", profile_version: "1" };
const gate = { data: { gate_id: "gate-one", context_digest: "e".repeat(64) } };
function report() {
  return {
    kind: "console-control-report.v1",
    gate_phase: "initial",
    application_id: null,
    plan_id: snap.plan_id,
    plan_digest: snap.plan_digest,
    source_environment_id: snap.source_environment_id,
    target_environment_id: snap.target_environment_id,
    gate_id: gate.data.gate_id,
    context_digest: gate.data.context_digest,
    report_digest: "a".repeat(64),
    profile: { id: "demo-gate", version: "1" },
    decision: "PASS",
    evaluated_at: new Date(now - 1000).toISOString(),
    queried_at: new Date(now).toISOString(),
    authorizes_cutover: false,
    run_issues: [],
    rows: ["IAM-01.1", "SEC-01.1", "AUD-01.1"].map((test_id) => ({
      test_id,
      control_id: test_id.split(".")[0],
      source_verdict: "PASS",
      target_verdict: "PASS",
      decision: "PASS",
      reason: "BOTH_PASS",
      improved: false,
      exception_applied: false,
    })),
  };
}
test("bound security report remains a historical projection, not permission", () => {
  const result = controlReport(report(), snap, gate, now);
  assert.equal(result.authorizes_cutover, false);
  assert.equal(result.rows.length, 3);
});
for (const current of [
  { ...snap, profile_id: "full" },
  { ...snap, profile_version: "2" },
  base,
]) {
  test(`report cannot replace registered plan profile: ${current.profile_id}/${current.profile_version}`, () => {
    assert.throws(() => controlReport(report(), current, gate, now));
  });
}
for (const fault of [
  "plan",
  "digest",
  "gate",
  "context",
  "target",
  "stale",
  "future",
  "duplicate",
  "missing",
  "profile",
  "authority",
  "reason",
  "verdict",
])
  test(`invalid control report hidden: ${fault}`, () => {
    const value = report();
    if (fault === "plan") value.plan_id = "other";
    if (fault === "digest") value.plan_digest = "f".repeat(64);
    if (fault === "gate") value.gate_id = "other";
    if (fault === "context") value.context_digest = "f".repeat(64);
    if (fault === "target") value.target_environment_id = "other";
    if (fault === "stale")
      value.queried_at = new Date(now - 15001).toISOString();
    if (fault === "future")
      value.evaluated_at = new Date(now + 1).toISOString();
    if (fault === "duplicate") value.rows[1] = value.rows[0];
    if (fault === "missing") value.rows.pop();
    if (fault === "profile") value.profile.version = "2";
    if (fault === "authority") value.authorizes_cutover = true;
    if (fault === "reason") value.rows[0].reason = "arbitrary private text";
    if (fault === "verdict") value.rows[0].target_verdict = "probably OK";
    assert.throws(() => controlReport(value, snap, gate, now));
  });

test("post-apply report uses the registered recheck profile and exact required tests", () => {
  const current = {
    ...snap,
    recheck_profile_id: "apply-recheck",
    recheck_profile_version: "1",
  };
  const value = report();
  Object.assign(value, {
    gate_phase: "post_apply",
    application_id: "applied-one",
    profile: { id: "apply-recheck", version: "1" },
  });
  Object.assign(value.rows[2], { test_id: "ENC-01.1", control_id: "ENC-01" });
  assert.deepEqual(controlReport(value, current, gate, now), value);
  for (const change of [
    { gate_phase: "initial" },
    { gate_phase: undefined },
    { application_id: null },
    { application_id: "../invalid" },
    { profile: { id: "demo-gate", version: "1" } },
    { rows: report().rows },
  ])
    assert.throws(() =>
      controlReport({ ...value, ...change }, current, gate, now),
    );
  assert.throws(() => controlReport(value, snap, gate, now));
});

test("authenticated initial comparison is visible before a workflow gate event", () => {
  const current = { ...snap, state: "PROPOSE", version: 3 };
  const value = {
    ...report(),
    plan_version: current.version,
    plan_state: current.state,
    decision: "BLOCK",
  };
  Object.assign(value.rows[0], {
    target_verdict: "UNKNOWN",
    decision: "BLOCK",
    reason: "TARGET_UNKNOWN",
  });
  const visible = controlReport(value, current, null, now);
  assert.equal(visible.decision, "BLOCK");
  assert.equal(visible.authorizes_cutover, false);
  assert.equal(visible.rows[0].target_verdict, "UNKNOWN");
});

for (const change of [
  { plan_version: undefined },
  { plan_state: undefined },
  { plan_version: true },
  { plan_version: 0 },
  { plan_version: -1 },
  { plan_version: "3" },
  { plan_version: 2 },
  { plan_version: 4 },
  { plan_state: "GATE" },
  { plan_state: "UNKNOWN" },
  { gate_phase: "post_apply", application_id: "applied-one" },
  { application_id: "applied-one" },
  { gate_id: "../gate" },
  { context_digest: "not-a-digest" },
  { authorizes_cutover: true },
])
  test(`initial comparison rejects invalid snapshot or phase: ${JSON.stringify(change)}`, () => {
    const current = { ...snap, state: "PROPOSE", version: 3 };
    const value = { ...report(), plan_version: 3, plan_state: "PROPOSE", ...change };
    assert.throws(() => controlReport(value, current, null, now));
  });

test("an event-bound report cannot ignore its supplied snapshot revision", () => {
  const value = { ...report(), plan_version: snap.version, plan_state: snap.state };
  assert.deepEqual(controlReport(value, snap, gate, now), value);
  assert.throws(() => controlReport({ ...value, plan_version: 2 }, snap, gate, now));
  assert.throws(() => controlReport({ ...value, plan_state: "PROPOSE" }, snap, gate, now));
});

test("initial read model never replaces a conflicting event or a later workflow state", () => {
  const current = { ...snap, state: "PROPOSE", version: 3 };
  const value = { ...report(), plan_version: 3, plan_state: "PROPOSE" };
  assert.throws(() => controlReport(value, current, { data: { ...gate.data, gate_id: "other" } }, now));
  for (const state of ["AWAIT_APPROVAL", "SHIFT", "CUTOVER", "WATCH", "FAILBACK", "SAFE_HOLD", "NEEDS_HUMAN", "DONE", "DONE_ROLLED_BACK"])
    assert.throws(() => controlReport({ ...value, plan_state: state }, { ...current, state }, null, now));
  assert.throws(() => controlReport(report(), current, null, now));
});

test("cached initial comparison disappears when current scope changes", () => {
  const current = { ...snap, state: "PROPOSE", version: 3 };
  const value = { ...report(), plan_version: 3, plan_state: "PROPOSE" };
  assert.equal(controlReportMatches(value, current, null), true);
  for (const change of [
    { plan_id: "other-plan" }, { plan_digest: "f".repeat(64) },
    { source_environment_id: "other-source" }, { target_environment_id: "other-target" },
    { profile_id: "full" }, { profile_version: "2" }, { version: 4 }, { state: "REHEARSE" },
  ])
    assert.equal(controlReportMatches(value, { ...current, ...change }, null), false);
});

test("a newly registered version-one plan can display an initial comparison", () => {
  const current = { ...snap, state: "COLLECT", version: 1 };
  const value = { ...report(), plan_version: 1, plan_state: "COLLECT" };
  assert.deepEqual(controlReport(value, current, null, now), value);
});
