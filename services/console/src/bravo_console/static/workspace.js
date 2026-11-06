// Sova's workspace presentation, projected from T21's validated read model.
// No API client, action eligibility, workflow state or persistent storage here.
export const VIEWS = {
  network: [
    "네트워크 아틀라스",
    "현재 작업 화면에서 환경·리소스·전송 경로와 수집 근거를 확인합니다.",
  ],
  analysis: [
    "의존성·후보 비교",
    "등록된 분석과 추천, 출발지에 남은 의존성을 확인합니다.",
  ],
  ai: [
    "AI·리허설",
    "AI 제안 작업과 리허설 실행·독립 검증 결과를 구분해 확인합니다.",
  ],
  costs: [
    "비용·예산",
    "집계 범위·귀속 여부·환율 기준을 확인합니다. 미관측을 0원으로 표시하지 않습니다.",
  ],
  overview: [
    "워크스페이스",
    "이전 계획과 현재 확인된 근거를 한곳에서 확인합니다.",
  ],
  migration: [
    "환경 이전",
    "등록된 경로와 진행 단계, 검토할 승인 대상을 확인합니다.",
  ],
  topology: [
    "워크로드 지도",
    "계획에 등록된 앱과 환경을 표시합니다. 실제 구성 요소의 연결 정보는 별도 관측이 필요합니다.",
  ],
  security: [
    "보안·거버넌스",
    "양쪽 환경의 통제 시험과 탐지 수집 상태를 비교합니다.",
  ],
  monitoring: [
    "모니터링",
    "현재 수신한 지표와 판정 근거를 구분해서 확인합니다.",
  ],
  timeline: [
    "실행 타임라인",
    "서버에 기록된 실행·승인·복귀 이력을 확인합니다.",
  ],
};

export function workspaceView(snapshot, observed, connected, snapshotAt, now) {
  const current = Boolean(
    snapshot && connected && now >= snapshotAt && now - snapshotAt < 15000,
  );
  const metricsCurrent =
    current && observed && now >= observed.at && now - observed.at < 15000;
  const side = (name) => {
    const row = metricsCurrent
      ? observed.rows.find((value) => value.side === name)
      : null;
    return {
      id: snapshot?.[name + "_environment_id"] ?? "등록된 환경 없음",
      observed: Boolean(row?.state === "OK"),
      detail:
        row?.state === "OK"
          ? `오류율 ${row.error_ratio === null ? "미확인" : (row.error_ratio * 100).toFixed(2) + "%"} · p95 ${row.p95_seconds === null ? "미확인" : (row.p95_seconds * 1000).toFixed(0) + " ms"}`
          : "현재 계획에 연결된 최신 HTTP 관측이 없습니다.",
    };
  };
  return {
    source: side("source"),
    target: side("target"),
    workload: snapshot?.workload_id ?? "등록된 앱 없음",
    current,
  };
}

export function mountWorkspace(root = document) {
  const byId = (id) => root.getElementById(id);
  let selected = "source";
  let view = workspaceView(null, null, false, 0, Date.now());
  const labels = {
    source: "출발 환경",
    workload: "이전 대상 앱",
    target: "목적 환경",
  };
  function inspect() {
    for (const button of root.querySelectorAll("[data-map-node]")) {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.mapNode === selected),
      );
    }
    byId("inspector-title").textContent = labels[selected];
    byId("inspector-id").textContent =
      selected === "workload" ? view.workload : view[selected].id;
    byId("inspector-state").textContent =
      selected === "workload"
        ? "계획에 등록된 앱 · 실행 위치 미판정"
        : view[selected].observed
          ? "HTTP 관측 수신 · 업무 정상 여부 별도 확인"
          : "실행 상태 미확인";
    byId("inspector-detail").textContent =
      selected === "workload"
        ? "앱 내부 프로세스·DB·의존성 연결 정보는 아직 이 지도에 연결되지 않았습니다."
        : view[selected].detail;
  }
  for (const button of root.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => {
      const key = button.dataset.view;
      if (!Object.hasOwn(VIEWS, key)) return;
      const [title, description] = VIEWS[key];
      for (const item of root.querySelectorAll("[data-view]")) {
        item.setAttribute("aria-current", item === button ? "page" : "false");
      }
      for (const panel of root.querySelectorAll("[data-views]")) {
        panel.hidden =
          key !== "overview" && !panel.dataset.views.split(" ").includes(key);
      }
      byId("view-title").textContent = title;
      byId("view-description").textContent = description;
      byId("breadcrumb-view").textContent = title;
      byId("view-title").focus({ preventScroll: true });
      root.defaultView?.dispatchEvent(
        new CustomEvent("sova-view-changed", { detail: { view: key } }),
      );
      root.defaultView?.scrollTo(0, 0);
    });
  }
  for (const button of root.querySelectorAll("[data-map-node]")) {
    button.addEventListener("click", () => {
      selected = button.dataset.mapNode;
      inspect();
    });
  }
  inspect();
  return {
    render({ snapshot, observed, connected, snapshotAt, now }) {
      view = workspaceView(snapshot, observed, connected, snapshotAt, now);
      byId("map-workload-id").textContent = view.workload;
      for (const name of ["source", "target"]) {
        byId("map-" + name + "-id").textContent = view[name].id;
        byId("map-" + name + "-state").textContent = view[name].observed
          ? "HTTP 관측 수신"
          : "실행 상태 미확인";
        byId("map-" + name).classList.toggle("observed", view[name].observed);
      }
      byId("map-freshness").textContent = view.current
        ? "현재 계획 조회됨"
        : "연결 대기·조회 만료";
      inspect();
    },
  };
}
