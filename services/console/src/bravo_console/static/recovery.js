// Client recovery is a display guard, never server execution authority.
import { binding, changeBinding } from "./model.js";

// A repeat POST is a new explicit approval action, never a result lookup.
export function canRepeatApproval(model, pending, now) {
  const change = pending.kind === "approve-change";
  if (
    (!change && pending.kind !== "approve") ||
    model.plan !== pending.plan ||
    model.cursor < pending.cursor ||
    model.request?.event_id !== pending.requestEventId ||
    !(change ? model.changeReady(now) : model.eligible(now))
  )
    return false;
  for (const key of [
    "plan_id",
    "plan_digest",
    "collection_scope_digest",
    "workload_id",
    "source_environment_id",
    "target_environment_id",
    "version",
  ])
    if (model.snapshot[key] !== pending.snapshot[key]) return false;
  const current = {
    approval_kind: change ? "change" : "cutover",
    ...(change ? { run_id: model.request.run_id } : {}),
    binding: (change ? changeBinding : binding)(model.request.data.binding),
    expected_version: model.snapshot.version,
  };
  return JSON.stringify(current) === JSON.stringify(pending.body);
}

export function recoveryCommand(model, pending) {
  if (model.plan !== pending.plan || model.error || !model.snapshot)
    return null;
  if (Number(model.snapshot.last_event_id.split(":")[1]) > model.cursor)
    return null;
  const dispatched = model.events.filter(
    (event) =>
      event.sequence > pending.cursor &&
      event.event_type === "state.changed" &&
      event.data.from === "SHIFT" &&
      event.data.to === "CUTOVER",
  );
  // No guessing if more than one execution cycle is present.
  return dispatched.length === 1 ? dispatched[0].run_id : null;
}

export function canResubmit(model, pending, now) {
  if (
    pending.kind !== "execute" ||
    !model.eligible(now) ||
    model.plan !== pending.plan ||
    model.cursor < pending.cursor ||
    recoveryCommand(model, pending)
  )
    return false;
  for (const field of [
    "plan_id",
    "plan_digest",
    "collection_scope_digest",
    "workload_id",
    "source_environment_id",
    "target_environment_id",
    "version",
  ])
    if (model.snapshot[field] !== pending.snapshot[field]) return false;
  const approved = pending.approval;
  const sameBinding = (value) =>
    JSON.stringify(binding(value)) ===
    JSON.stringify(binding(approved.binding));
  if (
    pending.body.approval_id !== approved.approval_id ||
    !sameBinding(model.request.data.binding)
  )
    return false;
  const decisions = model.events.filter(
    (event) =>
      event.event_type === "approval.decided" &&
      event.data.approval_id === approved.approval_id,
  );
  const latest = decisions.at(-1)?.data;
  return Boolean(
    latest &&
      latest.decision === "approved" &&
      latest.actor_id === approved.actor_id &&
      latest.approval_kind === "cutover" &&
      sameBinding(latest.binding),
  );
}
