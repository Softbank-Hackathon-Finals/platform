import test from "node:test";
import assert from "node:assert/strict";
import { agentAudit, auditSummary } from "../src/bravo_console/static/audit.js";
const plan = { plan_id: "plan-one", plan_digest: "b".repeat(64) };
function fixture() {
  return {
    kind: "console-agent-audit.v1",
    ...plan,
    run_id: "run-one",
    record_count: 1,
    tool_records: 1,
    rows_total: 1,
    unregistered_records: 0,
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
test("a new T11 tool and a tool outside the rule stay on their own row (#270 P1)", () => {
  const v = fixture();
  v.rows[0].tool = "sandbox.exec";
  assert.equal(agentAudit(v, plan).rows[0].tool, "sandbox.exec");
  v.rows[0].tool = null;
  assert.equal(agentAudit(v, plan).rows[0].tool, null);
});
test("no tool record reads as unknown, never as zero blocks (#270 P2)", () => {
  const v = fixture();
  Object.assign(v, { tool_records: 0, rows_total: 0, rows: [] });
  const text = auditSummary(agentAudit(v, plan));
  assert.match(text, /도구 기록 확인 불가/);
  assert.doesNotMatch(text, /차단 0건/);
  assert.match(auditSummary(fixture()), /버킷 삭제 차단 1건/);
});
test("a long run is not rejected for its length (#270 P3)", () => {
  const v = fixture();
  Object.assign(v, { record_count: 5000, tool_records: 4000, rows_total: 300 });
  assert.equal(agentAudit(v, plan).rows.length, 1);
  assert.match(auditSummary(v), /버킷 삭제 차단 300건 \(최근 1건 표시\)/);
});
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
  toolRecords: (v) => (v.tool_records = 2),
  rowsTotal: (v) => (v.rows_total = 0),
  missingCounts: (v) => delete v.unregistered_records,
}))
  test(`reject ${name}`, () => {
    const v = fixture();
    mutate(v);
    assert.throws(() => agentAudit(v, plan));
  });
