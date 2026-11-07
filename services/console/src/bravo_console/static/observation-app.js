import { createWorkloadRenderer } from "./workload-motion.js";
import { observedTopology } from "./observation-model.js";
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
let selected = new Set(),
  current = null,
  raw = null,
  busy = false,
  stopped = false;
for (const button of document.querySelectorAll("nav button")) {
  button.disabled = button.dataset.view !== "topology";
  button.setAttribute(
    "aria-current",
    button.dataset.view === "topology" ? "page" : "false",
  );
}
for (const control of document.querySelectorAll(
  "form input, form button, form select",
))
  control.disabled = true;
$("breadcrumb-view").textContent = "워크로드 지도";
$("view-title").textContent = "실제 워크로드 관측";
$("view-description").textContent =
  "등록된 클러스터의 Service·Pod·컨테이너 관계를 읽습니다. 운영 계획·승인·실행은 연결하지 않습니다.";
document.querySelector(".scope-pill").textContent = "읽기 전용 · 변경 불가";
document.querySelector(".console-label").textContent = "실제 관측";
document.querySelector(".sidebar-bottom").textContent =
  "읽기 전용 · 실행 권한 없음";
document.querySelector(".workspace-label small").textContent =
  "실클러스터 관측 전용";
const host = $("response-workspace");
const banner = el(
  "div",
  "response-banner",
  "실클러스터 GET 관측입니다. 준비 상태와 HTTP 탐침을 구분하며 공격·격리·이전 성공을 추정하지 않습니다.",
);
const controls = el("div", "response-toolbar");
const refresh = el("button", null, "관측 새로고침");
refresh.type = "button";
refresh.onclick = () => read();
const expand = el("button", null, "지도 확대");
expand.type = "button";
expand.onclick = () => {
  host.classList.toggle("response-expanded");
  expand.textContent = host.classList.contains("response-expanded")
    ? "확대 닫기"
    : "지도 확대";
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    host.classList.remove("response-expanded");
    expand.textContent = "지도 확대";
  }
});
controls.append(refresh, expand);
const environments = el("div", "observed-environments");
const grid = el("div", "response-grid"),
  canvas = el("div", "response-canvas"),
  inspect = el("aside", "response-inspector");
