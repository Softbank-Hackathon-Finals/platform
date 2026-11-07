// Read-only network features in Sova's existing shell; no second page or workflow.
import { request } from "./transport.js";
import Panzoom from "./network/vendor/panzoom-4.6.2.es.js";
import { paths, permissions, snapshot as reference } from "./network/data.js";
import { phrases } from "./network/locales.js";
import { sequenceModel } from "./network/sequence.js";
import { summarizeDiagnostics } from "./network/diagnostics.js";

const SOURCE_NAMES = {
  source_k8s: "Source · AWS EKS",
  platform_k8s: "Platform · RKE2",
  target_k8s: "Target · RKE2",
  aws_network: "AWS 네트워크",
  tailscale_human: "운영자 공유망",
  tailscale_workload: "워크로드 망",
  local_underlay: "호스트·VM",
  platform_api: "운영자 API",
  target_host: "목적지 호스트",
};
const TABS = {
  map: "지도",
  resources: "리소스",
  transport: "전송 경로",
  routes: "라우팅·NAT",
  health: "수집 상태",
  protocols: "프로토콜",
  permissions: "권한 참고",
  diagrams: "구조도",
};
const CLUSTERS = [
  ["source_k8s", "Source", 36, 50],
  ["platform_k8s", "Platform", 36, 304],
  ["target_k8s", "Target", 672, 178],
];
const SHEETS = [
  ["플랫폼 아키텍처", "architecture-original.jpg", 3800, 2980],
  ["S1–S4 이전 순서", "migration-original.jpg", 3200, 4408],
];
const validTime = (value) =>
  typeof value === "string" && Number.isFinite(Date.parse(value));
const finiteText = (value) => typeof value === "string" && value.length <= 1200;

export function networkDocument(body, plan, now = Date.now()) {
  const c = body?.sova_context;
  if (
    !plan ||
    body.schema_version !== 1 ||
    c?.plan_id !== plan.plan_id ||
    c.plan_digest !== plan.plan_digest ||
    c.plan_version !== plan.version ||
    c.source_environment_id !== plan.source_environment_id ||
    c.target_environment_id !== plan.target_environment_id ||
    c.scope !== "shared-environment-network" ||
    c.authorizes_cutover !== false ||
    !validTime(body.server_now) ||
    Math.abs(now - Date.parse(body.server_now)) > 15000 ||
    !Array.isArray(body.sources) ||
    body.sources.length > 9
  )
    throw Error("network_scope_invalid");
  const seen = new Set();
  for (const s of body.sources) {
    if (
      !Object.hasOwn(SOURCE_NAMES, s.id) ||
      seen.has(s.id) ||
      !["ok", "error"].includes(s.status) ||
      !validTime(s.checked_at) ||
      Date.parse(s.checked_at) > now + 60000 ||
      (s.last_success_at != null && !validTime(s.last_success_at)) ||
      !finiteText(s.scope)
    )
      throw Error("network_source_invalid");
    seen.add(s.id);
    if (s.retained && (s.status !== "error" || !s.last_success_at))
      throw Error("network_retained_invalid");
    for (const name of ["resources", "networks", "links"]) {
      const rows = s.data?.[name] ?? [];
      if (
        !Array.isArray(rows) ||
        rows.length > 1500 ||
        rows.some(
          (row) =>
            !row ||
            typeof row !== "object" ||
            Object.values(row).some((value) => !finiteText(value)),
        )
      )
        throw Error("network_rows_invalid");
    }
  }
  return body;
}

export function networkRows(body, name, now = Date.now()) {
  return (body?.sources ?? []).flatMap((s) =>
    (s.data?.[name] ?? []).map((row) => ({
      ...row,
      source: s.id,
      source_status: s.status,
      retained: s.retained === true,
      observed_at: s.retained ? s.last_success_at : s.checked_at,
      stale:
        now - Date.parse(s.retained ? s.last_success_at : s.checked_at) >
        300000,
    })),
  );
}

