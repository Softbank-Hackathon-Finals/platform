// Browser-only storyboard. No fetch, operational credentials, storage or execution API.
export const scenarioSteps = [
  ["정상 흐름", "클라이언트 요청과 앱·작업자·DB의 관계를 살펴봅니다."],
  [
    "보안 이상",
    "앱과 작업자에서 통제 위반이 확인된 상황을 재생합니다. 주변 강조는 영향 가능성입니다.",
  ],
  ["보안 보류", "일반 변경을 보류하고 대응할 자원 범위를 고정합니다."],
  ["사람 승인", "검토자가 격리 대상과 대체 계획을 승인한 상황입니다."],
  [
    "격리 요청",
    "차단 요청을 접수했습니다. 아직 연결이 끊겼다고 표시하지 않습니다.",
  ],
  ["격리 확인", "대상 앱·작업자의 새 연결 거부 근거를 확인한 상황입니다."],
  ["대체 준비", "검증할 대체 앱이 준비됩니다. 트래픽은 아직 넘기지 않습니다."],
  ["독립 검증", "대체 앱의 응답과 선정한 보안 통제를 확인합니다."],
  ["경로 전환", "대체 경로의 실제 응답을 확인한 뒤 전환을 표시합니다."],
  [
    "서비스 복구 확인",
    "대체 경로의 업무 응답을 확인합니다. 원본 격리와 사고 보류는 유지합니다.",
  ],
];

