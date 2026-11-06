import { controlReportFresh, controlReportMatches } from "./model.js";
import { workloadLayout } from "./workload-layout.js";
import { request } from "./transport.js";
import {
  WorkloadProjection,
  projectedWorkload,
  possibleImpact,
  stageLabels,
  healthLabels,
  securityLabels,
  linkLabels,
  responseLabels,
  t10ResponseContext,
} from "./workload-model.js";
import { scenarioDocument, scenarioSteps } from "./workload-scenario.js";

const SVG = "http://www.w3.org/2000/svg";
const icons = {
  edge: "↗",
  app: "▣",
  worker: "⚙",
  database: "▤",
  secret: "◇",
  security: "⬡",
  executor: "▷",
  storage: "▥",
  queue: "≋",
  identity: "◈",
  ai: "✧",
  observability: "◉",
};
const el = (name, className, text) => {
  const n = document.createElement(name);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  return n;
};
const svg = (name, attrs = {}, text) => {
  const n = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  if (text !== undefined) n.textContent = text;
  return n;
};
const time = (v) =>
  Number.isFinite(Date.parse(v))
    ? new Date(v).toLocaleTimeString("ko-KR", { hour12: false })
    : "미확인";

export function mountWorkload(
  { getModel, getControls = () => null, onForbidden = () => {} },
  root = document,
) {
  const host = root.getElementById("response-workspace");
  if (!host) return { render() {}, clear() {}, destroy() {} };
  let mode = "live",
    step = 0,
    variant = "normal",
    playing = false,
    timer = null,
    selected = new Set(),
    live = new WorkloadProjection();
  let busy = false,
    generation = 0,
    binding = null,
    lastRead = 0,
    readError = null,
    renderedKey = null,
    lastGraph = null,
    controller = null;
  let localObserved = null;
  const title = el("div", "response-heading");
  const text = el("div");
  text.append(
    el("p", "eyebrow", "SECURITY RESPONSE MAP"),
    el("h3", null, "워크로드와 보안 대응"),
  );
  const tabs = el("div", "response-modes");
  tabs.setAttribute("role", "group");
  tabs.setAttribute("aria-label", "관측 출처 선택");
  const liveButton = el("button", "response-mode", "플랫폼 관측"),
    demoButton = el("button", "response-mode", "로컬 시나리오");
  for (const b of [liveButton, demoButton]) b.type = "button";
  tabs.append(liveButton, demoButton);
  title.append(text, tabs);
  const reconnect = el("button", "response-reconnect", "관측 다시 연결");
  reconnect.type = "button";
  reconnect.addEventListener("click", () => {
    generation++;
    controller?.abort();
    live.clear();
    readError = null;
    lastRead = 0;
    selected.clear();
    renderedKey = null;
    render();
    refresh();
  });
  tabs.append(reconnect);
  const banner = el("div", "response-banner");
  banner.setAttribute("role", "status");
  const toolbar = el("div", "response-toolbar");
  const variantSelect = el("select");
  variantSelect.setAttribute("aria-label", "로컬 시나리오 종류");
  for (const [v, label] of [
    ["normal", "격리 후 대체"],
    ["verification_failure", "대체 검증 실패"],
  ]) {
    const o = el("option", null, label);
    o.value = v;
    variantSelect.append(o);
  }
  const play = el("button", null, "자동 재생"),
    previous = el("button", null, "이전"),
    next = el("button", null, "다음 단계"),
    reset = el("button", null, "처음부터"),
    expire = el("button", null, "근거 만료 보기");
  for (const b of [play, previous, next, reset, expire]) b.type = "button";
  const stepTitle = el("strong", "response-step-title");
  toolbar.append(variantSelect, previous, play, next, reset, expire, stepTitle);
  const description = el("p", "response-description");
  const main = el("div", "response-grid"),
    canvas = el("div", "response-canvas"),
    inspector = el("aside", "response-inspector");
  inspector.setAttribute("aria-label", "선택한 워크로드 근거");
  const graph = svg("svg", {
    viewBox: "0 0 1000 600",
    role: "group",
    "aria-label": "워크로드 관계와 현재 보안 상태",
  });
  canvas.append(graph);
  const legend = el("div", "response-legend");
  for (const [klass, label] of [
    ["observed", "연결 관측"],
    ["warning", "보안 이상"],
    ["impact", "영향 가능"],
    ["blocked", "차단 확인"],
    ["unknown", "미확인·만료"],
  ]) {
    const l = el("span", klass);
    l.append(el("i"), document.createTextNode(label));
    legend.append(l);
  }
  canvas.append(
    legend,
    el(
      "p",
      "response-footnote",
      "선은 등록된 관계입니다. 움직임은 관측 상태의 표현이며 실제 패킷·트래픽 양을 나타내지 않습니다.",
    ),
  );
  main.append(canvas, inspector);
  const flow = el("ol", "response-flow");
  flow.setAttribute("aria-label", "보안 대응 단계");
  const bottom = el("div", "response-bottom"),
    eventBox = el("div"),
    timeline = el("ol", "response-events"),
    context = el("div", "response-context");
  eventBox.append(el("h4", null, "대응 기록"), timeline);
  bottom.append(eventBox, context);
  host.append(title, banner, toolbar, description, main, flow, bottom);
  function stop() {
    playing = false;
    clearInterval(timer);
    timer = null;
    play.textContent = "자동 재생";
  }
  function newLocal() {
    localObserved = Date.now();
  }
  function switchMode(value) {
    stop();
    mode = value;
    selected.clear();
    renderedKey = null;
    if (value === "demo") newLocal();
    render();
  }
  liveButton.addEventListener("click", () => switchMode("live"));
  demoButton.addEventListener("click", () => switchMode("demo"));
  next.addEventListener("click", () => {
    stop();
    step = Math.min(9, step + 1);
    localObserved = Date.now();
    render();
  });
  previous.addEventListener("click", () => {
    stop();
    step = Math.max(0, step - 1);
    localObserved = Date.now();
    render();
  });
  reset.addEventListener("click", () => {
    stop();
    step = 0;
    newLocal();
    selected.clear();
    render();
  });
  expire.addEventListener("click", () => {
    stop();
    localObserved = Date.now() - 31000;
    render();
  });
  variantSelect.addEventListener("change", () => {
    stop();
    variant = variantSelect.value;
    step = 0;
    newLocal();
    render();
  });
  play.addEventListener("click", () => {
    if (playing) {
      stop();
      return;
    }
    if (step === 9) step = 0;
    playing = true;
    play.textContent = "일시 정지";
    localObserved = Date.now();
    render();
    timer = setInterval(() => {
      step++;
      localObserved = Date.now();
      if (step >= 9 || (variant === "verification_failure" && step >= 7))
        stop();
      render();
    }, 2600);
  });
  function select(id, multi = false) {
    if (!multi) selected = new Set([id]);
    else if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    renderedKey = null;
    render();
  }
  function draw(view) {
    const focused = graph.contains(document.activeElement)
      ? document.activeElement.getAttribute("data-resource")
      : null;
    graph.replaceChildren();
    if (!view) {
      graph.setAttribute("viewBox", "0 0 1000 600");
      graph.append(
        svg("rect", {
          x: 20,
          y: 30,
          width: 960,
          height: 535,
          rx: 24,
          class: "response-empty-bg",
        }),
        svg(
          "text",
          {
            x: 500,
            y: 250,
            "text-anchor": "middle",
            class: "response-empty-title",
          },
          "실제 워크로드 관측 연결 대기",
        ),
        svg(
          "text",
          {
            x: 500,
            y: 290,
            "text-anchor": "middle",
            class: "response-empty-detail",
          },
          "등록된 앱의 자원·연결·근거가 제공되면 여기에 표시됩니다.",
        ),
        svg(
          "text",
          {
            x: 500,
            y: 325,
            "text-anchor": "middle",
            class: "response-empty-detail",
          },
          "로컬 시나리오에서 격리·대체 과정을 먼저 살펴볼 수 있습니다.",
        ),
      );
      return;
    }
    const defs = svg("defs");
    for (const kind of ["observed", "blocked", "unknown", "requested"]) {
      const marker = svg("marker", {
        id: `response-arrow-${kind}`,
        viewBox: "0 0 10 10",
        refX: 8,
        refY: 5,
        markerWidth: 6,
        markerHeight: 6,
        orient: "auto-start-reverse",
      });
      marker.append(
        svg("path", { d: "M 0 0 L 10 5 L 0 10 z", class: `arrow-${kind}` }),
      );
      defs.append(marker);
    }
    graph.append(defs);
    const layout = workloadLayout(view.nodes);
    graph.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    for (const [x, y, w, h, label] of layout.lanes)
      graph.append(
        svg("rect", {
          x,
          y,
          width: w,
          height: h,
          rx: 18,
          class: "response-lane",
        }),
        svg(
          "text",
          { x: x + 18, y: y + 27, class: "response-lane-label" },
          label,
        ),
      );
    const positions = layout.positions;
    const impact = new Set(
      possibleImpact(
        view,
        [...selected].filter((id) =>
          ["warning", "hold", "isolated"].includes(
            view.nodes.find((n) => n.id === id)?.security,
          ),
        ),
      ),
    );
    for (const e of view.edges) {
      const a = positions.get(e.source),
        b = positions.get(e.target);
      let d;
      if (a.y === b.y)
        d = `M ${a.x + 67} ${a.y} C ${a.x + 105} ${a.y}, ${b.x - 105} ${b.y}, ${b.x - 67} ${b.y}`;
      else
        d = `M ${a.x} ${a.y + 40} C ${a.x} ${(a.y + b.y) / 2}, ${b.x} ${(a.y + b.y) / 2}, ${b.x} ${b.y - 40}`;
      const g = svg("g", {
        class: `response-link ${e.state}`,
        "data-link": e.id,
      });
      g.append(
        svg("title", {}, `${e.label} · ${linkLabels[e.state]}`),
        svg("path", { d, "marker-end": `url(#response-arrow-${e.state})` }),
      );
      if (e.state === "blocked")
        g.append(
          svg(
            "text",
            {
              x: (a.x + b.x) / 2,
              y: (a.y + b.y) / 2 - 5,
              class: "response-cut",
              "text-anchor": "middle",
            },
            "×",
          ),
        );
      graph.append(g);
    }
    for (const n of view.nodes) {
      const p = positions.get(n.id);
      const g = svg("g", {
        transform: `translate(${p.x},${p.y})`,
        class: `response-node health-${n.health} security-${n.security}${selected.has(n.id) ? " selected" : ""}${impact.has(n.id) ? " possible-impact" : ""}`,
        role: "button",
        tabindex: 0,
        "aria-label": `${n.label} · ${healthLabels[n.health]} · ${securityLabels[n.security]}`,
        "aria-pressed": selected.has(n.id),
        "data-resource": n.id,
      });
      g.append(
        svg("rect", { x: -71, y: -42, width: 142, height: 84, rx: 12 }),
        svg(
          "text",
          { x: -51, y: -15, class: "response-node-icon" },
          icons[n.kind],
        ),
        svg(
          "text",
          { x: -30, y: -15, class: "response-node-label" },
          n.label.length > 10 ? n.label.slice(0, 10) + "…" : n.label,
        ),
        svg(
          "text",
          { x: -56, y: 8, class: "response-node-state" },
          n.security !== "clear" && n.security !== "unknown"
            ? securityLabels[n.security]
            : healthLabels[n.health],
        ),
        svg(
          "text",
          { x: -56, y: 28, class: "response-node-kind" },
          impact.has(n.id) ? "영향 가능 · 미확정" : n.kind.toUpperCase(),
        ),
      );
      g.addEventListener("click", (e) => select(n.id, e.shiftKey));
      g.addEventListener("keydown", (e) => {
        if (["Enter", " "].includes(e.key)) {
          e.preventDefault();
          select(n.id, e.shiftKey);
        }
      });
      graph.append(g);
      if (n.id === focused) g.focus({ preventScroll: true });
    }
  }
  function details(view) {
    inspector.replaceChildren();
    inspector.append(el("p", "eyebrow", "RESOURCE EVIDENCE"));
    const n = view?.nodes.find((n) => selected.has(n.id));
    if (!n) {
      inspector.append(
        el("h4", null, "구성 요소를 선택하세요"),
        el(
          "p",
          null,
          "노드를 누르면 상태와 근거를 확인합니다. Shift를 누른 채 여러 노드를 선택하면 연결 관계를 함께 살펴볼 수 있습니다.",
        ),
      );
      return;
    }
    inspector.append(
      el("h4", null, n.label),
      el("p", "response-resource-id", n.id),
      el("span", `response-status ${n.security}`, securityLabels[n.security]),
      el("p", null, n.detail),
    );
    const list = el("dl");
    for (const [k, v] of [
      ["현재 응답", healthLabels[n.health]],
      [
        "관측 출처",
        view.provenance === "synthetic"
          ? "합성 시나리오"
          : view.provenance === "historical"
            ? "과거 관측"
            : "등록된 플랫폼 공급자",
      ],
      ["마지막 관측", time(view.observed_at)],
      ["자원 식별자", n.resource_uid],
      ["선택한 자원", `${selected.size}개`],
    ])
      list.append(el("dt", null, k), el("dd", null, v));
    inspector.append(list);
    const proofs = view.evidence.filter(
      (e) => e.resource_id === n.id && e.resource_uid === n.resource_uid,
    );
    inspector.append(el("h5", null, "관측 근거"));
    if (!proofs.length) inspector.append(el("p", "muted", "등록된 근거 없음"));
    for (const e of proofs) {
      const box = el("div", "response-proof");
      box.append(
        el("strong", null, e.id),
        el("p", null, e.summary),
        el(
          "small",
          null,
          `${time(e.observed_at)} · ${view.fresh && Date.now() < Date.parse(e.expires_at) ? "유효한 관측" : "과거 근거 · 현재 확인 필요"}`,
        ),
      );
      inspector.append(box);
    }
    const edges = view.edges.filter(
      (e) => e.source === n.id || e.target === n.id,
    );
    inspector.append(el("h5", null, "연결 상태"));
    for (const e of edges)
      inspector.append(
        el("p", "response-link-detail", `${e.label} · ${linkLabels[e.state]}`),
      );
    inspector.append(
      el(
        "p",
        "response-boundary",
        "이 지도는 조회 화면입니다. 노드 선택이나 시나리오 재생으로 운영 자원이 변경되지 않습니다.",
      ),
    );
  }
  function render() {
    const model = getModel(),
      now = Date.now();
    if (
      mode === "live" &&
      live.document &&
      (!model?.snapshot ||
        live.document.plan_id !== model.snapshot.plan_id ||
        live.document.plan_digest !== model.snapshot.plan_digest ||
        live.document.plan_version !== model.snapshot.version)
    ) {
      live.clear();
      selected.clear();
      renderedKey = null;
    }
    const freshConnection = Boolean(
      model?.connected &&
        model.snapshot &&
        now >= model.snapshotAt &&
        now - model.snapshotAt < 15000 &&
        !model.error,
    );
    const view =
      mode === "demo"
        ? projectedWorkload(
            scenarioDocument(step, { now: localObserved ?? now, variant }),
            { now },
          )
        : live.view(freshConnection, now);
    lastGraph = view;
    liveButton.setAttribute("aria-pressed", String(mode === "live"));
    demoButton.setAttribute("aria-pressed", String(mode === "demo"));
    toolbar.hidden = mode !== "demo";
    reconnect.hidden = mode !== "live";
    banner.className = `response-banner ${mode === "demo" ? "synthetic" : view?.fresh ? "current" : "unavailable"}`;
    banner.textContent =
      mode === "demo"
        ? `로컬 합성 시나리오 · 실제 공격·격리·트래픽 변경 없음${view?.fresh ? "" : " · 근거 만료: 현재 상태 미확인"}`
        : view
          ? `${view.provenance === "live" ? "플랫폼 관측" : view.provenance === "historical" ? "과거 플랫폼 관측" : "서버 합성 시험"} · ${view.source_label} · ${time(view.observed_at)}${view.fresh ? "" : " · 연결 끊김 또는 근거 만료"}`
          : "플랫폼 워크로드 공급자 미연결 · 실환경 격리·복구 완료를 표시하지 않습니다.";
    if (mode === "live" && readError)
      banner.textContent += " · 조회 실패 또는 재동기화 필요";
    stepTitle.textContent = `${step + 1} / ${scenarioSteps.length} · ${scenarioSteps[step][0]}`;
    description.textContent =
      mode === "demo"
        ? scenarioSteps[step][1]
        : "관측된 관계와 증거가 있는 상태만 표시합니다. 주변 영향 강조는 공격 확산을 확정하는 표시가 아닙니다.";
    previous.disabled = step === 0;
    next.disabled = step === 9;
    const key = JSON.stringify([mode, view, [...selected]]);
    if (key !== renderedKey) {
      renderedKey = key;
      draw(view);
      details(view);
      flow.replaceChildren();
      timeline.replaceChildren();
      for (const [stage, label] of Object.entries(stageLabels)) {
        const r = view?.responses.find((r) => r.stage === stage);
        const item = el(
          "li",
          `response-phase ${r?.current ? r.status : "unknown"}`,
        );
        item.append(
          el("span", "phase-dot"),
          el("strong", null, label),
          el(
            "small",
            null,
            r
              ? r.current
                ? responseLabels[r.status]
                : "과거 기록·현재 미확인"
              : "미연결",
          ),
        );
        item.title = r?.detail ?? "대응 기록 공급자 미연결";
        flow.append(item);
      }
      for (const e of (view?.events ?? []).slice(-10).reverse()) {
        const item = el("li", e.status);
        item.append(
          el("time", null, time(e.occurred_at)),
          el("strong", null, stageLabels[e.stage]),
          el("span", null, e.summary),
        );
        timeline.append(item);
      }
      if (!view?.events.length)
        timeline.append(el("li", "muted", "확인된 대응 기록이 없습니다."));
    }
    context.replaceChildren();
    const t10 = t10ResponseContext(
      model?.snapshot,
      model?.events,
      freshConnection,
    );
    context.append(
      el("h4", null, "플랫폼 T10·T19 기록"),
      el("p", null, t10.summary),
    );
    const report = getControls()?.report;
    const currentReport =
      freshConnection &&
      controlReportFresh(report, now) &&
      controlReportMatches(report, model.snapshot, model.gate);
    context.append(
      el(
        "p",
        "response-gate-record",
        currentReport
          ? `T19 ${report.profile.id} · ${report.decision} · 선정 통제의 비교 결과이며 워크로드 격리 완료를 뜻하지 않습니다.`
          : "T19 현재 계획의 검증된 보고서 미연결·만료",
      ),
    );
    if (mode === "demo")
      context.append(
        el(
          "p",
          "response-boundary",
          "왼쪽 시나리오와 실제 계획의 T10 기록은 서로 별개입니다. 합성 결과를 승인·판정 근거로 보내지 않습니다.",
        ),
      );
    for (const e of t10.events.slice(-4))
      context.append(
        el(
          "p",
          "response-platform-event",
          `${time(e.at)} · ${e.type} · ${e.id}`,
        ),
      );
  }
  async function refresh() {
    const model = getModel();
    const nextBinding = model?.snapshot
      ? `${model.plan}/${model.snapshot.plan_digest}/${model.snapshot.version}`
      : null;
    if (binding !== nextBinding) {
      binding = nextBinding;
      generation++;
      controller?.abort();
      live.clear();
      readError = null;
      lastRead = 0;
      selected.clear();
      renderedKey = null;
    }
    if (
      busy ||
      !model?.snapshot ||
      !model.connected ||
      model.error ||
      Date.now() - lastRead < 5000
    ) {
      render();
      return;
    }
    const ticket = generation,
      plan = { ...model.snapshot };
    busy = true;
    lastRead = Date.now();
    controller = new AbortController();
    const activeController = controller;
    const timeout = setTimeout(() => activeController.abort(), 6000);
    try {
      const value = await request(`/v1/plans/${model.plan}/workload`, {
        signal: activeController.signal,
      });
      if (ticket !== generation) return;
      live.accept(value, plan, Date.now());
      readError = live.error;
    } catch (error) {
      if (ticket !== generation) return;
      readError = error.code ?? "workload_unavailable";
      live.error = readError;
      if ([401, 403].includes(error.status)) {
        clear();
        onForbidden(error);
      }
    } finally {
      clearTimeout(timeout);
      busy = false;
      render();
    }
  }
  function clear() {
    generation++;
    controller?.abort();
    live.clear();
    binding = null;
    readError = null;
    selected.clear();
    renderedKey = null;
    stop();
    mode = "live";
    render();
  }
  const poll = setInterval(refresh, 1000);
  render();
  return {
    render,
    clear,
    refresh,
    destroy() {
      clearInterval(poll);
      stop();
      controller?.abort();
    },
    get graph() {
      return lastGraph;
    },
  };
}
