import {
  Model,
  STATES,
  identifier,
  observation,
  controlReport,
  CONTROL_TESTS,
  failbackSummary,
} from "./model.js";
import { request, stream } from "./transport.js";
import { recoveryCommand, canResubmit } from "./recovery.js";
import { mountRegistration } from "./registration.js";
const $ = (id) => document.getElementById(id);
let model,
  generation = 0,
  stopped = false,
  streamController,
  postController;
let observed = null;
let controls = null;
let pending = null,
  commandId = null,
  lastHeartbeat = 0,
  refreshBusy = false,
  refreshTimer;
const names = {
  COLLECT: "조사",
  ANALYZE: "분석",
  PROPOSE: "제안",
  REHEARSE: "리허설",
  GATE: "게이트",
  AWAIT_APPROVAL: "승인 대기",
  SHIFT: "트래픽 전환",
  CUTOVER: "DB 전환",
  WATCH: "관측",
  FAILBACK: "복귀",
  SAFE_HOLD: "안전 보류",
  NEEDS_HUMAN: "담당자 확인",
  DONE: "완료",
  DONE_ROLLED_BACK: "출발지 복귀",
};
const messages = {
  forbidden: "접근 권한 또는 로그인 상태를 확인해 주세요.",
  authority_unavailable: "서버가 현재 승인 근거를 확인할 수 없습니다.",
  binding_mismatch: "승인 대상이 바뀌었습니다. 현재 계획을 다시 조회해 주세요.",
  version_conflict: "계획 버전이 바뀌었습니다. 다시 조회해 주세요.",
  approval_stale: "기존 승인은 현재 계획에 사용할 수 없습니다.",
  approval_expired: "승인 유효기간이 지났습니다.",
  resync_required: "이벤트 재동기화가 필요합니다. 계획을 다시 연결해 주세요.",
};
const registration = mountRegistration({
  onPlan: (id) => {
    $("plan").value = id;
    $("connect-form").requestSubmit();
  },
  onForbidden: revokeView,
});
function text(id, value) {
  $(id).textContent = value;
}
function notice(value, warning = false) {
  text("notice", value);
  $("notice").classList.toggle("warning", warning);
}
function details(id, values) {
  const nodes = [];
  for (const [k, v] of values) {
    const dt = document.createElement("dt"),
      dd = document.createElement("dd");
    dt.textContent = k;
    dd.textContent = String(v);
    nodes.push(dt, dd);
  }
  $(id).replaceChildren(...nodes);
}
function render() {
  if (!model) return;
  const now = Date.now(),
    eligible = model.eligible(now),
    changeUsable = model.changeEligible(now) && !pending,
    usable =
      eligible &&
      (!pending ||
        (pending.retryReady &&
          !pending.running &&
          canResubmit(model, pending, now)));
  text(
    "connection",
    model.connected ? "서버 이벤트 연결됨" : "연결 끊김 · 실행 잠금",
  );
  text("state", model.state ? names[model.state] : "미확인");
  text("workload", model.snapshot?.workload_id ?? "—");
  text(
    "version",
    model.snapshot ? `계획 버전 ${model.snapshot.version}` : "계획 미확인",
  );
  text(
    "updated",
    model.snapshotAt
      ? `조회 ${new Date(model.snapshotAt).toLocaleTimeString("ko-KR")}`
      : "서버 응답 대기",
  );
  text("source", model.snapshot?.source_environment_id ?? "미연결");
  text("target", model.snapshot?.target_environment_id ?? "미연결");
  for (const side of ["source", "target"]) {
    const row = observed?.rows.find((r) => r.side === side);
    const fresh = observed && now - observed.at < 15000;
    text(
      side + "-http",
      fresh && row?.state === "OK"
        ? `${row.error_ratio === null ? "미확인" : (row.error_ratio * 100).toFixed(2) + "%"} / ${row.p95_seconds === null ? "미확인" : (row.p95_seconds * 1000).toFixed(0) + " ms"}`
        : "미확인",
    );
  }
  const reportFresh =
    controls &&
    now - controls.at < 15000 &&
    controls.report.gate_id === model.gate?.data.gate_id &&
    controls.report.context_digest === model.gate?.data.context_digest;
  text(
    "control-status",
    reportFresh
      ? `${controls.report.profile.id} · 기록된 보안 판정 ${controls.report.decision}`
      : "검증된 보고서 미연결",
  );
  text(
    "control-time",
    reportFresh
      ? `평가 ${new Date(controls.report.evaluated_at).toLocaleString("ko-KR")} · 조회 ${new Date(controls.report.queried_at).toLocaleTimeString("ko-KR")}`
      : "현재 계획·게이트와 일치하는 서버 보고서를 기다립니다.",
  );
  const controlRows = reportFresh
    ? controls.report.rows.map((row) => {
        const tr = document.createElement("tr");
        for (const value of [
          `${row.test_id} · ${CONTROL_TESTS[row.test_id]}`,
          row.source_verdict,
          row.target_verdict,
          row.decision,
          row.reason + (row.exception_applied ? " · 예외 적용" : ""),
        ]) {
          const td = document.createElement("td");
          td.textContent = value;
          tr.append(td);
        }
        if (row.decision !== "PASS") tr.className = "blocked-row";
        return tr;
      })
    : [];
  $("control-rows").replaceChildren(...controlRows);
  text(
    "control-issues",
    reportFresh && controls.report.run_issues.length
      ? `실행 묶음 문제: ${controls.report.run_issues.join(", ")}`
      : "",
  );
  text("failback-status", failbackSummary(model.failback?.data));
  details(
    "failback-detail",
    model.failback
      ? [
          ["명령 ID", model.failback.data.command_id],
          ["승인 ID", model.failback.data.approval_id],
          ["승인자", model.failback.data.approved_by],
          ["실행 ID", model.failback.data.execution_id],
          [
            "무결성 근거",
            model.failback.data.integrity_receipt_id ??
              "해당 없음 (쓰기 전 중단)",
          ],
          ["기록 시각", model.failback.occurred_at],
        ]
      : [],
  );
  text("cursor", `확인한 이벤트 ${model.cursor}건`);
  text("gate-status", model.gate?.data.decision ?? "미확인");
  const phases = STATES.map((s) => {
    const li = document.createElement("li");
    li.textContent = names[s];
    li.className = s === model.state ? "current" : "";
    return li;
  });
  $("phases").replaceChildren(...phases);
  const b = model.request?.data.binding;
  if (b && model.request.data.approval_kind === "failback")
    details("binding", [
      ["승인 종류", "복귀 (조회 전용)"],
      ["계획 digest", b.plan_digest],
      [
        "복귀 출발 → 도착",
        `${b.source_environment_id} → ${b.target_environment_id}`,
      ],
      ["기존 전환 실행", b.cutover_execution_id],
      ["기존 전환 epoch", b.cutover_epoch],
      ["무결성 근거", b.integrity_receipt_id],
      ["만료 시각", b.expires_at],
    ]);
  else if (b)
    details("binding", [
      ["승인 종류", model.request.data.approval_kind],
      [
        "리허설 실행",
        model.request.data.approval_kind === "change"
          ? model.request.run_id
          : "해당 없음",
      ],
      ["계획 digest", b.plan_digest ?? "없음"],
      ["목적 환경", b.target_environment_id ?? "없음"],
      ["이미지 digest", b.image_digest ?? "없음"],
      ["변경 hash", b.diff_hash ?? "없음"],
      [
        "게이트 · context",
        `${b.gate_id ?? "없음"} / ${b.context_digest ?? "없음"}`,
      ],
      ["만료 시각", b.expires_at ?? "없음"],
    ]);
  else details("binding", [["승인 대상", "서버의 승인 요청 대기"]]);
  if (model.gate)
    details("gate-detail", [
      ["게이트 ID", model.gate.data.gate_id],
      ["증거 참조", model.gate.data.evidence_ref],
      ["기록 시각", model.gate.occurred_at],
    ]);
  else $("gate-detail").replaceChildren();
  $("confirm").disabled = !usable && !changeUsable;
  if (!usable && !changeUsable) $("confirm").checked = false;
  $("approve-change").disabled = !changeUsable || !$("confirm").checked;
  $("approve").disabled =
    !usable ||
    Boolean(pending) ||
    !$("confirm").checked ||
    Boolean(model.approval);
  $("execute").disabled =
    !usable || (!pending && !model.approval) || !$("confirm").checked;
  $("retry").hidden =
    !pending || pending.running || model.error === "forbidden";
  $("reconcile").hidden = !commandId || Boolean(pending);
  $("download").disabled = !model.events.length;
  const history = model.events
    .slice(-100)
    .reverse()
    .map((e) => {
      const li = document.createElement("li"),
        when = document.createElement("time"),
        title = document.createElement("b"),
        desc = document.createElement("span");
      when.textContent = new Date(e.occurred_at).toLocaleTimeString("ko-KR");
      title.textContent = e.event_type;
      desc.textContent =
        e.event_type === "state.changed"
          ? `${e.data.from ?? "시작"} → ${e.data.to}`
          : e.event_type === "gate.completed"
            ? `${e.data.decision} · ${e.data.gate_id}`
            : e.event_type === "failback.recorded"
              ? failbackSummary(e.data)
              : e.event_type.startsWith("approval.")
                ? `${e.data.approval_kind} · ${e.data.decision ?? "요청"} · ${e.data.approval_id}`
                : `${e.data.step} · ${e.data.status}${e.data.elapsed_ms === undefined ? "" : ` · ${e.data.elapsed_ms} ms`}`;
      if (
        ["BLOCK", "failed", "unknown"].some((v) =>
          Object.values(e.data).includes(v),
        )
      )
        li.className = "bad";
      li.append(when, title, desc);
      return li;
    });
  $("history").replaceChildren(...history);
}
function revokeView(error) {
  if (![401, 403].includes(error.status)) return false;
  // Invalidate every outstanding read before erasing protected data. A late
  // stream heartbeat or snapshot must not restore a revoked session's view.
  generation++;
  stopped = true;
  streamController?.abort();
  clearInterval(refreshTimer);
  registration.revoke();
  model = new Model(model?.plan ?? "unconnected");
  model.error = "forbidden";
  observed = null;
  controls = null;
  commandId = null;
  notice(messages.forbidden, true);
  text(
    "action-status",
    pending
      ? "요청 결과는 미확인입니다. 다시 인증한 뒤 서버 실행 기록을 확인하세요."
      : "",
  );
  render();
  return true;
}
async function refresh(g) {
  if (refreshBusy || !model) return;
  refreshBusy = true;
  try {
    const value = await request(`/v1/plans/${model.plan}`, {
      signal: AbortSignal.timeout(8000),
    });
    if (g !== generation) return;
    model.observeSnapshot(value, Date.now());
    const [metrics, report] = await Promise.allSettled([
      request(`/v1/plans/${model.plan}/observations`, {
        signal: AbortSignal.timeout(7000),
      }),
      request(`/v1/plans/${model.plan}/controls`, {
        signal: AbortSignal.timeout(7000),
      }),
    ]);
    if (g !== generation) return;
    for (const result of [metrics, report]) {
      if (result.status === "rejected" && revokeView(result.reason)) return;
    }
    try {
      observed =
        metrics.status === "fulfilled"
          ? {
              rows: observation(metrics.value, model.snapshot, Date.now()),
              at: Date.now(),
            }
          : null;
    } catch {
      observed = null;
    }
    try {
      controls =
        report.status === "fulfilled"
          ? {
              report: controlReport(
                report.value,
                model.snapshot,
                model.gate,
                Date.now(),
              ),
              at: Date.now(),
            }
          : null;
    } catch {
      controls = null;
    }
    return true;
  } catch (error) {
    if (g !== generation || revokeView(error)) return false;
    model.connected = false;
    notice(
      messages[error.code] ??
        "현재 계획을 확인할 수 없습니다. 변경 동작을 잠갔습니다.",
      true,
    );
  } finally {
    if (g === generation) {
      refreshBusy = false;
      render();
    }
  }
}
async function follow(g) {
  let attempt = 0;
  while (g === generation && !stopped) {
    streamController = new AbortController();
    try {
      await stream(model.plan, model.cursor, {
        signal: streamController.signal,
        onHeartbeat: () => {
          if (g !== generation) return;
          lastHeartbeat = Date.now();
          model.connected = true;
          render();
        },
        onEvent: (e) => {
          if (g !== generation) return;
          model.accept(e);
          lastHeartbeat = Date.now();
          model.connected = true;
          render();
        },
      });
      if (g !== generation) return;
      model.connected = false;
    } catch (error) {
      if (g !== generation || revokeView(error)) return;
      model.connected = false;
      if (
        error.name !== "AbortError" &&
        !(error instanceof TypeError) &&
        !(error.status >= 500)
      ) {
        model.error = error.code ?? error.message;
        notice(
          messages[error.code] ??
            "서버 이력이나 접근 권한을 확인할 수 없습니다. 계획을 다시 연결해 주세요.",
          true,
        );
        render();
        return;
      }
    }
    render();
    await new Promise((resolve) =>
      setTimeout(resolve, Math.min(1000 * 2 ** attempt++, 10000)),
    );
    if (g === generation) await refresh(g);
  }
}
async function connectPlan(plan, recovery = false) {
  if (pending && ((pending.running && !recovery) || pending.plan !== plan)) {
    notice(
      "결과가 확인되지 않은 요청이 있습니다. 먼저 같은 요청의 결과를 확인해 주세요.",
      true,
    );
    return;
  }
  if (!identifier(plan)) return false;
  if (pending) pending.retryReady = false;
  generation++;
  const g = generation;
  stopped = false;
  streamController?.abort();
  clearInterval(refreshTimer);
  model = new Model(plan);
  observed = null;
  controls = null;
  commandId = null;
  lastHeartbeat = 0;
  refreshBusy = false;
  render();
  notice("등록된 계획과 서버 이력을 조회하고 있습니다.");
  await refresh(g);
  if (g !== generation || !model.snapshot) return;
  // Replay is bounded. Without the missing approval history, changes stay locked.
  try {
    await stream(plan, 0, {
      signal: AbortSignal.timeout(15000),
      follow: false,
      onEvent: (e) => {
        if (g === generation) model.accept(e);
      },
    });
  } catch (error) {
    if (g !== generation || revokeView(error)) return;
    model.error = error.code ?? "history_unavailable";
    notice("서버 이력을 완전히 확인할 수 없어 실행을 잠갔습니다.", true);
    render();
    return;
  }
  if (g !== generation) return;
  // Read a fresh snapshot after the complete replay. A concurrent event that
  // was not replayed leaves the model locked until the follow stream catches up.
  const refreshed = await refresh(g);
  if (!refreshed || g !== generation || model.error || !model.snapshot)
    return false;
  model.connected = true;
  lastHeartbeat = Date.now();
  notice("서버 기록을 연결했습니다. 최종 실행 권한은 서버가 다시 확인합니다.");
  refreshTimer = setInterval(() => refresh(g), 5000);
  follow(g);
  return true;
}
$("connect-form").onsubmit = async (e) => {
  e.preventDefault();
  await connectPlan($("plan").value.trim());
};
$("confirm").onchange = render;
async function perform() {
  if (!pending || pending.running || model.error === "forbidden") return;
  const g = generation,
    current = pending;
  current.running = true;
  current.retryReady = false;
  render();
  text("action-status", "서버 응답을 확인하고 있습니다.");
  postController = new AbortController();
  const timeout = setTimeout(() => postController.abort(), 12000);
  try {
    const result = await request(current.path, {
      body: current.body,
      key: current.key,
      signal: postController.signal,
    });
    if (g !== generation) return;
    if (current.kind === "approve-change") {
      model.recordChangeApproval(result, current.body);
      text(
        "action-status",
        `코드 변경 승인 기록 완료 · ${result.approval_id}. 배포·전환을 실행한 것은 아닙니다.`,
      );
    } else if (current.kind === "approve") {
      model.recordApproval(result, current.body);
      text(
        "action-status",
        `승인 기록 완료 · ${result.approval_id}. 전환 실행은 별도입니다.`,
      );
    } else {
      if (
        !identifier(result.command_id) ||
        !["OBSERVED", "UNKNOWN"].includes(result.outcome)
      )
        throw new Error("invalid_execution_receipt");
      commandId = result.command_id;
      text(
        "action-status",
        result.outcome === "UNKNOWN"
          ? "실행 결과 미확인. 재실행하지 않고 서버의 실행 상태를 조회합니다."
          : "실행 접수를 확인했습니다. 완료 여부는 서버 이력에서 확인합니다.",
      );
      model.approval = null;
    }
    pending = null;
    await refresh(generation);
  } catch (error) {
    if (g !== generation || revokeView(error)) return;
    // No automatic mutation retry. Same request and idempotency key remain frozen.
    if ([400, 401, 403, 409, 422].includes(error.status)) {
      pending = null;
      model.approval = null;
      text(
        "action-status",
        messages[error.code] ??
          "서버가 요청을 거부했습니다. 현재 계획을 확인해 주세요.",
      );
      await refresh(generation);
    } else {
      text(
        "action-status",
        "응답을 확인하지 못했습니다. 성공·실패를 추정하지 않습니다. 아래에서 같은 요청의 결과를 확인하세요.",
      );
    }
  } finally {
    clearTimeout(timeout);
    current.running = false;
    render();
  }
}
$("approve-change").onclick = async () => {
  if (pending || !$("confirm").checked || !model?.changeEligible(Date.now()))
    return;
  pending = {
    kind: "approve-change",
    plan: model.plan,
    path: `/v1/plans/${model.plan}/approvals`,
    body: model.changeApprovalRequest(Date.now()),
    key: crypto.randomUUID(),
    running: false,
  };
  await perform();
};
$("approve").onclick = async () => {
  if (pending || !$("confirm").checked || !model?.eligible(Date.now())) return;
  pending = {
    kind: "approve",
    plan: model.plan,
    path: `/v1/plans/${model.plan}/approvals`,
    body: model.approvalRequest(Date.now()),
    key: crypto.randomUUID(),
    running: false,
  };
  await perform();
};
$("execute").onclick = async () => {
  if (
    (pending &&
      (!pending.retryReady || !canResubmit(model, pending, Date.now()))) ||
    !$("confirm").checked ||
    !model?.eligible(Date.now()) ||
    (!pending && !model.approval)
  )
    return;
  if (
    !window.confirm(
      "승인된 계획의 실제 전환을 요청합니다. 서버가 현재 권한과 근거를 재검증합니다. 진행하시겠습니까?",
    )
  )
    return;
  if (
    !model.eligible(Date.now()) ||
    (pending && !canResubmit(model, pending, Date.now()))
  )
    return;
  pending ??= {
    kind: "execute",
    plan: model.plan,
    snapshot: structuredClone(model.snapshot),
    approval: structuredClone(model.approval),
    cursor: model.cursor,
    path: `/v1/plans/${model.plan}/cutover`,
    body: { approval_id: model.approval.approval_id },
    key: crypto.randomUUID(),
    running: false,
  };
  await perform();
};
async function recoverPending() {
  if (!pending || pending.running || model.error === "forbidden") return;
  if (pending.kind !== "execute") return perform();
  const current = pending;
  current.running = true;
  current.retryReady = false;
  render();
  text(
    "action-status",
    "계획·이벤트 이력을 읽어 기존 접수를 확인하고 있습니다.",
  );
  try {
    if (!(await connectPlan(current.plan, true)) || pending !== current) return;
    const id = recoveryCommand(model, current);
    if (id) {
      // A plan event identifies an existing dispatch claim. Reconcile can only
      // read it; never repeat the submit endpoint while checking its result.
      commandId = id;
      current.kind = "reconcile";
      current.path = `/v1/plans/${current.plan}/cutover/${id}/reconcile`;
      current.body = {};
      current.key = crypto.randomUUID();
      current.running = false;
      return await perform();
    }
    current.retryReady = canResubmit(model, current, Date.now());
    text(
      "action-status",
      current.retryReady
        ? "확인한 이력에 접수 기록이 없습니다. 대상·유효기간을 다시 확인하고 ‘승인된 전환 실행’을 눌러야 같은 요청을 전송합니다. 결과 확인만으로 실행하지 않습니다."
        : "접수 여부 또는 현재 승인 근거를 확인할 수 없어 실행을 잠갔습니다. 같은 계획을 다시 연결하거나 결과 확인을 다시 시도하세요.",
    );
  } catch (error) {
    if (pending !== current || revokeView(error)) return;
    text(
      "action-status",
      "접수 여부를 확인할 수 없어 실행을 잠갔습니다. 계획·이벤트를 재동기화한 뒤 다시 확인하세요.",
    );
  } finally {
    current.running = false;
    render();
  }
}
$("retry").onclick = recoverPending;
$("reconcile").onclick = async () => {
  if (!commandId || pending) return;
  pending = {
    kind: "reconcile",
    plan: model.plan,
    path: `/v1/plans/${model.plan}/cutover/${commandId}/reconcile`,
    body: {},
    key: crypto.randomUUID(),
    running: false,
  };
  await perform();
};
$("download").onclick = () => {
  if (!model?.events.length) return;
  const blob = new Blob(
    [
      JSON.stringify(
        {
          kind: "browser-view-only",
          plan: model.plan,
          queried_at: new Date().toISOString(),
          events: model.events,
        },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = "console-observed-events.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
};
setInterval(() => {
  if (!model) return;
  if (model.connected && Date.now() - lastHeartbeat > 15000) {
    model.connected = false;
    streamController?.abort();
    notice("이벤트 응답이 오래되어 실행을 잠갔습니다.", true);
  }
  render();
}, 1000);
window.addEventListener("offline", () => {
  if (model) {
    model.connected = false;
    streamController?.abort();
    render();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (pending) {
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener("pagehide", () => {
  stopped = true;
  generation++;
  clearInterval(refreshTimer);
  streamController?.abort();
  postController?.abort();
});
