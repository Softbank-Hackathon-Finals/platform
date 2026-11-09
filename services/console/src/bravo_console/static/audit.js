// T11 tool name rule (T24 bravo_audit.adapters.TOOL_NAME); null is a tool outside it.
const TOOL = /^[a-z][a-z0-9_.]{0,63}$/;
const count = (n, max) => Number.isInteger(n) && n >= 0 && n <= max;

// A read model only. It cannot grant approval or manufacture a tool event.
export function agentAudit(value, plan) {
  const id = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
  if (
    !value ||
    value.kind !== "console-agent-audit.v1" ||
    value.plan_id !== plan.plan_id ||
    value.plan_digest !== plan.plan_digest ||
    !id.test(value.run_id) ||
    !/^[a-f0-9]{64}$/.test(value.chain_digest) ||
    value.authorizes_cutover !== false ||
    value.retention_verified !== false ||
    !Number.isInteger(value.record_count) ||
    value.record_count < 1 ||
    value.record_count > 100000 ||
    !count(value.tool_records, value.record_count) ||
    !count(value.rows_total, value.tool_records) ||
    !count(value.unregistered_records, 1e9) ||
    !Array.isArray(value.rows) ||
    value.rows.length > Math.min(value.rows_total, 200)
  )
    throw Error("invalid_audit");
  const seen = new Set();
  for (const row of value.rows) {
    const n = row.record_id?.slice(value.run_id.length + 1);
    if (
      !row.record_id?.startsWith(value.run_id + ":") ||
      !/^[1-9][0-9]*$/.test(n) ||
      Number(n) > value.record_count ||
      seen.has(row.record_id) ||
      !Number.isInteger(row.attempt) ||
      row.attempt < 1 ||
      row.attempt > 1000000 ||
      typeof row.origin_id !== "string" ||
      !new RegExp(`^t11:${value.run_id}:attempt${row.attempt}:[0-9]+$`).test(
        row.origin_id,
      ) ||
      row.outcome !== "DENIED" ||
      row.reason !== "DESTRUCTIVE_ATTEMPT_BLOCKED" ||
      row.category !== "bucket_delete" ||
      !(
        row.tool === null ||
        (typeof row.tool === "string" && TOOL.test(row.tool))
      ) ||
      typeof row.recorded_at !== "string" ||
      row.recorded_at.length > 40 ||
      !Number.isFinite(Date.parse(row.recorded_at))
    )
      throw Error("invalid_audit_row");
    seen.add(row.record_id);
  }
  return value;
}

// No tool record is not "no attempt": T10 does not forward tool events yet (schema 1.0).
export function auditSummary(value) {
  const head = `등록된 실행 ${value.run_id} · 저장 기록 ${value.record_count}건`;
  const later = value.unregistered_records
    ? ` · 등록 이후 기록 ${value.unregistered_records}건은 표시하지 않음`
    : "";
  if (value.tool_records === 0)
    return `${head} · 도구 기록 확인 불가 — 이 실행에 에이전트 도구 기록이 없습니다(시도가 없었다는 뜻이 아님)${later}`;
  const shown =
    value.rows.length < value.rows_total
      ? ` (최근 ${value.rows.length}건 표시)`
      : "";
  return `${head} · 버킷 삭제 차단 ${value.rows_total}건${shown}${later}`;
}

export function renderAudit(root, value) {
  root.replaceChildren();
  if (!value) return;
  for (const row of value.rows) {
    const li = document.createElement("li");
    li.textContent = `버킷 삭제 시도 차단 — 에이전트 도구 정책 · ${row.record_id} · 시도 ${row.attempt} · ${row.tool ?? "도구 확인 불가"} · ${row.recorded_at}`;
    root.append(li);
  }
}
