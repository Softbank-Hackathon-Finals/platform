import test from "node:test";
import assert from "node:assert/strict";
import { agentAudit } from "../src/bravo_console/static/audit.js";
const plan = { plan_id: "plan-one", plan_digest: "b".repeat(64) };
function fixture() {
  return {
    kind: "console-agent-audit.v1",
    ...plan,
    run_id: "run-one",
    record_count: 1,
    chain_digest: "a".repeat(64),
    authorizes_cutover: false,
    retention_verified: false,
    rows: [
      {
        record_id: "run-one:1",
        origin_id: "t11:run-one:attempt1:0",
        attempt: 1,
        tool: "rehearsal.exec",
        outcome: "DENIED",
        reason: "DESTRUCTIVE_ATTEMPT_BLOCKED",
        category: "bucket_delete",
        recorded_at: "2026-11-05T01:00:00Z",
      },
    ],
  };
}
test("registered stored denial preserves identity", () =>
  assert.equal(agentAudit(fixture(), plan).rows.length, 1));
for (const [name, mutate] of Object.entries({
  foreign: (v) => (v.plan_id = "other"),
  digest: (v) => (v.plan_digest = "c".repeat(64)),
  approval: (v) => (v.authorizes_cutover = true),
  retention: (v) => (v.retention_verified = true),
  count: (v) => (v.record_count = 0),
  duplicate: (v) => v.rows.push(v.rows[0]),
  source: (v) => (v.rows[0].origin_id = "t11:other:attempt1:0"),
  malformedOrigin: (v) => (v.rows[0].origin_id = "t11:run-one:attempt1:0:1"),
  denial: (v) => (v.rows[0].outcome = "ALLOWED"),
  script: (v) => (v.rows[0].tool = "<img onerror=x>"),
  time: (v) => (v.rows[0].recorded_at = "bad"),
  attempt: (v) => (v.rows[0].attempt = true),
}))
  test(`reject ${name}`, () => {
    const v = fixture();
    mutate(v);
    assert.throws(() => agentAudit(v, plan));
  });
