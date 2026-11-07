import { request } from "./transport.js";
import { identifier, failbackBinding } from "./model.js";

export const FEATURES = [
  "inventory",
  "recommendations",
  "exit",
  "costs",
  "ai",
  "rehearsal",
  "traffic",
  "monitoring",
  "audit",
];
export const ACTIONS = [
  "shift",
  "failback-approve",
  "failback-submit",
  "failback-reconcile",
  "failback-edge",
  "rehearsal-start",
  "rehearsal-reconcile",
  "initial-start",
  "initial-collect",
];
const states = {
  OK: "조회됨 · 실행 허가 아님",
  UNKNOWN: "확인 불가",
  UNCONFIGURED: "서버 연결 필요",
  FORBIDDEN: "조회 권한 없음",
};
const plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v, size = 512) => typeof v === "string" && v.length <= size;
const hash = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
export const canonical = (value) =>
  JSON.stringify(value, (_key, item) =>
    plain(item)
      ? Object.fromEntries(
          Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
        )
      : item,
  );

export function currentPanels(value, now) {
  if (!value) return value;
  let features = value.features;
  for (const name of FEATURES) {
    const item = features[name];
    if (
      item.state === "OK" &&
      item.valid_until !== undefined &&
      now >= Date.parse(item.valid_until)
    ) {
      features = {
        ...features,
        [name]: { state: "UNKNOWN", reason: "source_expired", tables: [] },
      };
    }
  }
  return features === value.features ? value : { ...value, features };
}

export function panelDocument(value, snapshot, now) {
  if (
    !plain(value) ||
    value.kind !== "console-workspace.v1" ||
    value.authorizes_cutover !== false ||
    value.plan_id !== snapshot.plan_id ||
    value.plan_digest !== snapshot.plan_digest ||
    value.plan_version !== snapshot.version ||
    !Number.isFinite(Date.parse(value.queried_at)) ||
    !Number.isFinite(Date.parse(value.expires_at)) ||
    now < Date.parse(value.queried_at) ||
    now >= Date.parse(value.expires_at) ||
    Date.parse(value.expires_at) - Date.parse(value.queried_at) > 15000 ||
    !plain(value.features) ||
    Object.keys(value.features).sort().join() !== [...FEATURES].sort().join()
  )
    throw Error("invalid_workspace");
  let cells = 0;
  for (const feature of FEATURES) {
    const item = value.features[feature];
    if (
      item.valid_until !== undefined &&
      (typeof item.valid_until !== "string" ||
        !Number.isFinite(Date.parse(item.valid_until)))
    )
      throw Error("invalid_panel_expiry");
    if (
      !plain(item) ||
      !Object.hasOwn(states, item.state) ||
      !Array.isArray(item.tables) ||
      item.tables.length > 10 ||
      (item.state !== "OK" && item.tables.length) ||
      (item.source_revision !== undefined &&
        !/^[a-f0-9]{40}$/.test(item.source_revision)) ||
      (item.content_digest !== undefined && !hash(item.content_digest))
    )
      throw Error("invalid_workspace_feature");
    for (const table of item.tables) {
      if (
        !plain(table) ||
        !text(table.title, 160) ||
        !Array.isArray(table.columns) ||
        !table.columns.length ||
        table.columns.length > 12 ||
        !table.columns.every((v) => text(v, 80)) ||
        !Array.isArray(table.rows) ||
        table.rows.length > 200
      )
        throw Error("invalid_workspace_table");
      for (const row of table.rows) {
        if (
          !Array.isArray(row) ||
          row.length !== table.columns.length ||
          !row.every((v) => text(v)) ||
          (cells += row.length) > 5000
        )
          throw Error("invalid_workspace_cell");
      }
    }
  }
  if (value.operations !== null) {
    const ops = value.operations;
    if (
      !plain(ops) ||
      !plain(ops.actions) ||
      Object.keys(ops.actions).sort().join() !== [...ACTIONS].sort().join() ||
      !Object.values(ops.actions).every((v) => typeof v === "boolean") ||
      !Array.isArray(ops.rehearsal_tasks) ||
      ops.rehearsal_tasks.length > 32 ||
      !ops.rehearsal_tasks.every(identifier) ||
      new Set(ops.rehearsal_tasks).size !== ops.rehearsal_tasks.length
    )
      throw Error("invalid_operations_context");
    if (
      ops.failback_command_id !== undefined &&
      ops.failback_command_id !== null &&
      !identifier(ops.failback_command_id)
    )
      throw Error("invalid_failback_command_context");
    if (
      ops.rehearsal_runs !== undefined &&
      (!Array.isArray(ops.rehearsal_runs) ||
        ops.rehearsal_runs.length > 32 ||
        ops.rehearsal_runs.some(
          (run) =>
            !plain(run) ||
            !identifier(run.rehearsal_id) ||
            !identifier(run.task_id) ||
            ![1, 2, 3].includes(run.attempt),
        ))
    )
      throw Error("invalid_rehearsal_context");
    if (ops.failback_binding !== null) {
      const binding = failbackBinding(ops.failback_binding, snapshot.plan_id);
      if (
        binding.plan_digest !== snapshot.plan_digest ||
        binding.source_environment_id !== snapshot.target_environment_id ||
        binding.target_environment_id !== snapshot.source_environment_id ||
        now >= Date.parse(binding.expires_at)
      )
        throw Error("invalid_failback_context");
    }
  }
  return currentPanels(structuredClone(value), now);
}

