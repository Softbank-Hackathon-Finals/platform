// SVG motion is presentation over already validated evidence. It never executes
// an effect, changes a resource UID or promotes an unavailable claim to success.
import { workloadLayout } from "./workload-layout.js";
import {
  possibleImpact,
  healthLabels,
  securityLabels,
  linkLabels,
} from "./workload-model.js";

const NS = "http://www.w3.org/2000/svg";
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
const make = (tag, attrs = {}, text) => {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs))
    node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
};
const mix = (a, b, t) => ({
  x: a.x + (b.x - a.x) * t,
  y: a.y + (b.y - a.y) * t,
});
function split(points, t) {
  const a = mix(points[0], points[1], t),
    b = mix(points[1], points[2], t),
    c = mix(points[2], points[3], t);
  const d = mix(a, b, t),
    e = mix(b, c, t),
    f = mix(d, e, t);
  return [
    [points[0], a, d, f],
    [f, e, c, points[3]],
  ];
}
const number = (value) => Math.round(value * 100) / 100;
const path = ([a, b, c, d]) =>
  `M ${number(a.x)} ${number(a.y)} C ${number(b.x)} ${number(b.y)}, ${number(c.x)} ${number(c.y)}, ${number(d.x)} ${number(d.y)}`;

export function linkGeometry(source, target) {
  const dx = target.x - source.x,
    dy = target.y - source.y;
  let a, b, c, d;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const sign = dx >= 0 ? 1 : -1,
      bend = Math.max(45, Math.abs(dx) * 0.32);
    a = { x: source.x + sign * 71, y: source.y };
    d = { x: target.x - sign * 71, y: target.y };
    b = { x: a.x + sign * bend, y: a.y };
    c = { x: d.x - sign * bend, y: d.y };
  } else {
    const sign = dy >= 0 ? 1 : -1,
      bend = Math.max(45, Math.abs(dy) * 0.32);
    a = { x: source.x, y: source.y + sign * 42 };
    d = { x: target.x, y: target.y - sign * 42 };
    b = { x: a.x, y: a.y + sign * bend };
    c = { x: d.x, y: d.y - sign * bend };
  }
  const curve = [a, b, c, d];
  return {
    full: path(curve),
    before: path(split(curve, 0.43)[0]),
    after: path(split(curve, 0.57)[1]),
    middle: split(curve, 0.5)[0][3],
  };
}

export function interpolatePositions(previous, next, progress) {
  return new Map(
    [...next].map(([id, end]) => [
      id,
      mix(previous.get(id) ?? end, end, progress),
    ]),
  );
}