export function mountNetworkWorkspace({ getModel }, root = document) {
  const host = root.getElementById("network-workspace");
  let data = null,
    key = null,
    epoch = 0,
    socket = null,
    abort = null,
    retry = null,
    status = "계획 연결 대기",
    tab = "map",
    language = "ko",
    selected = null,
    playTimer = null,
    step = 0,
    sheet = 0,
    graphZoom = null,
    imageZoom = null,
    imageSource = null,
    busy = false,
    stopped = false,
    denied = false;
  let lastMessage = 0;
  const node = (tag, text, cls) => {
    const n = root.createElement(tag);
    if (text != null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  };
  const translations = Object.fromEntries(
    ["en", "ja"].map((lang, i) => [
      lang,
      new Map(phrases.map((row) => [row[0], row[i + 1]])),
    ]),
  );
  const tr = (text) =>
    language === "ko" ? text : (translations[language]?.get(text) ?? text);
  const date = (value) =>
    validTime(value)
      ? new Date(value).toLocaleString("ko-KR", {
          timeZone: "Asia/Seoul",
          hour12: false,
        })
      : "미확인";
  const btn = (text, fn, cls = "secondary") => {
    const b = node("button", tr(text), cls);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  };
  const field = (label, input) => {
    const wrap = node("label", label, "network-field");
    wrap.append(input);
    return wrap;
  };
  const select = (id, values, changed) => {
    const s = node("select");
    s.id = id;
    for (const [v, t] of values) {
      const o = node("option", tr(t));
      o.value = v;
      s.append(o);
    }
    s.addEventListener("change", changed);
    return s;
  };
  const heading = node("div", null, "section-title"),
    titleWrap = node("div"),
    title = node("h2", "환경 네트워크");
  titleWrap.append(node("p", "ENVIRONMENT NETWORK", "eyebrow"), title);
  const statusNode = node("span", status, "status-pill");
  statusNode.id = "network-connection";
  const refresh = btn("새로 확인", () => {
    denied = false;
    void load(epoch);
    connect(epoch);
  });
  refresh.id = "network-refresh";
  const locale = select(
    "network-language",
    [
      ["ko", "한국어"],
      ["en", "English"],
      ["ja", "日本語"],
    ],
    () => {
      language = locale.value;
      draw();
    },
  );
  const tools = node("div", null, "actions");
  tools.append(field("자료 언어", locale), statusNode, refresh);
  heading.append(titleWrap, tools);
  const note = node(
    "p",
    "등록된 환경을 포함한 공유 관측입니다. 앱 내부 의존성·업무 성공·전환 허가를 뜻하지 않습니다.",
    "muted",
  );
  note.id = "network-scope";
  const summary = node("div", null, "network-summary");
  summary.id = "network-summary";
  const tabs = node("div", null, "network-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "네트워크 상세 보기");
  const panes = {};
  for (const [id, text] of Object.entries(TABS)) {
    const button = btn(text, () => show(id));
    button.dataset.networkTab = id;
    button.id = "network-tab-" + id;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "network-pane-" + id);
    tabs.append(button);
    const pane = node("div", null, "network-pane");
    pane.id = "network-pane-" + id;
    pane.setAttribute("role", "tabpanel");
    pane.setAttribute("aria-labelledby", button.id);
    panes[id] = pane;
  }
  host.append(heading, note, summary, tabs, ...Object.values(panes));
  const detail = node("aside", null, "network-detail");
  detail.id = "network-detail";
  detail.setAttribute("aria-label", "선택한 구성 요소 상세");
  host.append(detail);
  function table(pane, columns, rows, onPick) {
    const scroll = node("div", null, "table-scroll"),
      table = node("table", null, "control-table"),
      head = node("thead"),
      header = node("tr"),
      body = node("tbody");
    for (const c of columns) {
      const th = node("th", tr(c));
      th.scope = "col";
      header.append(th);
    }
    head.append(header);
    for (const [index, row] of rows.entries()) {
      const r = node("tr");
      if (onPick) r.dataset.networkRow = "true";
      for (const [i, value] of row.entries()) {
        const cell = node("td");
        if (onPick && i === 0)
          cell.append(
            btn(
              String(value),
              () => {
                selected = onPick(index);
                renderDetail();
              },
              "network-pick",
            ),
          );
        else cell.textContent = String(value ?? "미확인");
        r.append(cell);
      }
      body.append(r);
    }
    if (!rows.length) {
      const r = node("tr"),
        cell = node("td", tr("표시할 자료가 없습니다."));
      cell.colSpan = columns.length;
      r.append(cell);
      body.append(r);
    }
    table.append(head, body);
    scroll.append(table);
    pane.replaceChildren(scroll);
  }
  const sourceFilter = select(
    "network-source-filter",
    [["all", "전체 환경"], ...Object.entries(SOURCE_NAMES)],
    () => drawTables(),
  );
  const kindFilter = select(
    "network-kind-filter",
    [
      ["all", "전체 종류"],
      ...[
        "Service",
        "Pod",
        "Node",
        "EC2",
        "EKS",
        "Tailscale peer",
        "Tailscale device",
      ].map((n) => [n, n]),
    ],
    () => drawTables(),
  );
  const search = node("input");
  search.id = "network-search";
  search.type = "search";
  search.placeholder = "리소스·namespace·주소 검색";
  search.setAttribute("aria-label", search.placeholder);
  search.addEventListener("input", drawTables);
  const filters = node("div", null, "network-filters");
  filters.append(
    field("환경", sourceFilter),
    field("종류", kindFilter),
    field("검색", search),
  );
  panes.resources.append(filters);
  const resourceTable = node("div");
  panes.resources.append(resourceTable);
  const transportFilter = select(
    "network-transport-filter",
    [
      ["all", "전체 경로"],
      ["DIRECT", "직접 연결"],
      ["DERP", "릴레이"],
      ["UNKNOWN", "미확인"],
    ],
    drawTables,
  );
  panes.transport.append(field("전송 방식", transportFilter));
  const transportTable = node("div");
  panes.transport.append(transportTable);
  const routesTable = node("div");
  panes.routes.append(routesTable);
  const healthTable = node("div"),
    issues = node("div", null, "network-issues");
  panes.health.append(issues, healthTable);
  const protocolSelect = select(
    "network-protocol-select",
    paths.map((p) => [p.id, p.name]),
    () => {
      stopPlay();
      step = 0;
      drawProtocol();
    },
  );
  const protocolContent = node("div");
  const protocolButtons = node("div", null, "actions");
  protocolButtons.append(
    btn("이전 단계", () => {
      stopPlay();
      step = Math.max(0, step - 1);
      drawProtocol();
    }),
    btn("다음 단계", () => {
      stopPlay();
      step++;
      drawProtocol();
    }),
    btn("설명 재생·정지", () => {
      if (playTimer) return stopPlay();
      playTimer = setInterval(() => {
        step++;
        drawProtocol();
      }, 1400);
    }),
  );
  panes.protocols.append(
    node(
      "p",
      `설명용 순서 · ${reference.label} 기준 · 실제 패킷이나 실행 이벤트가 아닙니다.`,
      "muted",
    ),
    field("설명할 경로", protocolSelect),
    protocolButtons,
    protocolContent,
  );
  const permissionTable = node("div");
  panes.permissions.append(
    node(
      "p",
      `권한 참고 자료 · ${reference.label} · 현재 권한이나 새 승인 근거로 사용하지 않습니다.`,
      "muted",
    ),
    permissionTable,
  );
  const viewport = node("div", null, "network-viewport"),
    paper = node("div", null, "network-paper"),
    svg = root.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 960 530");
  svg.setAttribute("role", "group");
  svg.setAttribute("aria-label", "실제 환경 관측과 선택한 전송 경로");
  paper.append(svg);
  viewport.append(paper);
  panes.map.append(viewport);
  const mapTools = node("div", null, "actions"),
    mapScale = node("output", "100%");
  mapScale.id = "network-map-scale";
  const fitGraph = () => {
    if (!graphZoom || !viewport.clientWidth) return;
    graphZoom.zoom(Math.min(1, viewport.clientWidth / 960), { animate: false });
    graphZoom.pan(0, 0, { animate: false });
  };
  mapTools.append(
    btn("−", () => graphZoom?.zoomOut()),
    mapScale,
    btn("＋", () => graphZoom?.zoomIn()),
    btn("화면 맞춤", fitGraph),
    btn("선택 해제", () => {
      selected = null;
      renderDetail();
      drawGraph();
    }),
  );
  panes.map.append(
    mapTools,
    node(
      "p",
      "노드는 실제 목록 조회입니다. 선은 선택한 관측 경로의 이름을 기준으로 배치합니다. 다른 연결이나 DB 복제를 추정하지 않습니다.",
      "muted",
    ),
  );
  graphZoom = Panzoom(paper, {
    canvas: true,
    minScale: 0.2,
    maxScale: 3,
    animate: false,
  });
  paper.addEventListener(
    "panzoomchange",
    (e) => (mapScale.textContent = Math.round(e.detail.scale * 100) + "%"),
  );
  viewport.addEventListener("wheel", (e) => graphZoom.zoomWithWheel(e), {
    passive: false,
  });
  const sheetSelect = select(
    "network-sheet",
    SHEETS.map((s, i) => [String(i), s[0]]),
    () => {
      sheet = Number(sheetSelect.value);
      drawImage();
    },
  );
  const imageViewport = node("div", null, "network-image-viewport"),
    image = node("img");
  image.id = "network-original-image";
  image.alt = "팀에서 공유한 과거 설계 원본";
  imageViewport.append(image);
  const imageScale = node("output", "대기");
  imageScale.id = "network-image-scale";
  const imageFit = () => {
    if (!imageZoom || !imageViewport.clientWidth) return;
    imageZoom.zoom(
      Math.min(
        1,
        (imageViewport.clientWidth - 24) / SHEETS[sheet][2],
        (imageViewport.clientHeight - 24) / SHEETS[sheet][3],
      ),
      { animate: false },
    );
    imageZoom.pan(0, 0, { animate: false });
  };
  image.addEventListener("load", () => {
    imageZoom?.destroy();
    imageZoom = Panzoom(image, {
      canvas: true,
      minScale: 0.025,
      maxScale: 3,
      animate: false,
    });
    imageFit();
  });
  image.addEventListener(
    "panzoomchange",
    (e) => (imageScale.textContent = Math.round(e.detail.scale * 100) + "%"),
  );
  imageViewport.addEventListener("wheel", (e) => imageZoom?.zoomWithWheel(e), {
    passive: false,
  });
  const imageTools = node("div", null, "actions"),
    download = node("a", "원본 다운로드", "secondary");
  download.id = "network-image-download";
  download.download = "";
  imageTools.append(
    btn("−", () => imageZoom?.zoomOut()),
    imageScale,
    btn("＋", () => imageZoom?.zoomIn()),
    btn("화면 맞춤", imageFit),
    btn("100%", () => imageZoom?.zoom(1)),
    btn("전체 화면", () => {
      if (root.fullscreenElement === imageViewport) void root.exitFullscreen();
      else void imageViewport.requestFullscreen?.();
    }),
    download,
  );
  const imageCaption = node("p", null, "muted");
  imageCaption.id = "network-image-caption";
  panes.diagrams.append(
    node(
      "p",
      "팀이 공유한 원본 도면 · 과거 설계 참고 · 현재 실행 결과가 아닙니다.",
      "muted",
    ),
    field("도면", sheetSelect),
    imageCaption,
    imageTools,
    imageViewport,
  );
  function drawImage() {
    const s = SHEETS[sheet];
    imageCaption.textContent = `${s[0]} · ${s[2]} × ${s[3]} · 원본`;
    download.href = "/console/network/" + s[1];
    if (imageSource !== s[1]) {
      imageZoom?.destroy();
      imageZoom = null;
      imageSource = s[1];
      image.src = download.href;
    } else imageFit();
  }
  function stopPlay() {
    clearInterval(playTimer);
    playTimer = null;
  }
  function show(next) {
    tab = next;
    for (const [id, p] of Object.entries(panes)) {
      p.hidden = id !== tab;
      const b = root.getElementById("network-tab-" + id);
      b.setAttribute("aria-selected", String(id === tab));
      b.tabIndex = id === tab ? 0 : -1;
    }
    if (tab !== "protocols") stopPlay();
    if (tab === "diagrams") drawImage();
    if (tab === "map") fitGraph();
  }
  tabs.addEventListener("keydown", (event) => {
    const ids = Object.keys(TABS);
    const at = ids.indexOf(tab);
    let next;
    if (event.key === "ArrowRight") next = (at + 1) % ids.length;
    else if (event.key === "ArrowLeft")
      next = (at + ids.length - 1) % ids.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = ids.length - 1;
    else return;
    event.preventDefault();
    show(ids[next]);
    root.getElementById("network-tab-" + ids[next]).focus();
  });
  function drawProtocol() {
    const p = paths.find((p) => p.id === protocolSelect.value) || paths[0],
      model = sequenceModel(p);
    step = Math.max(0, Math.min(step, p.steps.length - 1));
    if (step === p.steps.length - 1) stopPlay();
    const list = node("ol", null, "network-protocol-steps");
    for (const [i, s] of p.steps.entries()) {
      const row = node("li", null, i === step ? "current" : "");
      row.append(node("b", tr(s[1])), node("p", tr(s[2])));
      if (model?.steps[i]?.arrow) {
        const [a, b] = model.steps[i].arrow;
        row.append(
          node("small", model.lanes[a][0] + " → " + model.lanes[b][0]),
        );
      }
      list.append(row);
    }
    const layers = node("div");
    table(
      layers,
      ["계층", "프로토콜·보호 기능"],
      (p.stack ?? []).map((row) => row.map(tr)),
    );
    protocolContent.replaceChildren(
      node("h3", tr(p.headline)),
      node("p", tr(p.desc)),
      node("p", p.endpoint ?? "", "network-code"),
      layers,
      list,
    );
  }
  function drawTables() {
    const query = search.value.trim().toLowerCase();
    const resources = networkRows(data, "resources").filter(
      (r) =>
        (sourceFilter.value === "all" || r.source === sourceFilter.value) &&
        (kindFilter.value === "all" || r.kind === kindFilter.value) &&
        (!query || JSON.stringify(r).toLowerCase().includes(query)),
    );
    table(
      resourceTable,
      [
        "리소스",
        "환경 / namespace",
        "종류",
        "실제 상태",
        "주소 / 포트",
        "관측 시각",
      ],
      resources.map((r) => [
        r.name,
        SOURCE_NAMES[r.source] + " / " + (r.namespace ?? "—"),
        r.kind,
        (r.source_status === "error" ? "조회 실패 · " : "") +
          (r.retained ? "마지막 성공값 · " : "") +
          (r.stale ? "오래됨 · " : "") +
          (r.status ?? "UNKNOWN"),
        (r.ip ?? "—") + " / " + (r.ports ?? "—"),
        date(r.observed_at),
      ]),
      (i) => ({ kind: "resource", value: resources[i] }),
    );
    const links = networkRows(data, "links").filter(
      (l) =>
        transportFilter.value === "all" ||
        (transportFilter.value === "UNKNOWN"
          ? !["DIRECT", "DERP"].includes(l.state)
          : l.state === transportFilter.value),
    );
    table(
      transportTable,
      ["출발", "목적지", "실제 경로", "상태", "관측 시각", "근거"],
      links.map((l) => [
        l.from,
        l.to,
        l.transport,
        l.state ?? "UNKNOWN",
        date(l.observed_at),
        l.detail,
      ]),
      (i) => {
        const s = { kind: "link", value: links[i] };
        selected = s;
        drawGraph();
        return s;
      },
    );
    const routes = networkRows(data, "networks");
    table(
      routesTable,
      ["환경", "계층", "이름", "주소망", "다음 홉", "NAT", "근거"],
      routes.map((r) => [
        SOURCE_NAMES[r.source],
        r.layer,
        r.name,
        r.cidr,
        r.next_hop,
        r.nat,
        r.detail,
      ]),
      (i) => ({ kind: "route", value: routes[i] }),
    );
    const sources = data?.sources ?? [];
    table(
      healthTable,
      [
        "수집 소스",
        "상태",
        "마지막 시도",
        "마지막 성공",
        "오류 코드",
        "조회 범위",
      ],
      sources.map((s) => [
        SOURCE_NAMES[s.id],
        s.status === "error"
          ? "조회 실패"
          : Date.now() - Date.parse(s.checked_at) > 300000
            ? "오래된 관측"
            : "최근 조회",
        date(s.checked_at),
        date(s.last_success_at),
        s.error_code ?? "—",
        s.scope,
      ]),
      (i) => ({ kind: "source", value: sources[i] }),
    );
    const diagnostics = data ? summarizeDiagnostics(data, Date.now()) : null;
    issues.replaceChildren(
      node(
        "p",
        diagnostics
          ? `확인이 필요한 항목 ${diagnostics.issues.length}개 · 네트워크 관측 근거이며 전환 판정이 아닙니다.`
          : "현재 자료를 기다립니다.",
        "muted",
      ),
    );
    table(
      permissionTable,
      ["역할", "사용하는 망", "대상", "허용", "제한", "당시 근거"],
      permissions.map((r) => r.map(tr)),
    );
  }
  function svgNode(tag, attrs, text) {
    const n = root.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    if (text != null) n.textContent = String(text);
    return n;
  }
  function graphCluster(value) {
    const match = String(value ?? "").match(/^(Source|Platform|Target)\s*\//i);
    if (match) return match[1].toLowerCase() + "_k8s";
    return /target/i.test(value)
      ? "target_k8s"
      : /source/i.test(value)
        ? "source_k8s"
        : /platform|metrics|prometheus|intake/i.test(value)
          ? "platform_k8s"
          : null;
  }
  function drawGraph() {
    const content = [];
    if (selected?.kind === "link") {
      const l = selected.value,
        a = CLUSTERS.find((c) => c[0] === graphCluster(l.from)),
        b = CLUSTERS.find((c) => c[0] === graphCluster(l.to));
      if (a && b && a !== b) {
        const active = ["DIRECT", "DERP"].includes(l.state);
        content.push(
          svgNode("path", {
            d: `M${a[2] + 244} ${a[3] + 74} C510 ${a[3] + 74},510 ${b[3] + 74},${b[2]} ${b[3] + 74}`,
            class: active ? "network-edge" : "network-edge unknown",
          }),
          svgNode(
            "text",
            { x: 470, y: 160, class: "network-graph-note" },
            l.state ?? "UNKNOWN",
          ),
        );
      }
    }
    content.push(
      svgNode(
        "text",
        { x: 390, y: 245, class: "network-graph-note" },
        "공유 환경 관측",
      ),
      svgNode(
        "text",
        { x: 354, y: 270, class: "network-graph-note" },
        "전송 경로를 선택하면 근거를 표시",
      ),
    );
    for (const [id, name, x, y] of CLUSTERS) {
      const s = data?.sources.find((s) => s.id === id),
        r = s?.data?.resources ?? [];
      const group = svgNode("g", {
        transform: `translate(${x} ${y})`,
        tabindex: 0,
        role: "button",
        "aria-label": name + " 환경 상세",
        class: "network-cluster",
      });
      group.append(
        svgNode("rect", { width: 244, height: 164, rx: 14 }),
        svgNode("text", { x: 20, y: 34, class: "network-node-name" }, name),
        svgNode(
          "text",
          { x: 20, y: 61, class: "network-node-label" },
          SOURCE_NAMES[id],
        ),
        svgNode(
          "text",
          { x: 20, y: 91, class: "network-node-state" },
          s?.status === "error"
            ? "조회 실패"
            : s
              ? Date.now() - Date.parse(s.checked_at) > 300000
                ? "오래된 관측"
                : "목록 조회됨"
              : "관측 대기",
        ),
        svgNode(
          "text",
          { x: 20, y: 120, class: "network-node-count" },
          s?.data
            ? `Pod ${r.filter((r) => r.kind === "Pod").length} · Service ${r.filter((r) => r.kind === "Service").length}`
            : "Pod·Service 수 미확인",
        ),
        svgNode(
          "text",
          { x: 20, y: 145, class: "network-node-time" },
          s ? date(s.checked_at) : "성공을 추정하지 않습니다.",
        ),
      );
      const pick = () => {
        selected = { kind: "source", value: s ?? { id, scope: "자료 미연결" } };
        renderDetail();
        sourceFilter.value = id;
        drawTables();
      };
      group.addEventListener("click", pick);
      group.addEventListener("keydown", (e) => {
        if (["Enter", " "].includes(e.key)) {
          e.preventDefault();
          pick();
        }
      });
      content.push(group);
    }
    svg.replaceChildren(...content);
  }
  function renderDetail() {
    if (!selected) {
      detail.replaceChildren(
        node("h3", "구성 요소 상세"),
        node(
          "p",
          "클러스터·리소스·전송 경로를 선택하면 원본 관측 근거를 확인할 수 있습니다.",
          "muted",
        ),
      );
      return;
    }
    const v = { ...selected.value };
    if (v.observed_at)
      v.stale = Date.now() - Date.parse(v.observed_at) > 300000;
    const heading = node(
        "h3",
        v.name ?? v.from ?? SOURCE_NAMES[v.id] ?? "선택한 관측",
      ),
      facts = node("dl");
    for (const [label, value] of Object.entries(v)) {
      if (value == null || typeof value === "object") continue;
      facts.append(node("dt", label), node("dd", String(value)));
    }
    detail.replaceChildren(heading, facts);
  }
  function draw() {
    statusNode.textContent = status;
    refresh.disabled = busy || !getModel()?.snapshot;
    summary.replaceChildren();
    const sources = data?.sources ?? [];
    for (const [label, value] of [
      [
        "수집 소스",
        sources.length
          ? `${sources.filter((s) => s.status === "ok").length} / ${sources.length}`
          : "미연결",
      ],
      ["조회 리소스", data ? networkRows(data, "resources").length : "미확인"],
      ["관측 경로", data ? networkRows(data, "links").length : "미확인"],
      ["최근 원본 수신", date(data?.received_at)],
    ]) {
      const item = node("div", null, "network-stat");
      item.append(node("span", tr(label)), node("strong", value));
      summary.append(item);
    }
    for (const [id, text] of Object.entries(TABS))
      root.getElementById("network-tab-" + id).textContent = tr(text);
    drawGraph();
    drawTables();
    drawProtocol();
    renderDetail();
  }
  function stop() {
    epoch++;
    socket?.close();
    socket = null;
    abort?.abort();
    abort = null;
    clearTimeout(retry);
    retry = null;
    busy = false;
    lastMessage = 0;
    stopPlay();
  }
  function clear(message = "계획 연결 대기") {
    stop();
    data = null;
    selected = null;
    status = message;
    key = null;
    denied = false;
    search.value = "";
    sourceFilter.value = "all";
    kindFilter.value = "all";
    draw();
  }
  function deny() {
    stop();
    data = null;
    selected = null;
    denied = true;
    search.value = "";
    status = "네트워크 조회 권한을 확인할 수 없습니다.";
    draw();
  }
  function accept(value, current) {
    const m = getModel();
    if (current !== epoch || !m?.snapshot) return;
    try {
      data = networkDocument(value, m.snapshot);
      selected = null;
      status = "네트워크 관측 수신 · 실행 허가 아님";
    } catch {
      data = null;
      selected = null;
      status = "현재 계획의 관측 범위를 확인할 수 없습니다.";
    }
    draw();
  }
  async function load(current) {
    const m = getModel();
    if (busy || !m?.snapshot || current !== epoch) return;
    busy = true;
    refresh.disabled = true;
    const plan = m.plan;
    abort = new AbortController();
    try {
      const body = await request(`/v1/plans/${plan}/network-status`, {
        signal: abort.signal,
      });
      accept(body, current);
    } catch (e) {
      if (current !== epoch) return;
      if ([401, 403].includes(e.status)) {
        deny();
        return;
      }
      data = null;
      selected = null;
      status = "네트워크 관측을 확인할 수 없습니다.";
      draw();
    } finally {
      if (current === epoch) {
        busy = false;
        refresh.disabled = false;
      }
    }
  }
  function connect(current) {
    const m = getModel();
    if (!m?.snapshot || socket || current !== epoch || stopped) return;
    const url = new URL(`/v1/plans/${m.plan}/network-stream`, location.origin);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const active = new WebSocket(url);
    socket = active;
    active.onopen = () => {
      if (current === epoch) lastMessage = Date.now();
    };
    active.onmessage = (event) => {
      if (current !== epoch || active !== socket) return;
      lastMessage = Date.now();
      try {
        const frame = JSON.parse(event.data);
        if (frame.type === "snapshot") accept(frame.data, current);
        else if (frame.type === "error") {
          data = null;
          selected = null;
          status = "네트워크 관측을 확인할 수 없습니다.";
          draw();
        }
      } catch {
        data = null;
        selected = null;
        status = "관측 형식을 확인할 수 없습니다.";
        draw();
      }
    };
    active.onerror = () => {
      if (current === epoch) {
        data = null;
        selected = null;
        status = "네트워크 연결을 확인할 수 없습니다.";
        draw();
      }
    };
    active.onclose = (event) => {
      if (current !== epoch || active !== socket) return;
      socket = null;
      if ([4401, 4403].includes(event.code)) {
        deny();
        return;
      }
      retry = setTimeout(() => {
        retry = null;
        connect(current);
      }, 5000);
    };
  }
  function render() {
    const m = getModel(),
      snapshot = m?.snapshot;
    const next =
      snapshot && m.connected && m.error !== "forbidden"
        ? JSON.stringify([
            m.plan,
            snapshot.plan_digest,
            snapshot.version,
            snapshot.source_environment_id,
            snapshot.target_environment_id,
          ])
        : null;
    if (!next) {
      if (key || data) clear();
      return;
    }
    if (next !== key) {
      stop();
      data = null;
      selected = null;
      denied = false;
      key = next;
      status = "관측 자료 연결 중";
      draw();
    }
    if (!host.hidden && !socket && !retry && !stopped && !denied) {
      void load(epoch);
      connect(epoch);
    }
  }
  root.defaultView?.addEventListener("sova-view-changed", () => {
    if (host.hidden) {
      stop();
      key = null;
    } else {
      render();
      if (tab === "map") requestAnimationFrame(fitGraph);
      if (tab === "diagrams") requestAnimationFrame(imageFit);
    }
  });
  root.defaultView?.addEventListener("resize", () => {
    if (!host.hidden) {
      fitGraph();
      imageFit();
    }
  });
  const timer = setInterval(() => {
    if (socket && lastMessage && Date.now() - lastMessage > 45000) {
      stop();
      data = null;
      selected = null;
      status = "네트워크 관측 수신이 만료됐습니다.";
      draw();
      retry = setTimeout(() => {
        retry = null;
        connect(epoch);
      }, 5000);
    }
    if (data && !host.hidden) draw();
  }, 10000);
  root.defaultView?.addEventListener("pagehide", () => {
    stopped = true;
    clearInterval(timer);
    stop();
  });
  show("map");
  draw();
  return { render, clear };
}
