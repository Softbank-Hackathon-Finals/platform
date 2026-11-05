// T10 display model. This client never issues an execution permission.
export const STATES = [
  "COLLECT",
  "ANALYZE",
  "PROPOSE",
  "REHEARSE",
  "GATE",
  "AWAIT_APPROVAL",
  "SHIFT",
  "CUTOVER",
  "WATCH",
  "FAILBACK",
  "SAFE_HOLD",
  "NEEDS_HUMAN",
  "DONE",
  "DONE_ROLLED_BACK",
];
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const IMAGE = /^sha256:[0-9a-f]{64}$/;
export function fail(code) {
  throw new Error(code);
}
export function identifier(v) {
  return typeof v === "string" && ID.test(v);
}
export function evaluationProfile(v) {
  return typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(v);
}
const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const date = (v) =>
  typeof v === "string" &&
  /^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(v) &&
  Number.isFinite(Date.parse(v));
const clone = (v) => JSON.parse(JSON.stringify(v));
function exact(v, fields) {
  if (
    !object(v) ||
    Object.keys(v).sort().join(",") !== fields.split(" ").sort().join(",")
  )
    fail("invalid_contract");
}
export function binding(v) {
  exact(
    v,
    "plan_id plan_digest diff_hash image_digest target_environment_id expires_at gate_id context_digest",
  );
  if (
    ![v.plan_id, v.target_environment_id, v.gate_id].every(identifier) ||
    ![v.plan_digest, v.context_digest].every(
      (x) => typeof x === "string" && DIGEST.test(x),
    ) ||
    ![v.diff_hash, v.image_digest].every(
      (x) => typeof x === "string" && IMAGE.test(x),
    ) ||
    !date(v.expires_at)
  )
    fail("invalid_binding");
  return Object.fromEntries(
    "plan_id plan_digest diff_hash image_digest target_environment_id expires_at gate_id context_digest"
      .split(" ")
      .map((k) => [k, v[k]]),
  );
}
// S2 has no mandatory gate and includes the rehearsal run in the request.
export function changeBinding(v) {
  const fields =
    "plan_id plan_digest diff_hash image_digest target_environment_id expires_at";
  const hasGate = object(v) && ("gate_id" in v || "context_digest" in v);
  exact(v, fields + (hasGate ? " gate_id context_digest" : ""));
  if (
    ![v.plan_id, v.target_environment_id].every(identifier) ||
    typeof v.plan_digest !== "string" ||
    !DIGEST.test(v.plan_digest) ||
    ![v.diff_hash, v.image_digest].every(
      (x) => typeof x === "string" && IMAGE.test(x),
    ) ||
    !date(v.expires_at) ||
    (hasGate &&
      (!identifier(v.gate_id) ||
        typeof v.context_digest !== "string" ||
        !DIGEST.test(v.context_digest)))
  )
    fail("invalid_change_binding");
  return Object.fromEntries(
    (fields + (hasGate ? " gate_id context_digest" : ""))
      .split(" ")
      .map((k) => [k, v[k]]),
  );
}
// Failback has its own binding; it never reuses CUTOVER's execution receipt.
export function failbackBinding(v, plan) {
  exact(
    v,
    "plan_id plan_digest source_environment_id target_environment_id cutover_execution_id cutover_epoch integrity_receipt_id expires_at",
  );
  if (
    v.plan_id !== plan ||
    ![
      v.plan_id,
      v.source_environment_id,
      v.target_environment_id,
      v.cutover_execution_id,
      v.integrity_receipt_id,
    ].every(identifier) ||
    v.source_environment_id === v.target_environment_id ||
    typeof v.plan_digest !== "string" ||
    !DIGEST.test(v.plan_digest) ||
    !Number.isSafeInteger(v.cutover_epoch) ||
    v.cutover_epoch < 1 ||
    !date(v.expires_at)
  )
    fail("invalid_failback_binding");
  return clone(v);
}
export function failbackRecord(v) {
  const required = "command_id approval_id approved_by status started_at".split(
    " ",
  );
  const optional =
    "finished_at recovery_ms execution_id integrity_receipt_id".split(" ");
  if (
    !object(v) ||
    required.some((k) => !Object.hasOwn(v, k)) ||
    Object.keys(v).some((k) => ![...required, ...optional].includes(k)) ||
    ![v.command_id, v.approval_id, v.approved_by].every(identifier) ||
    !["edge_pending", "rolled_back", "aborted"].includes(v.status) ||
    !date(v.started_at) ||
    !identifier(v.execution_id) ||
    ("integrity_receipt_id" in v && !identifier(v.integrity_receipt_id)) ||
    (v.status !== "aborted" && !identifier(v.integrity_receipt_id)) ||
    ("finished_at" in v &&
      (!date(v.finished_at) ||
        Date.parse(v.finished_at) < Date.parse(v.started_at))) ||
    ("recovery_ms" in v &&
      (!Number.isSafeInteger(v.recovery_ms) || v.recovery_ms < 0)) ||
    (v.status === "edge_pending" &&
      ("finished_at" in v || "recovery_ms" in v)) ||
    (v.status !== "edge_pending" && !date(v.finished_at)) ||
    (v.status === "rolled_back" && !Number.isSafeInteger(v.recovery_ms))
  )
    fail("invalid_failback_record");
  return clone(v);
}
export function failbackSummary(record) {
  if (!record) return "복귀 검증 기록 없음";
  const v = failbackRecord(record);
  return {
    edge_pending: "DB 복귀 검증 기록됨 · 트래픽 복귀 확인 대기",
    rolled_back: `복귀 완료 기록됨 · 복구 시간 ${v.recovery_ms} ms`,
    aborted: "쓰기 전 복귀 중단 · 대상 환경 관측으로 돌아감",
  }[v.status];
}
export function snapshot(v, plan) {
  const hasProfile =
    object(v) &&
    (Object.hasOwn(v, "profile_id") || Object.hasOwn(v, "profile_version"));
  exact(
    v,
    "schema_version plan_id workload_id source_environment_id target_environment_id plan_digest collection_scope_digest state version created_at updated_at last_event_id" +
      (hasProfile ? " profile_id profile_version" : ""),
  );
  if (
    v.schema_version !== "1.0" ||
    v.plan_id !== plan ||
    (hasProfile &&
      ![v.profile_id, v.profile_version].every(evaluationProfile)) ||
    ![
      v.plan_id,
      v.workload_id,
      v.source_environment_id,
      v.target_environment_id,
    ].every(identifier) ||
    ![v.plan_digest, v.collection_scope_digest].every(
      (x) => typeof x === "string" && DIGEST.test(x),
    ) ||
    !STATES.includes(v.state) ||
    !Number.isSafeInteger(v.version) ||
    v.version < 1 ||
    !date(v.created_at) ||
    !date(v.updated_at)
  )
    fail("invalid_snapshot");
  const seq = Number(v.last_event_id?.slice(plan.length + 1));
  if (
    v.last_event_id !== `${plan}:${seq}` ||
    !Number.isSafeInteger(seq) ||
    seq < 1
  )
    fail("invalid_cursor");
  return clone(v);
}
export function event(v, plan) {
  exact(
    v,
    "schema_version event_id sequence plan_id run_id occurred_at event_type data",
  );
  if (
    v.schema_version !== "1.0" ||
    v.plan_id !== plan ||
    !identifier(v.run_id) ||
    !Number.isSafeInteger(v.sequence) ||
    v.sequence < 1 ||
    v.event_id !== `${plan}:${v.sequence}` ||
    !date(v.occurred_at) ||
    !object(v.data)
  )
    fail("invalid_event");
  const d = v.data;
  if (v.event_type === "state.changed") {
    exact(d, "from to");
    if (!(d.from === null || STATES.includes(d.from)) || !STATES.includes(d.to))
      fail("invalid_state");
  } else if (v.event_type === "gate.completed") {
    exact(d, "gate_id evidence_ref decision context_digest");
    if (
      ![d.gate_id, d.evidence_ref].every(identifier) ||
      !["PASS", "BLOCK"].includes(d.decision) ||
      !DIGEST.test(d.context_digest)
    )
      fail("invalid_gate");
  } else if (
    v.event_type === "approval.requested" ||
    v.event_type === "approval.decided"
  ) {
    exact(
      d,
      `approval_id approval_kind binding${v.event_type.endsWith("decided") ? " decision actor_id" : ""}`,
    );
    if (
      !identifier(d.approval_id) ||
      !["change", "cutover", "failback"].includes(d.approval_kind)
    )
      fail("invalid_approval");
    // Approval never supplies a change-apply route or a cutover receipt.
    if (d.approval_kind === "cutover") binding(d.binding);
    else if (d.approval_kind === "failback") failbackBinding(d.binding, plan);
    else changeBinding(d.binding);
    if (
      v.event_type.endsWith("decided") &&
      (!identifier(d.actor_id) ||
        !["approved", "rejected", "expired"].includes(d.decision))
    )
      fail("invalid_decision");
  } else if (v.event_type === "failback.recorded") {
    failbackRecord(d);
    if (d.command_id !== v.run_id) fail("failback_run_mismatch");
  } else if (v.event_type === "execution.updated") {
    if (
      Object.keys(d).some(
        (k) => !["execution_id", "step", "status", "elapsed_ms"].includes(k),
      ) ||
      !identifier(d.execution_id) ||
      !identifier(d.step) ||
      !["started", "succeeded", "failed", "unknown"].includes(d.status) ||
      ("elapsed_ms" in d &&
        (!Number.isSafeInteger(d.elapsed_ms) || d.elapsed_ms < 0))
    )
      fail("invalid_execution");
  } else fail("unknown_event");
  return clone(v);
}
export class Model {
  constructor(plan) {
    if (!identifier(plan)) fail("invalid_plan");
    this.plan = plan;
    this.cursor = 0;
    this.events = [];
    this.connected = false;
    this.gate = null;
    this.request = null;
    this.approval = null;
    this.failback = null;
    this.state = null;
    this.snapshotAt = 0;
    this.snapshot = null;
    this.error = null;
    this.historyBytes = 0;
  }
  observeSnapshot(value, now) {
    const next = snapshot(value, this.plan);
    if (this.snapshot) {
      for (const key of [
        "plan_id",
        "plan_digest",
        "collection_scope_digest",
        "workload_id",
        "source_environment_id",
        "target_environment_id",
        "profile_id",
        "profile_version",
      ])
        if (next[key] !== this.snapshot[key]) fail("plan_changed");
      if (next.version < this.snapshot.version) fail("snapshot_regressed");
    }
    if (this.snapshot && next.version !== this.snapshot.version)
      this.approval = null;
    this.snapshot = next;
    this.snapshotAt = now;
  }
  accept(value) {
    const next = event(value, this.plan);
    if (next.sequence <= this.cursor) {
      const previous = this.events.find((x) => x.sequence === next.sequence);
      if (!previous || JSON.stringify(previous) !== JSON.stringify(next))
        fail("replay_changed");
      return false;
    }
    if (next.sequence !== this.cursor + 1) fail("event_gap");
    if (next.data.approval_kind === "failback" && this.snapshot) {
      const b = next.data.binding;
      if (
        b.plan_digest !== this.snapshot.plan_digest ||
        b.source_environment_id !== this.snapshot.target_environment_id ||
        b.target_environment_id !== this.snapshot.source_environment_id
      )
        fail("failback_scope_mismatch");
    }
    const size = new TextEncoder().encode(JSON.stringify(next)).length;
    if (
      this.historyBytes + size > 8 * 1024 * 1024 ||
      this.events.length >= 10000
    )
      fail("history_limit");
    this.historyBytes += size;
    if (next.event_type === "state.changed") {
      if (next.data.from !== this.state) fail("state_history_mismatch");
      this.state = next.data.to;
    } else if (next.event_type === "failback.recorded") {
      this.failback = next;
      this.approval = null;
    } else if (next.event_type === "gate.completed") {
      this.gate = next;
      this.request = null;
      this.approval = null;
    } else if (next.event_type === "approval.requested") {
      this.request = next;
      this.approval = null;
    } else if (next.event_type === "approval.decided") {
      // An event alone cannot be used as this browser's execution receipt.
      if (
        next.data.approval_kind !== "cutover" ||
        !this.approval ||
        next.data.approval_id !== this.approval.approval_id ||
        next.data.decision !== "approved" ||
        JSON.stringify(binding(next.data.binding)) !==
          JSON.stringify(binding(this.approval.binding))
      )
        this.approval = null;
    }
    this.events.push(next);
    this.cursor = next.sequence;
    if (this.events.length > 10000) fail("history_limit");
    return true;
  }
  eligible(now) {
    if (
      !this.connected ||
      this.error ||
      !this.snapshot ||
      now < this.snapshotAt ||
      now - this.snapshotAt >= 15000
    )
      return false;
    if (
      this.state !== "SHIFT" ||
      this.snapshot.state !== "SHIFT" ||
      Number(this.snapshot.last_event_id.split(":")[1]) > this.cursor
    )
      return false;
    const b = this.request?.data?.binding,
      g = this.gate?.data;
    return (
      this.request?.data.approval_kind === "cutover" &&
      g?.decision === "PASS" &&
      b.plan_id === this.plan &&
      b.plan_digest === this.snapshot.plan_digest &&
      b.target_environment_id === this.snapshot.target_environment_id &&
      b.gate_id === g.gate_id &&
      b.context_digest === g.context_digest &&
      Date.parse(b.expires_at) > now
    );
  }
  changeReady(now) {
    const req = this.request,
      b = req?.data.binding;
    return Boolean(
      this.connected &&
        !this.error &&
        this.snapshot &&
        now >= this.snapshotAt &&
        now - this.snapshotAt < 15000 &&
        this.state === "REHEARSE" &&
        this.snapshot.state === "REHEARSE" &&
        Number(this.snapshot.last_event_id.split(":")[1]) <= this.cursor &&
        req?.data.approval_kind === "change" &&
        b.plan_id === this.plan &&
        b.plan_digest === this.snapshot.plan_digest &&
        b.target_environment_id === this.snapshot.target_environment_id &&
        Date.parse(b.expires_at) > now,
    );
  }
  changeEligible(now) {
    const req = this.request;
    return (
      this.changeReady(now) &&
      !this.events.some(
        (e) =>
          e.sequence > req.sequence &&
          e.event_type === "approval.decided" &&
          e.data.approval_kind === "change" &&
          e.run_id === req.run_id &&
          JSON.stringify(changeBinding(e.data.binding)) ===
            JSON.stringify(changeBinding(req.data.binding)),
      )
    );
  }
  changeApprovalRequest(now) {
    if (!this.changeEligible(now)) fail("change_approval_not_ready");
    return {
      approval_kind: "change",
      run_id: this.request.run_id,
      binding: changeBinding(this.request.data.binding),
      expected_version: this.snapshot.version,
    };
  }
  recordChangeApproval(receipt, sent) {
    exact(
      receipt,
      "approval_id approval_kind run_id binding actor_id approved_at",
    );
    if (
      sent.approval_kind !== "change" ||
      receipt.approval_kind !== "change" ||
      !identifier(receipt.approval_id) ||
      !identifier(receipt.actor_id) ||
      !identifier(receipt.run_id) ||
      receipt.run_id !== sent.run_id ||
      !date(receipt.approved_at) ||
      JSON.stringify(changeBinding(receipt.binding)) !==
        JSON.stringify(changeBinding(sent.binding))
    )
      fail("invalid_change_approval_receipt");
    // S2 receipt must never become the S3 execution receipt.
    return clone(receipt);
  }
  approvalRequest(now) {
    if (!this.eligible(now)) fail("approval_not_ready");
    return {
      approval_kind: "cutover",
      binding: binding(this.request.data.binding),
      expected_version: this.snapshot.version,
    };
  }
  recordApproval(receipt, sent) {
    exact(receipt, "approval_id approval_kind binding actor_id approved_at");
    if (
      !identifier(receipt.approval_id) ||
      !identifier(receipt.actor_id) ||
      receipt.approval_kind !== "cutover" ||
      !date(receipt.approved_at) ||
      JSON.stringify(binding(receipt.binding)) !==
        JSON.stringify(binding(sent.binding))
    )
      fail("invalid_approval_receipt");
    this.approval = clone(receipt);
  }
}

