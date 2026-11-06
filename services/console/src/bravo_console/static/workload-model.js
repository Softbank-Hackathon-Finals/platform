// Presentation only. Evidence and topology come from a registered server provider.
// Nothing in this module authorizes, submits or retries an operational effect.
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const HASH = /^[a-f0-9]{64}$/;
const STAGES = [
  "detect",
  "hold",
  "approve",
  "isolate",
  "replace",
  "verify",
  "route",
  "recover",
];
export const stageLabels = {
  detect: "이상 확인",
  hold: "보안 보류",
  approve: "사람 승인",
  isolate: "격리",
  replace: "대체 준비",
  verify: "독립 검증",
  route: "경로 전환",
  recover: "복구 확인",
};
export const healthLabels = {
  unknown: "상태 미확인",
  ready: "응답 확인",
  degraded: "성능·설정 이상",
  unavailable: "사용 불가",
};
export const securityLabels = {
  unknown: "보호 상태 미확인",
  clear: "선정 통제 확인",
  warning: "보안 이상",
  hold: "보안 보류",
  isolated: "격리 확인",
};
export const linkLabels = {
  unknown: "등록 관계",
  observed: "연결 관측",
  requested: "차단 요청",
  blocked: "차단 확인",
};
export const responseLabels = {
  pending: "대기",
  running: "진행 중",
  requested: "요청됨",
  verified: "확인됨",
  failed: "실패",
  unknown: "결과 미확인",
};
const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const fail = () => {
  throw new Error("invalid_workload_projection");
};
const id = (v) => typeof v === "string" && ID.test(v);
const text = (v, n = 160) =>
  typeof v === "string" &&
  v.length > 0 &&
  v.length <= n &&
  !/[\u0000-\u001f\u007f]/.test(v);
const stamp = (v) =>
  typeof v === "string" &&
  /(?:Z|[+-]\d\d:\d\d)$/.test(v) &&
  Number.isFinite(Date.parse(v));
const enumeration = (v, keys) => typeof v === "string" && keys.includes(v);
const unique = (rows) => new Set(rows.map((r) => r.id)).size === rows.length;
const exact = (v, names) => {
  if (
    !object(v) ||
    Object.keys(v).sort().join(" ") !== names.split(" ").sort().join(" ")
  )
    fail();
};