const graph = document.createElementNS("http://www.w3.org/2000/svg", "svg");
graph.setAttribute("role", "img");
graph.setAttribute("aria-label", "실클러스터 워크로드 관계");
canvas.append(graph);
grid.append(canvas, inspect);
const legend = el(
  "p",
  "response-description",
  "회색 선: selector·소속·설정 참조. 파란 선: 최근 API 프록시 HTTP 탐침. 실제 사용자 트래픽·복제·격리 상태는 별도 근거가 필요합니다.",
);
host.replaceChildren(banner, controls, environments, grid, legend);
const renderer = createWorkloadRenderer(graph, {
  onSelect: (id, multi) => {
    selected = multi ? new Set([...selected, id]) : new Set([id]);
    draw();
  },
});
function draw() {
  host.dataset.observationRevision = current ? String(current.revision) : "";
  host.dataset.observationSource = current?.source_revision ?? "";
  environments.replaceChildren();
  for (const env of current?.environments ?? [])
    environments.append(
      el(
        "div",
        "observed-environment",
        `${env.lane === "source" ? "AWS 출발지" : "온프렘 목적지"} · ${env.status === "CURRENT" ? "현재 관측" : env.status === "STALE" ? "관측 만료·실패" : "미연결"} · Service ${env.observed_service_count ?? "—"}/${env.registered_service_count} · ${env.observed_at ? new Date(env.observed_at).toLocaleTimeString("ko-KR", { hour12: false }) : "수집 대기"}`,
      ),
    );
  renderer.draw(current?.view ?? null, { selected });
  const laneLabels = {
    "출발 환경": "AWS 출발지",
    "대체 실행 환경": "온프렘 목적지",
    "격리 근거 영역 · 논리적 분리": "",
    "보호·제어": "관리 HTTP 관측",
  };
  for (const label of graph.querySelectorAll(".response-lane-label"))
    label.textContent = laneLabels[label.textContent] ?? label.textContent;
  for (const zone of graph.querySelectorAll(".response-quarantine-zone"))
    zone.remove();
  if (current?.view)
    for (const node of current.view.nodes) {
      const facet = current.facets[node.id];
      const element = graph.querySelector(`[data-resource="${node.id}"]`);
      if (element && node.health !== "ready") {
        const laneFresh = current.environments.some(
          (e) => e.lane === node.lane && e.status === "CURRENT",
        );
        const label =
          laneFresh && facet.ready !== null
            ? facet.ready
              ? "K8s 준비 확인"
              : "K8s 준비 미완료"
            : "현재 준비 미확인";
        element.querySelector(".response-node-state").textContent = label;
        element.setAttribute(
          "data-readiness",
          laneFresh ? String(facet.ready) : "null",
        );
        element.setAttribute(
          "aria-label",
          `${node.label} · ${label} · 보안 통제 미확인`,
        );
      }
    }
  inspect.replaceChildren(el("h3", null, "자원과 관측 근거"));
  const node = current?.view?.nodes.find((n) => selected.has(n.id));
  if (node) {
    inspect.append(
      el("strong", null, node.label),
      el("p", null, node.detail),
      el("p", null, current.facets[node.id].summary),
    );
    for (const proof of current.view.evidence.filter(
      (e) => e.resource_id === node.id,
    ))
      inspect.append(el("p", null, proof.summary));
  } else
    inspect.append(
      el(
        "p",
        null,
        "자원을 선택하면 실제 관측한 준비 상태와 근거 범위를 확인할 수 있습니다.",
      ),
    );
}
async function read() {
  if (busy || stopped) return;
  busy = true;
  refresh.disabled = true;
  const controller = new AbortController(),
    timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("/v1/observed-workload", {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok)
      throw Error(response.status === 403 ? "session" : "unavailable");
    const received = await response.json();
    const next = observedTopology(received);
    if (
      current &&
      current.stream_id === next.stream_id &&
      next.revision < current.revision
    )
      throw Error("old_revision");
    if (current && current.stream_id !== next.stream_id) {
      selected.clear();
      renderer.reset();
    }
    raw = received;
    current = next;
    draw();
    $("connection").textContent = next.environments.some(
      (e) => e.status === "CURRENT",
    )
      ? "실제 GET 관측 연결"
      : "현재 관측 대기";
    $("map-freshness").textContent =
      `현재 관측 ${next.environments.filter((e) => e.status === "CURRENT").length}/${next.environments.length} 환경`;
    $("notice").textContent =
      "관측 전용 모드입니다. 운영 계획·승인·게이트·격리·컷오버 API는 제공하지 않습니다.";
  } catch (error) {
    raw = null;
    current = null;
    selected.clear();
    renderer.reset();
    draw();
    $("connection").textContent = "관측 조회 불가";
    $("map-freshness").textContent = "관측 철회";
    $("notice").textContent =
      error.message === "session"
        ? "운영자 세션이 없거나 만료됐습니다. 비공개 접속 주소를 다시 사용해 주세요."
        : "현재 자료를 확인할 수 없어 기존 상태를 철회했습니다.";
  } finally {
    clearTimeout(timer);
    busy = false;
    refresh.disabled = false;
  }
}
async function bootstrap() {
  const token = location.hash.slice(1);
  history.replaceState(null, "", "/console/");
  if (token) {
    try {
      await fetch("/v1/observation/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Bravo-Console": "1" },
        credentials: "same-origin",
        body: JSON.stringify({ bootstrap: token }),
      });
    } catch {}
  }
  await read();
}
bootstrap();
setInterval(read, 5000);
setInterval(() => {
  if (current) {
    try {
      current = observedTopology(raw);
      draw();
    } catch {
      current = null;
      draw();
    }
  }
}, 1000);
window.addEventListener("pagehide", () => {
  stopped = true;
  renderer.reset();
});