export function createWorkloadRenderer(graph, { onSelect }) {
  const layers = {
    lanes: make("g"),
    links: make("g"),
    ghosts: make("g"),
    nodes: make("g"),
  };
  const defs = make("defs");
  for (const state of ["observed", "blocked", "unknown", "requested"]) {
    const marker = make("marker", {
      id: `response-arrow-${state}`,
      viewBox: "0 0 10 10",
      refX: 8,
      refY: 5,
      markerWidth: 6,
      markerHeight: 6,
      orient: "auto-start-reverse",
    });
    marker.append(
      make("path", { d: "M 0 0 L 10 5 L 0 10 z", class: `arrow-${state}` }),
    );
    defs.append(marker);
  }
  let attached = false,
    nodes = new Map(),
    links = new Map(),
    positions = new Map(),
    frame = null;
  let quarantine = new Set(),
    stream = null;
  const reduced = () =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function reset() {
    cancelAnimationFrame(frame);
    frame = null;
    nodes.clear();
    links.clear();
    positions.clear();
    quarantine.clear();
    stream = null;
    for (const layer of Object.values(layers)) layer.replaceChildren();
    graph.replaceChildren();
    attached = false;
  }
  function empty() {
    reset();
    graph.setAttribute("viewBox", "0 0 1000 600");
    graph.append(
      make("rect", {
        x: 20,
        y: 30,
        width: 960,
        height: 535,
        rx: 24,
        class: "response-empty-bg",
      }),
      make(
        "text",
        {
          x: 500,
          y: 250,
          "text-anchor": "middle",
          class: "response-empty-title",
        },
        "실제 워크로드 관측 연결 대기",
      ),
      make(
        "text",
        {
          x: 500,
          y: 290,
          "text-anchor": "middle",
          class: "response-empty-detail",
        },
        "등록된 앱의 자원·연결·근거가 제공되면 여기에 표시됩니다.",
      ),
      make(
        "text",
        {
          x: 500,
          y: 325,
          "text-anchor": "middle",
          class: "response-empty-detail",
        },
        "로컬 시나리오에서 격리·대체 움직임을 먼저 살펴볼 수 있습니다.",
      ),
    );
  }
  function createNode(node) {
    const g = make("g", {
      role: "button",
      tabindex: 0,
      "data-resource": node.id,
    });
    const body = make("g", { class: "response-node-body" });
    body.append(
      make("rect", { x: -71, y: -42, width: 142, height: 84, rx: 12 }),
      make("text", { x: -51, y: -15, class: "response-node-icon" }),
      make("text", { x: -30, y: -15, class: "response-node-label" }),
      make("text", { x: -56, y: 8, class: "response-node-state" }),
      make("text", { x: -56, y: 28, class: "response-node-kind" }),
    );
    g.append(body);
    g.addEventListener("click", (event) => onSelect(node.id, event.shiftKey));
    g.addEventListener("keydown", (event) => {
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault();
        onSelect(node.id, event.shiftKey);
      }
    });
    return g;
  }
  function createLink(edge) {
    const g = make("g", { "data-link": edge.id });
    g.append(
      make("title"),
      make("path", { class: "response-path-before" }),
      make("path", { class: "response-path-after" }),
      make("text", { class: "response-cut", "text-anchor": "middle" }, "×"),
    );
    const particle = make("circle", { r: 3.5, class: "response-packet" });
    particle.append(
      make("animateMotion", {
        dur: "2.6s",
        repeatCount: "indefinite",
        rotate: "auto",
      }),
    );
    g.append(particle);
    return g;
  }
  function draw(view, { selected = new Set() } = {}) {
    if (!view) return empty();
    const identity = `${view.plan_id}/${view.stream_id}`;
    if (stream !== null && stream !== identity) reset();
    stream = identity;
    if (!attached) {
      graph.replaceChildren(defs, ...Object.values(layers));
      attached = true;
    }
    cancelAnimationFrame(frame);
    const keys = new Set(view.nodes.map((n) => `${n.id}/${n.resource_uid}`));
    quarantine = new Set([...quarantine].filter((key) => keys.has(key)));
    for (const n of view.nodes) {
      const key = `${n.id}/${n.resource_uid}`;
      if (n.security === "isolated") quarantine.add(key);
      // Expiry is not release. Only a current, resource-bound recovery receipt
      // can clear the old placement; unknown nodes stay as historical outlines.
      if (
        n.security === "clear" &&
        view.fresh &&
        view.evidence.some(
          (e) =>
            e.resource_id === n.id &&
            e.resource_uid === n.resource_uid &&
            e.kind === "recovery" &&
            e.outcome === "verified" &&
            Date.parse(e.expires_at) > Date.now(),
        )
      )
        quarantine.delete(key);
    }
    const layout = workloadLayout(
      view.nodes.map((n) => ({
        ...n,
        display_quarantined: quarantine.has(`${n.id}/${n.resource_uid}`),
      })),
    );
    graph.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    layers.lanes.replaceChildren();
    for (const [x, y, width, height, label] of layout.lanes) {
      const isolated = label.startsWith("격리");
      layers.lanes.append(
        make("rect", {
          x,
          y,
          width,
          height,
          rx: 18,
          class: isolated
            ? "response-lane response-quarantine-zone"
            : "response-lane",
        }),
        make(
          "text",
          { x: x + 18, y: y + 27, class: "response-lane-label" },
          label,
        ),
      );
    }
    layers.ghosts.replaceChildren();
    for (const id of layout.quarantined) {
      const p = layout.registeredPositions.get(id);
      const ghost = make("g", {
        transform: `translate(${p.x},${p.y})`,
        class: "response-origin-ghost",
      });
      ghost.append(
        make("rect", { x: -71, y: -42, width: 142, height: 84, rx: 12 }),
        make(
          "text",
          { x: 0, y: 5, "text-anchor": "middle" },
          "기존 위치 · 격리 표시",
        ),
      );
      layers.ghosts.append(ghost);
    }
    const warnings = view.nodes
      .filter((n) => ["warning", "hold"].includes(n.security))
      .map((n) => n.id);
    const impact = new Set(
      possibleImpact(view, [...new Set([...warnings, ...selected])]).filter(
        (id) => !warnings.includes(id),
      ),
    );
    const ids = new Set(view.nodes.map((n) => n.id));
    for (const [id, value] of nodes)
      if (!ids.has(id)) {
        value.element.remove();
        nodes.delete(id);
        positions.delete(id);
      }
    for (const n of view.nodes) {
      let saved = nodes.get(n.id);
      if (saved && saved.uid !== n.resource_uid) {
        saved.element.remove();
        nodes.delete(n.id);
        positions.delete(n.id);
        saved = null;
      }
      if (!saved) {
        saved = { element: createNode(n), uid: n.resource_uid };
        nodes.set(n.id, saved);
        layers.nodes.append(saved.element);
      }
      const g = saved.element,
        isolated = layout.quarantined.has(n.id);
      const arriving =
        saved.health === "unknown" &&
        n.health === "ready" &&
        n.lane === "target" &&
        ["app", "worker"].includes(n.kind);
      g.querySelector(".response-node-body").setAttribute(
        "class",
        `response-node-body${arriving ? " response-arriving" : ""}`,
      );
      saved.health = n.health;
      const historical = isolated && n.security !== "isolated";
      g.setAttribute(
        "class",
        `response-node health-${n.health} security-${n.security}${selected.has(n.id) ? " selected" : ""}${impact.has(n.id) ? " possible-impact" : ""}${historical ? " historical-placement" : ""}`,
      );
      g.setAttribute(
        "aria-label",
        `${n.label} · ${healthLabels[n.health]} · ${securityLabels[n.security]}`,
      );
      g.setAttribute("aria-pressed", selected.has(n.id));
      g.setAttribute(
        "data-placement",
        isolated ? (historical ? "historical" : "quarantine") : n.lane,
      );
      g.querySelector(".response-node-icon").textContent = icons[n.kind];
      g.querySelector(".response-node-label").textContent =
        n.label.length > 10 ? n.label.slice(0, 10) + "…" : n.label;
      g.querySelector(".response-node-state").textContent = historical
        ? "격리 여부 재확인 필요"
        : n.security !== "clear" && n.security !== "unknown"
          ? securityLabels[n.security]
          : healthLabels[n.health];
      g.querySelector(".response-node-kind").textContent = impact.has(n.id)
        ? "영향 가능 · 미확정"
        : n.kind.toUpperCase();
    }
    const edgeIds = new Set(view.edges.map((e) => e.id));
    for (const [id, element] of links)
      if (!edgeIds.has(id)) {
        element.remove();
        links.delete(id);
      }
    for (const e of view.edges) {
      if (!links.has(e.id)) {
        const value = createLink(e);
        links.set(e.id, value);
        layers.links.append(value);
      }
      const g = links.get(e.id);
      g.setAttribute("class", `response-link ${e.state}`);
      g.querySelector("title").textContent =
        `${e.label} · ${linkLabels[e.state]}`;
      g.querySelector(".response-path-before").setAttribute(
        "marker-end",
        e.state === "blocked" ? "" : `url(#response-arrow-${e.state})`,
      );
      g.querySelector(".response-path-after").setAttribute(
        "marker-end",
        `url(#response-arrow-${e.state})`,
      );
      g.querySelector(".response-packet").style.display =
        e.state === "observed" && !reduced() ? "" : "none";
    }
    const before = new Map(positions),
      after = layout.positions;
    const started = performance.now(),
      duration = reduced() || !positions.size ? 0 : 950;
    function paint(now) {
      const progress = duration ? Math.min(1, (now - started) / duration) : 1;
      const eased = 1 - (1 - progress) ** 3;
      positions = interpolatePositions(before, after, eased);
      for (const [id, point] of positions)
        nodes
          .get(id)
          .element.setAttribute(
            "transform",
            `translate(${point.x},${point.y})`,
          );
      for (const edge of view.edges) {
        const g = links.get(edge.id),
          shape = linkGeometry(
            positions.get(edge.source),
            positions.get(edge.target),
          );
        const blocked = edge.state === "blocked";
        g.querySelector(".response-path-before").setAttribute(
          "d",
          blocked ? shape.before : shape.full,
        );
        const end = g.querySelector(".response-path-after");
        end.setAttribute("d", shape.after);
        end.style.display = blocked ? "" : "none";
        const cut = g.querySelector(".response-cut");
        cut.setAttribute("x", shape.middle.x);
        cut.setAttribute("y", shape.middle.y + 8);
        cut.style.display = blocked ? "" : "none";
        g.querySelector("animateMotion").setAttribute("path", shape.full);
      }
      if (progress < 1) frame = requestAnimationFrame(paint);
      else {
        frame = null;
        graph.removeAttribute("data-moving");
      }
    }
    if (duration) graph.setAttribute("data-moving", "true");
    paint(started);
  }
  return { draw, reset, destroy: reset };
}