export function workloadDocument(value, plan, now = Date.now()) {
  exact(
    value,
    "kind authorizes_execution plan_id plan_digest plan_version revision stream_id provenance source_label observed_at expires_at nodes edges evidence responses events",
  );
  if (
    !object(value) ||
    value.kind !== "console-workload.v1" ||
    value.authorizes_execution !== false ||
    value.plan_id !== plan?.plan_id ||
    value.plan_digest !== plan?.plan_digest ||
    value.plan_version !== plan?.version ||
    !id(value.plan_id) ||
    !HASH.test(value.plan_digest) ||
    !Number.isSafeInteger(value.plan_version) ||
    value.plan_version < 1 ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    !id(value.stream_id) ||
    !enumeration(value.provenance, ["live", "synthetic", "historical"]) ||
    !text(value.source_label) ||
    !stamp(value.observed_at) ||
    !stamp(value.expires_at) ||
    Date.parse(value.observed_at) > now ||
    Date.parse(value.expires_at) <= Date.parse(value.observed_at) ||
    Date.parse(value.expires_at) - Date.parse(value.observed_at) > 300000
  )
    fail();
  for (const [key, max] of [
    ["nodes", 48],
    ["edges", 96],
    ["evidence", 128],
    ["responses", 8],
    ["events", 100],
  ]) {
    if (
      !Array.isArray(value[key]) ||
      value[key].length > max ||
      !value[key].every(object)
    )
      fail();
  }
  if (
    !value.nodes.length ||
    !unique(value.nodes) ||
    !unique(value.edges) ||
    !unique(value.evidence) ||
    !unique(value.events) ||
    !unique([...value.nodes, ...value.edges])
  )
    fail();
  const nodes = new Map(value.nodes.map((n) => [n.id, n]));
  const evidence = new Map(value.evidence.map((e) => [e.id, e]));
  for (const e of value.evidence) {
    exact(
      e,
      "id resource_id resource_uid kind outcome summary observed_at expires_at",
    );
    if (
      !id(e.id) ||
      !id(e.resource_id) ||
      !text(e.resource_uid, 160) ||
      !text(e.summary, 240) ||
      !enumeration(e.kind, [
        "health",
        "security",
        "isolation",
        "replacement",
        "route",
        "recovery",
        "approval",
        "hold",
      ]) ||
      !enumeration(e.outcome, ["verified", "failed", "unknown"]) ||
      !stamp(e.observed_at) ||
      !stamp(e.expires_at) ||
      Date.parse(e.observed_at) > Date.parse(value.observed_at) ||
      Date.parse(e.expires_at) <= Date.parse(e.observed_at) ||
      Date.parse(e.expires_at) > Date.parse(value.expires_at)
    )
      fail();
  }
  const reference = (v) =>
    v === null || (typeof v === "string" && evidence.has(v));
  const proof = (proofId, resource, kind) => {
    const e = evidence.get(proofId);
    return Boolean(
      e &&
        e.resource_id === resource.id &&
        e.resource_uid === resource.resource_uid &&
        e.kind === kind &&
        e.outcome === "verified",
    );
  };
  for (const n of value.nodes) {
    exact(
      n,
      "id label resource_uid detail kind lane column row health security health_evidence security_evidence",
    );
    if (
      !id(n.id) ||
      !text(n.label, 80) ||
      !text(n.resource_uid) ||
      !text(n.detail, 240) ||
      !enumeration(n.kind, [
        "edge",
        "app",
        "worker",
        "database",
        "secret",
        "security",
        "executor",
        "storage",
        "queue",
        "identity",
        "ai",
        "observability",
      ]) ||
      !enumeration(n.lane, ["source", "control", "target"]) ||
      !Number.isInteger(n.column) ||
      n.column < 0 ||
      n.column > 4 ||
      !Number.isInteger(n.row) ||
      n.row < 0 ||
      n.row > 9 ||
      !reference(n.health_evidence) ||
      !reference(n.security_evidence) ||
      !enumeration(n.health, Object.keys(healthLabels)) ||
      !enumeration(n.security, Object.keys(securityLabels)) ||
      (n.health !== "unknown" && !proof(n.health_evidence, n, "health")) ||
      (n.security === "isolated" &&
        !proof(n.security_evidence, n, "isolation")) ||
      (["clear", "warning"].includes(n.security) &&
        !proof(n.security_evidence, n, "security")) ||
      (n.security === "hold" && !proof(n.security_evidence, n, "hold"))
    )
      fail();
  }
  for (const edge of value.edges) {
    exact(edge, "id source target label kind resource_uid state evidence_id");
    if (
      !id(edge.id) ||
      !nodes.has(edge.source) ||
      !nodes.has(edge.target) ||
      edge.source === edge.target ||
      !text(edge.label, 80) ||
      !text(edge.resource_uid) ||
      !enumeration(edge.kind, [
        "http",
        "database",
        "control",
        "dependency",
        "replication",
      ]) ||
      !reference(edge.evidence_id) ||
      !enumeration(edge.state, Object.keys(linkLabels)) ||
      (edge.state === "blocked" &&
        !proof(edge.evidence_id, edge, "isolation")) ||
      (edge.state === "observed" && !proof(edge.evidence_id, edge, "health"))
    )
      fail();
  }
  if (
    new Set(value.responses.map((r) => r.stage)).size !== value.responses.length
  )
    fail();
  const stageKinds = {
    detect: "security",
    hold: "hold",
    approve: "approval",
    isolate: "isolation",
    replace: "replacement",
    verify: "security",
    route: "route",
    recover: "recovery",
  };
  for (const r of value.responses) {
    exact(r, "stage status detail node_ids evidence_id");
    if (
      !STAGES.includes(r.stage) ||
      !enumeration(r.status, Object.keys(responseLabels)) ||
      !text(r.detail, 240) ||
      !Array.isArray(r.node_ids) ||
      r.node_ids.length > 48 ||
      new Set(r.node_ids).size !== r.node_ids.length ||
      !r.node_ids.every((n) => nodes.has(n)) ||
      !reference(r.evidence_id) ||
      (r.status === "verified" &&
        (r.node_ids.length !== 1 ||
          !proof(r.evidence_id, nodes.get(r.node_ids[0]), stageKinds[r.stage])))
    )
      fail();
  }
  let sequence = 0;
  for (const e of value.events) {
    exact(e, "id sequence occurred_at stage status summary");
    if (
      !id(e.id) ||
      !Number.isSafeInteger(e.sequence) ||
      e.sequence <= sequence ||
      !stamp(e.occurred_at) ||
      Date.parse(e.occurred_at) > Date.parse(value.observed_at) ||
      !text(e.summary, 240) ||
      !enumeration(e.status, Object.keys(responseLabels)) ||
      !STAGES.includes(e.stage)
    )
      fail();
    sequence = e.sequence;
  }
  return structuredClone(value);
}

