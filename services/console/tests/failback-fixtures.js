import { plan, snap, ev } from "./fixtures.js";
export const fb = {
  plan_id: plan,
  plan_digest: snap.plan_digest,
  source_environment_id: snap.target_environment_id,
  target_environment_id: snap.source_environment_id,
  cutover_execution_id: "cutover-one",
  cutover_epoch: 10,
  integrity_receipt_id: "integrity-one",
  expires_at: "2026-11-04T00:10:00Z",
};
export const record = {
  command_id: "failback-one",
  approval_id: "approval-failback",
  approved_by: "operator-one",
  started_at: "2026-11-04T00:01:00Z",
  execution_id: "execution-two",
  integrity_receipt_id: "integrity-two",
  status: "edge_pending",
};
export function timeline(status = "rolled_back") {
  const last =
    status === "aborted"
      ? { ...record, status, finished_at: "2026-11-04T00:01:09Z" }
      : {
          ...record,
          status,
          finished_at: "2026-11-04T00:01:09Z",
          recovery_ms: 9000,
        };
  if (status === "aborted") delete last.integrity_receipt_id;
  const raw = [
    ev(1, "state.changed", { from: null, to: "WATCH" }),
    ev(2, "approval.requested", {
      approval_id: record.approval_id,
      approval_kind: "failback",
      binding: fb,
    }),
    ev(3, "approval.decided", {
      approval_id: record.approval_id,
      approval_kind: "failback",
      binding: fb,
      decision: "approved",
      actor_id: "operator-one",
    }),
    ev(4, "state.changed", { from: "WATCH", to: "FAILBACK" }),
    {
      ...ev(5, "failback.recorded", status === "aborted" ? last : record),
      run_id: record.command_id,
    },
  ];
  if (status === "edge_pending") return raw;
  if (status === "rolled_back")
    raw.push({
      ...ev(6, "failback.recorded", last),
      run_id: record.command_id,
    });
  raw.push(
    ev(raw.length + 1, "state.changed", {
      from: "FAILBACK",
      to: status === "aborted" ? "WATCH" : "DONE_ROLLED_BACK",
    }),
  );
  return raw;
}