export function observation(value, current, now) {
  if (
    !object(value) ||
    value.kind !== "console-observation.v1" ||
    value.plan_id !== current.plan_id ||
    value.plan_digest !== current.plan_digest ||
    value.for_gate_decisions !== false ||
    !date(value.queried_at) ||
    now < Date.parse(value.queried_at) ||
    now - Date.parse(value.queried_at) > 15000 ||
    !Array.isArray(value.environments) ||
    value.environments.length !== 2
  )
    fail("invalid_observation");
  const rows = [];
  for (const side of ["source", "target"]) {
    const found = value.environments.filter(
      (r) =>
        object(r) &&
        r.side === side &&
        r.environment_id === current[side + "_environment_id"],
    );
    if (found.length !== 1) fail("observation_scope_mismatch");
    const row = found[0];
    if (!["OK", "UNOBSERVABLE"].includes(row.state))
      fail("invalid_observation_state");
    const values = {
      requests_per_second: null,
      error_ratio: null,
      p95_seconds: null,
    };
    if (row.state === "OK") {
      if (
        !date(row.queried_at) ||
        now < Date.parse(row.queried_at) ||
        now - Date.parse(row.queried_at) > 30000
      )
        fail("stale_observation");
      for (const key of Object.keys(values)) {
        const v = row[key];
        if (
          v !== null &&
          (typeof v !== "number" ||
            !Number.isFinite(v) ||
            v < 0 ||
            (key === "error_ratio" && v > 1))
        )
          fail("invalid_observation_value");
        values[key] = v;
      }
    }
    rows.push({ side, state: row.state, ...values });
  }
  return rows;
}