export function scenarioDocument(
  step = 0,
  { now = Date.now(), variant = "normal" } = {},
) {
  step = Math.max(0, Math.min(9, step));
  const at = new Date(now).toISOString(),
    expiry = new Date(now + 30000).toISOString();
  const doc = {
    kind: "console-workload.v1",
    plan_id: "local-security-story",
    plan_digest: "d".repeat(64),
    plan_version: 1,
    revision: step + 1,
    stream_id: "local-story",
    provenance: "synthetic",
    source_label: "브라우저 안의 합성 시나리오",
    observed_at: at,
    expires_at: expiry,
    authorizes_execution: false,
    nodes: [],
    edges: [],
    evidence: [],
    responses: [],
    events: [],
  };
  function proof(resource, kind, summary) {
    const id = `demo-${resource.id}-${kind}`;
    doc.evidence.push({
      id,
      resource_id: resource.id,
      resource_uid: resource.resource_uid,
      kind,
      outcome: "verified",
      summary: `합성 근거 · ${summary}`,
      observed_at: at,
      expires_at: expiry,
    });
    return id;
  }
  const rows = [
    ["edge", "고정 진입점", "edge", "source", 0, 1, "사용자 요청의 진입 경로"],
    ["api", "업무 API", "app", "source", 1, 1, "보안 대응 대상으로 선택한 앱"],
    [
      "worker",
      "백그라운드 작업자",
      "worker",
      "source",
      2,
      0,
      "업무 API와 연결된 작업 처리 프로세스",
    ],
    [
      "database",
      "업무 데이터베이스",
      "database",
      "source",
      3,
      1,
      "영향 가능성을 살펴볼 의존 자원 · 감염을 뜻하지 않음",
    ],
    [
      "secret",
      "자격·비밀 관리",
      "secret",
      "control",
      4,
      0,
      "등록된 접근 권한과 비밀 참조",
    ],
    [
      "security",
      "보안 통제·실행 기록",
      "security",
      "control",
      4,
      1,
      "정책·승인·격리 확인 근거",
    ],
    [
      "green",
      "대체 업무 API",
      "app",
      "target",
      1,
      0,
      "검증 후에만 사용자 요청을 받는 대체 실행본",
    ],
    [
      "target-db",
      "동일 업무 DB 연결",
      "database",
      "target",
      3,
      0,
      "공유 DB를 계속 쓰는 예시 · DB 이전 완료가 아님",
    ],
  ];
  for (const [id, label, kind, lane, column, row, detail] of rows) {
    const n = {
      id,
      label,
      kind,
      lane,
      column,
      row,
      detail,
      resource_uid: `synthetic-${id}`,
      health: "ready",
      security: "clear",
      health_evidence: null,
      security_evidence: null,
    };
    if (["green", "target-db"].includes(id) && step < 6) {
      n.health = "unknown";
      n.security = "unknown";
    }
    if (["api", "worker"].includes(id) && step >= 1)
      n.security = step >= 5 ? "isolated" : step >= 2 ? "hold" : "warning";
    if (id === "green" && step >= 7 && variant === "verification_failure") {
      n.health = "unavailable";
      n.security = "warning";
    }
    if (n.health !== "unknown")
      n.health_evidence = proof(
        n,
        "health",
        n.health === "ready" ? "대상 응답 확인" : "대체 앱 응답 실패 확인",
      );
    if (n.security !== "unknown")
      n.security_evidence = proof(
        n,
        n.security === "isolated"
          ? "isolation"
          : n.security === "hold"
            ? "hold"
            : "security",
        n.security === "isolated"
          ? "선정 연결의 거부 확인"
          : n.security === "hold"
            ? "현재 보류 기록"
            : n.security === "warning"
              ? "선정 통제 위반"
              : "선정 통제 확인",
      );
    doc.nodes.push(n);
  }
  const routed = step >= 8 && variant !== "verification_failure";
  for (const [id, source, target, label, kind] of [
    ["edge-api", "edge", "api", "HTTPS 요청", "http"],
    ["api-worker", "api", "worker", "작업 전달", "dependency"],
    ["api-db", "api", "database", "DB 읽기·쓰기", "database"],
    ["worker-db", "worker", "database", "작업 결과", "database"],
    ["secret-api", "secret", "api", "자격 참조", "control"],
    ["security-api", "security", "api", "보류·격리 근거", "control"],
    ["edge-green", "edge", "green", "대체 요청 경로", "http"],
    ["green-db", "green", "target-db", "검증 대상 연결", "database"],
  ]) {
    const e = {
      id,
      source,
      target,
      label,
      kind,
      resource_uid: `synthetic-${id}`,
      state: "observed",
      evidence_id: null,
    };
    if (["edge-api", "api-worker", "api-db", "worker-db"].includes(id))
      e.state = step >= 5 ? "blocked" : step === 4 ? "requested" : "observed";
    if (id === "edge-green" && !routed) e.state = "unknown";
    if (id === "green-db" && step < 7) e.state = "unknown";
    if (["observed", "blocked"].includes(e.state))
      e.evidence_id = proof(
        e,
        e.state === "blocked" ? "isolation" : "health",
        e.state === "blocked" ? "선정 새 연결 거부" : "해당 경로 응답",
      );
    doc.edges.push(e);
  }
  const stages = [
    ["detect", 1, "security"],
    ["hold", 2, "hold"],
    ["approve", 3, "approval"],
    ["isolate", 5, "isolation"],
    ["replace", 6, "replacement"],
    ["verify", 7, "security"],
    ["route", 8, "route"],
    ["recover", 9, "recovery"],
  ];
  for (const [stage, threshold, kind] of stages) {
    let status = step >= threshold ? "verified" : "pending";
    if (stage === "isolate" && step === 4) status = "requested";
    if (
      variant === "verification_failure" &&
      step >= 7 &&
      ["verify", "route", "recover"].includes(stage)
    )
      status = stage === "verify" ? "failed" : "pending";
    const resource = doc.nodes.find(
      (n) =>
        n.id ===
        (["replace", "verify", "route", "recover"].includes(stage)
          ? "green"
          : "api"),
    );
    let evidence_id = null;
    if (status === "verified") {
      evidence_id = `demo-stage-${stage}`;
      doc.evidence.push({
        id: evidence_id,
        resource_id: resource.id,
        resource_uid: resource.resource_uid,
        kind,
        outcome: "verified",
        summary: `합성 근거 · ${stage} 확인`,
        observed_at: at,
        expires_at: expiry,
      });
    }
    doc.responses.push({
      stage,
      status,
      node_ids: [resource.id],
      evidence_id,
      detail:
        status === "pending"
          ? "선행 확인 대기"
          : status === "failed"
            ? "검증 실패 · 원본 격리 유지, 경로 전환 보류"
            : stage === "recover"
              ? "서비스 복구 확인 · 사고 보류는 유지"
              : status === "requested"
                ? "요청만 접수 · 격리 완료 아님"
                : "합성 시나리오에서 확인한 단계",
    });
    if (status !== "pending")
      doc.events.push({
        id: `demo-event-${stage}`,
        sequence: doc.events.length + 1,
        occurred_at: at,
        stage,
        status,
        summary: doc.responses.at(-1).detail,
      });
  }
  return doc;
}