export function mountPanels(
  { getModel, onForbidden, onUpdate },
  root = document,
) {
  let value = null,
    lastAttempt = 0,
    busy = false,
    epoch = 0,
    lastRendered = Symbol();
  const byId = (id) => root.getElementById(id);
  function current(now = Date.now()) {
    const model = getModel();
    value = currentPanels(value, now);
    return model?.snapshot &&
      value?.plan_id === model.plan &&
      value.plan_digest === model.snapshot.plan_digest &&
      value.plan_version === model.snapshot.version &&
      now < Date.parse(value.expires_at)
      ? value
      : null;
  }
  function render() {
    const live = current();
    if (live === lastRendered) return;
    lastRendered = live;
    for (const name of FEATURES) {
      const item = live?.features[name],
        status = byId("panel-" + name + "-status"),
        content = byId("panel-" + name + "-body");
      status.textContent = item
        ? states[item.state] +
          (item.alerts_state === "UNOBSERVABLE" ? " · 경보 조회 불가" : "")
        : "현재 계획의 자료를 기다립니다.";
      const nodes = [];
      for (const block of item?.tables ?? []) {
        const title = root.createElement("h3"),
          scroll = root.createElement("div"),
          table = root.createElement("table"),
          head = root.createElement("thead"),
          tr = root.createElement("tr"),
          body = root.createElement("tbody");
        title.textContent = block.title;
        scroll.className = "table-scroll";
        scroll.tabIndex = 0;
        scroll.setAttribute("role", "region");
        scroll.setAttribute("aria-label", block.title);
        table.className = "control-table";
        for (const label of block.columns) {
          const th = root.createElement("th");
          th.scope = "col";
          th.textContent = label;
          tr.append(th);
        }
        head.append(tr);
        for (const row of block.rows) {
          const tr = root.createElement("tr");
          for (const cell of row) {
            const td = root.createElement("td");
            td.textContent = cell;
            tr.append(td);
          }
          body.append(tr);
        }
        table.append(head, body);
        scroll.append(table);
        nodes.push(title, scroll);
        if (!block.rows.length) {
          const empty = root.createElement("p");
          empty.className = "muted";
          empty.textContent =
            "표시할 행이 없습니다. 수집 상태와 적용 범위를 함께 확인하세요.";
          nodes.push(empty);
        }
      }
      if (item?.content_digest) {
        const evidence = root.createElement("p");
        evidence.className = "muted evidence-digest";
        evidence.textContent = "원문 digest: " + item.content_digest;
        nodes.push(evidence);
      }
      content.replaceChildren(...nodes);
    }
  }
  return {
    current,
    render,
    clear() {
      epoch++;
      value = null;
      lastAttempt = 0;
      busy = false;
      render();
    },
    async refresh(force = false) {
      const model = getModel();
      if (
        !model?.snapshot ||
        busy ||
        (!force && current() && Date.now() - lastAttempt < 10000)
      )
        return;
      const captured = model,
        version = model.snapshot.version,
        token = epoch;
      busy = true;
      lastAttempt = Date.now();
      try {
        const raw = await request(`/v1/plans/${model.plan}/workspace`, {
          signal: AbortSignal.timeout(12000),
        });
        if (
          getModel() !== captured ||
          token !== epoch ||
          captured.snapshot.version !== version
        )
          return;
        value = panelDocument(raw, captured.snapshot, Date.now());
      } catch (error) {
        if (getModel() !== captured || token !== epoch) return;
        value = null;
        onForbidden(error);
      } finally {
        if (token === epoch) {
          busy = false;
          render();
          onUpdate();
        }
      }
    },
  };
}
