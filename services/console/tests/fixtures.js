export const now = Date.parse("2026-11-04T00:00:00Z");
export const plan = "plan-demo";
export const bind = {
  plan_id: plan,
  plan_digest: "a".repeat(64),
  diff_hash: "sha256:" + "c".repeat(64),
  image_digest: "sha256:" + "d".repeat(64),
  target_environment_id: "env-target",
  expires_at: "2026-11-04T00:10:00Z",
  gate_id: "gate-one",
  context_digest: "e".repeat(64),
};
export const snap = {
  schema_version: "1.0",
  plan_id: plan,
  workload_id: "chat",
  source_environment_id: "env-source",
  target_environment_id: "env-target",
  plan_digest: "a".repeat(64),
  collection_scope_digest: "b".repeat(64),
  state: "SHIFT",
  version: 1,
  created_at: "2026-11-03T00:00:00Z",
  updated_at: "2026-11-04T00:00:00Z",
  last_event_id: plan + ":3",
};
export function ev(sequence, event_type, data) {
  return {
    schema_version: "1.0",
    event_id: `${plan}:${sequence}`,
    sequence,
    plan_id: plan,
    run_id: "run-demo",
    occurred_at: "2026-11-04T00:00:00Z",
    event_type,
    data,
  };
}
export const events = [
  ev(1, "state.changed", { from: null, to: "SHIFT" }),
  ev(2, "gate.completed", {
    gate_id: bind.gate_id,
    context_digest: bind.context_digest,
    decision: "PASS",
    evidence_ref: "evidence-one",
  }),
  ev(3, "approval.requested", {
    approval_id: "requested-one",
    approval_kind: "cutover",
    binding: bind,
  }),
];
export const receipt = {
  approval_id: "approved-one",
  approval_kind: "cutover",
  binding: bind,
  actor_id: "operator-demo",
  approved_at: "2026-11-04T00:00:01Z",
};
