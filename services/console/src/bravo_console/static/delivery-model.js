import { identifier } from "./model.js";
import { ready } from "./operations.js";
import { canonical } from "./panels.js";
const hash = (v, n) =>
  typeof v === "string" && new RegExp(`^[a-f0-9]{${n}}$`).test(v);
const object = (v) => v && typeof v === "object" && !Array.isArray(v);
const time = (v) =>
  typeof v === "string" &&
  /(?:Z|[+-]\d\d:\d\d)$/.test(v) &&
  Number.isFinite(Date.parse(v));
const require = (ok) => {
  if (!ok) throw Error("invalid_delivery_contract");
};
export const SYNC_NAMES = {
  NOT_INSTALLED: "미설치",
  BOOTSTRAP_ONLY: "기반만 설치됨",
  BOOTSTRAP_PENDING: "기반 준비 중",
  SYNC_PAUSED: "동기화 일시 정지",
  SYNC_IN_PROGRESS: "동기화 진행 중",
  SYNC_RETRYING: "동기화 재시도 중",
  SYNC_PENDING: "반영 확인 대기",
  BLOCKED: "동기화 확인 차단",
  SYNC_VERIFIED: "지정 커밋 반영 확인",
  UNCONFIGURED: "수집 연결 전",
  UNKNOWN: "확인 불가",
};
export function deliveryDocument(v, model, now = Date.now()) {
  require(
    object(v) &&
      v.kind === "console-delivery.v1" &&
      v.authorizes_execution === false &&
      v.plan_id === model.plan &&
      v.plan_digest === model.snapshot.plan_digest &&
      v.plan_version === model.snapshot.version &&
      identifier(v.subject_id) &&
      time(v.queried_at) &&
      time(v.expires_at) &&
      Date.parse(v.queried_at) <= now &&
      now < Date.parse(v.expires_at) &&
      Date.parse(v.expires_at) - Date.parse(v.queried_at) <= 15000,
  );
  require(
    object(v.iac) &&
      typeof v.iac.configured === "boolean" &&
      object(v.iac.actions) &&
      ["apply_infra", "approve_infra"].every(
        (key) => typeof v.iac.actions[key] === "boolean",
      ) &&
      Array.isArray(v.iac.modules) &&
      v.iac.modules.length <= 32 &&
      Array.isArray(v.customers) &&
      v.customers.length <= 2,
  );
  const pair = [
    model.snapshot.source_environment_id,
    model.snapshot.target_environment_id,
  ];
  require(
    new Set(v.iac.modules.map((m) => `${m.environment_id}:${m.module}`))
      .size === v.iac.modules.length,
  );
  for (const m of v.iac.modules)
    require(
      pair.includes(m.environment_id) &&
        identifier(m.environment) &&
        identifier(m.module) &&
        object(m.params) &&
        JSON.stringify(m.params).length <= 8192,
    );
  require(
    new Set(v.customers.map((c) => c.environment_id)).size ===
      v.customers.length,
  );
  for (const c of v.customers) {
    require(
      pair.includes(c.environment_id) &&
        identifier(c.environment) &&
        hash(c.expected_revision, 40) &&
        Object.hasOwn(SYNC_NAMES, c.status) &&
        ["live", "synthetic", "historical"].includes(c.provenance),
    );
    if (!["UNCONFIGURED", "UNKNOWN"].includes(c.status)) {
      require(
        c.authorizes_cutover === false &&
          c.migration_verified === false &&
          c.control_equivalence_verified === false &&
          c.cluster_identity_matches === true &&
          time(c.observed_at) &&
          time(c.valid_until) &&
          Date.parse(c.observed_at) <= now &&
          now < Date.parse(c.valid_until) &&
          Date.parse(c.valid_until) - Date.parse(c.observed_at) <= 300000 &&
          Array.isArray(c.reasons) &&
          c.reasons.length <= 20 &&
          c.reasons.every(
            (r) => typeof r === "string" && /^[A-Z0-9_]{1,80}$/.test(r),
          ),
      );
      if (c.status === "SYNC_RETRYING")
        require(Number.isSafeInteger(c.retry_count) && c.retry_count > 0);
      if (c.status === "SYNC_VERIFIED")
        require(
          c.reasons.length === 0 &&
            Number.isSafeInteger(c.synced_resource_count) &&
            c.synced_resource_count > 0,
        );
    }
  }
  return structuredClone(v);
}
export function deliveryFresh(value, model, now = Date.now()) {
  try {
    deliveryDocument(value, model, now);
    return ready(model, now);
  } catch {
    return false;
  }
}
export function iacPlan(v, expectedId = null) {
  require(
    object(v) &&
      identifier(v.iac_plan_id) &&
      (!expectedId || v.iac_plan_id === expectedId) &&
      identifier(v.environment) &&
      identifier(v.module) &&
      hash(v.base_sha, 40) &&
      hash(v.plan_digest, 64) &&
      typeof v.accepted === "boolean" &&
      identifier(v.requested_by) &&
      Number.isSafeInteger(v.changes) &&
      v.changes >= 0 &&
      Array.isArray(v.refusals) &&
      v.refusals.length <= 32 &&
      v.refusals.every((r) => typeof r === "string" && r.length <= 160) &&
      Array.isArray(v.effects) &&
      JSON.stringify(v.effects).length <= 32768 &&
      canonical(v.binding) ===
        canonical({
          infra_plan_id: v.iac_plan_id,
          plan_digest: v.plan_digest,
          requested_by: v.requested_by,
        }),
  );
  if (v.pull_request) pullRequest(v.pull_request, v);
  return structuredClone(v);
}
export function infraApproval(v, plan, now = Date.now(), expectedId = null) {
  require(
    object(v) &&
      v.approval_kind === "infra" &&
      identifier(v.approval_id) &&
      (!expectedId || v.approval_id === expectedId) &&
      identifier(v.actor_id) &&
      v.actor_id !== plan.requested_by &&
      canonical(v.binding) === canonical(plan.binding) &&
      time(v.approved_at) &&
      time(v.expires_at) &&
      Date.parse(v.approved_at) <= now &&
      Date.parse(v.approved_at) < Date.parse(v.expires_at),
  );
  return structuredClone(v);
}
export function approvalUsable(v, plan, now = Date.now()) {
  try {
    infraApproval(v, plan, now);
    return (
      now < Date.parse(v.expires_at) &&
      v.revoked === false &&
      (v.claimed_run_id === null || v.claimed_run_id === plan.iac_plan_id)
    );
  } catch {
    return false;
  }
}
export function pullRequest(v, plan, approvalId = null) {
  require(
    object(v) &&
      v.iac_plan_id === plan.iac_plan_id &&
      v.plan_digest === plan.plan_digest &&
      identifier(v.approval_id) &&
      (!approvalId || v.approval_id === approvalId) &&
      hash(v.head_sha, 40) &&
      hash(v.base_sha, 40) &&
      Number.isSafeInteger(v.number) &&
      v.number > 0 &&
      v.url ===
        `https://github.com/Softbank-Hackathon-Finals/platform/pull/${v.number}`,
  );
  return structuredClone(v);
}
export async function createdIacId(planId, key) {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(`${planId}\0${key}`),
  );
  return (
    "iac-" +
    Array.from(new Uint8Array(bytes), (b) => b.toString(16).padStart(2, "0"))
      .join("")
      .slice(0, 24)
  );
}