export const CONTROL_TESTS = Object.freeze({
  "SEC-01.1": "다른 서비스 계정의 Secret 읽기 거부",
  "SEC-01.2": "소스·매니페스트의 평문 비밀 검사",
  "SEC-01.3": "로그의 비밀 노출 검사",
  "SEC-01.4": "비밀 회전 후 새 DB 연결·쓰기",
  "ENC-02.1": "비밀 저장소 암호화",
  "ENC-01.1": "앱 → Pooler 인증서 검증 TLS",
  "ENC-01.2": "잘못된 CA·호스트 이름 거부",
  "ENC-01.3": "평문 DB 연결 거부",
  "ENC-01.4": "DB 복제 구간 인증서 검증 TLS",
  "ENC-01.5": "Pooler → DB 인증서 검증 TLS",
  "AUD-01.1": "관리자 로그인 감사 검색",
  "AUD-01.2": "DDL 생성·삭제 감사 검색",
  "AUD-01.3": "권한 변경 감사 검색",
  "NET-01.1": "허용 목록 외 공개 포트 검사",
  "NET-01.2": "외부 DB 접속 거부·대조군 확인",
  "NET-01.3": "다른 namespace DB 접속 거부·대조군 확인",
  "IAM-01.1": "익명 첨부파일 읽기 거부",
  "IAM-01.2": "최소 권한 기준선 검사",
  "ISO-01.1": "앱 간 첨부·DB 접근 격리",
  "DR-01.1": "새 DB 복원 후 표식·행 수 확인",
});