export function projectedWorkload(
  document,
  { connected = true, now = Date.now() } = {},
) {
  if (!document) return null;
  const fresh =
    connected &&
    document.provenance !== "historical" &&
    Date.parse(document.observed_at) <= now &&
    now < Date.parse(document.expires_at);
  const byProof = new Map(document.evidence.map((e) => [e.id, e]));
  const currentProof = (id) =>
    fresh && byProof.has(id) && now < Date.parse(byProof.get(id).expires_at);
  return {
    ...document,
    fresh,
    nodes: document.nodes.map((n) => ({
      ...n,
      health: currentProof(n.health_evidence) ? n.health : "unknown",
      security: currentProof(n.security_evidence) ? n.security : "unknown",
    })),
    edges: document.edges.map((e) => ({
      ...e,
      state:
        e.state === "requested" && fresh
          ? "requested"
          : currentProof(e.evidence_id)
            ? e.state
            : "unknown",
    })),
    responses: document.responses.map((r) => ({
      ...r,
      current:
        fresh && (r.status !== "verified" || currentProof(r.evidence_id)),
    })),
  };
}

export class WorkloadProjection {
  constructor() {
    this.clear();
  }
  clear() {
    this.document = null;
    this.fingerprint = null;
    this.error = null;
  }
  accept(value, plan, now = Date.now()) {
    const next = workloadDocument(value, plan, now);
    const old = this.document;
    const samePlan =
      old &&
      old.plan_id === next.plan_id &&
      old.plan_digest === next.plan_digest &&
      old.plan_version === next.plan_version;
    if (samePlan && old.stream_id !== next.stream_id) {
      this.error = "stream_changed";
      return false;
    }
    if (samePlan && next.revision < old.revision) return false;
    const fingerprint = JSON.stringify(next);
    if (samePlan && next.revision === old.revision) {
      if (this.fingerprint !== fingerprint) {
        this.error = "conflicting_revision";
        return false;
      }
      return true;
    }
    if (
      samePlan &&
      Date.parse(next.observed_at) < Date.parse(old.observed_at)
    ) {
      this.error = "observation_regressed";
      return false;
    }
    this.document = next;
    this.fingerprint = fingerprint;
    this.error = null;
    return true;
  }
  view(connected, now) {
    return projectedWorkload(this.document, {
      connected: connected && !this.error,
      now,
    });
  }
}

export function possibleImpact(graph, selected) {
  if (!graph) return [];
  const ids = new Set(selected);
  // This is dependency reachability, not evidence of compromise or a proposed isolation scope.
  return [
    ...new Set(
      graph.edges
        .filter(
          (e) =>
            ids.has(e.source) && !ids.has(e.target) && e.state !== "blocked",
        )
        .map((e) => e.target),
    ),
  ];
}

export function t10ResponseContext(snapshot, events, connected) {
  if (!snapshot || !connected)
    return { state: "unknown", summary: "플랫폼 연결 미확인", events: [] };
  const hold = ["SAFE_HOLD", "NEEDS_HUMAN"].includes(snapshot.state);
  return {
    state: hold ? "hold" : "observed",
    summary: hold
      ? "T10 계획이 보류 상태입니다. 공격·격리 여부는 별도 근거가 필요합니다."
      : `T10 현재 단계: ${snapshot.state} · 격리 완료를 뜻하지 않습니다.`,
    events: (events || [])
      .filter(
        (e) =>
          e.plan_id === snapshot.plan_id &&
          [
            "state.changed",
            "gate.completed",
            "approval.requested",
            "approval.decided",
            "execution.updated",
            "failback.recorded",
          ].includes(e.event_type),
      )
      .slice(-8)
      .map((e) => ({ id: e.event_id, at: e.occurred_at, type: e.event_type })),
  };
}
