import test from "node:test";
import assert from "node:assert/strict";
import {
  securitySummary,
  SECURITY_CATEGORIES,
} from "../src/bravo_console/static/model.js";
const now = Date.now();
const plan = {
  plan_id: "plan-demo",
  plan_digest: "a".repeat(64),
  source_environment_id: "env-source",
  target_environment_id: "env-target",
};
function sample() {
  return {
    kind: "console-security.v1",
    plan_id: plan.plan_id,
    plan_digest: plan.plan_digest,
    queried_at: new Date(now).toISOString(),
    for_gate_decisions: false,
    environments: ["source", "target"].map((side) => ({
      side,
      environment_id: `env-${side}`,
      cells: Object.fromEntries(
        Object.keys(SECURITY_CATEGORIES).map((k) => [
          k,
          { state: "OK", counts: { 4: 2, 99: 1 } },
        ]),
      ),
    })),
  };
}
test("security counts are display only, unknown severity remains counted", () => {
  const rows = securitySummary(sample(), plan, now);
  assert.equal(rows[0].cells.IAM.total, 3);
});
for (const state of ["NONE", "STALE", "UNKNOWN"])
  test(`${state} has no numeric count`, () => {
    const body = sample();
    body.environments[0].cells.IAM = { state, counts: null };
    assert.equal(securitySummary(body, plan, now)[0].cells.IAM.total, null);
    body.environments[0].cells.IAM.counts = {};
    assert.throws(() => securitySummary(body, plan, now));
  });
for (const kind of [
  "plan",
  "digest",
  "env",
  "duplicate",
  "future",
  "stale",
  "missing",
  "verdict",
  "boolean",
  "overflow",
  "severity",
  "sum",
  "raw",
  "permission",
])
  test(`security rejects ${kind}`, () => {
    const body = sample(),
      cell = body.environments[0].cells.IAM;
    if (kind === "plan") body.plan_id = "other";
    if (kind === "digest") body.plan_digest = "b".repeat(64);
    if (kind === "env") body.environments[0].environment_id = "other";
    if (kind === "duplicate")
      body.environments[1] = structuredClone(body.environments[0]);
    if (kind === "future") body.queried_at = new Date(now + 1).toISOString();
    if (kind === "stale") body.queried_at = new Date(now - 15001).toISOString();
    if (kind === "missing") delete body.environments[0].cells.AUDIT;
    if (kind === "verdict") cell.state = "PASS";
    if (kind === "boolean") cell.counts = { 1: true };
    if (kind === "overflow") cell.counts = { 1: 2 ** 53 };
    if (kind === "severity") cell.counts = { 7: 1 };
    if (kind === "sum") cell.counts = { 1: Number.MAX_SAFE_INTEGER, 2: 1 };
    if (kind === "raw") cell.raw = "do-not-show";
    if (kind === "permission") body.for_gate_decisions = true;
    assert.throws(() => securitySummary(body, plan, now));
  });