// A bound historical read model, never a new execution/approval authority.
export function controlReport(value, current, gate, now) {
  if (
    !object(value) ||
    value.kind !== "console-control-report.v1" ||
    value.plan_id !== current.plan_id ||
    value.plan_digest !== current.plan_digest ||
    value.source_environment_id !== current.source_environment_id ||
    value.target_environment_id !== current.target_environment_id ||
    value.authorizes_cutover !== false ||
    !gate ||
    value.gate_id !== gate.data.gate_id ||
    value.context_digest !== gate.data.context_digest ||
    !/^[0-9a-f]{64}$/.test(value.report_digest) ||
    !date(value.queried_at) ||
    now < Date.parse(value.queried_at) ||
    now - Date.parse(value.queried_at) > 15000 ||
    !date(value.evaluated_at) ||
    Date.parse(value.evaluated_at) > now ||
    !object(value.profile) ||
    !["demo-gate", "full"].includes(value.profile.id) ||
    value.profile.version !== "1" ||
    !["PASS", "BLOCK", "EXCEPTION"].includes(value.decision) ||
    !Array.isArray(value.rows) ||
    value.rows.length !== (value.profile.id === "full" ? 20 : 3) ||
    !Array.isArray(value.run_issues) ||
    value.run_issues.some(
      (x) =>
        ![
          "RUN_NOT_FOUND",
          "APP_MISMATCH",
          "ENVIRONMENT_ROLE_MISMATCH",
          "SAME_ENVIRONMENT",
        ].includes(x),
    )
  )
    fail("invalid_control_report");
  const seen = new Set();
  const reasons = [
    "BOTH_PASS",
    "IMPROVED",
    "TARGET_FAIL",
    "SOURCE_UNKNOWN",
    "TARGET_UNKNOWN",
    "EVIDENCE_EXPIRED",
    "METHOD_MISMATCH",
    "EXCEPTION_APPROVED",
    "MISSING_RESULT",
    "DUPLICATE_RESULT",
    "INVARIANT_VIOLATION",
    "EXCEPTION_INVALID",
  ];
  for (const row of value.rows) {
    if (
      !object(row) ||
      !Object.hasOwn(CONTROL_TESTS, row.test_id) ||
      row.control_id !== row.test_id.split(".")[0] ||
      seen.has(row.test_id) ||
      !["PASS", "FAIL", "UNKNOWN"].includes(row.source_verdict) ||
      !["PASS", "FAIL", "UNKNOWN"].includes(row.target_verdict) ||
      !["PASS", "BLOCK", "EXCEPTION"].includes(row.decision) ||
      !reasons.includes(row.reason) ||
      typeof row.improved !== "boolean" ||
      typeof row.exception_applied !== "boolean"
    )
      fail("invalid_control_row");
    seen.add(row.test_id);
  }
  if (
    value.profile.id === "demo-gate" &&
    !["IAM-01.1", "SEC-01.1", "AUD-01.1"].every((x) => seen.has(x))
  )
    fail("incomplete_control_report");
  return clone(value);
}
