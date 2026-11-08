import { request } from "./transport.js";
import { identifier, failbackBinding } from "./model.js";
import { canonical, ACTIONS } from "./panels.js";

const dates = (value) =>
  typeof value === "string" && Number.isFinite(Date.parse(value));
export function ready(model, now) {
  return Boolean(
    model?.snapshot &&
      model.connected &&
      !model.error &&
      model.state === model.snapshot.state &&
      now >= model.snapshotAt &&
      now - model.snapshotAt < 15000 &&
      model.snapshot.last_event_id === `${model.plan}:${model.cursor}`,
  );
}
export function operationReceipt(action, receipt, pending) {
  if (!receipt || typeof receipt !== "object" || Array.isArray(receipt))
    throw Error("invalid_operation_receipt");
  if (action === "shift") {
    if (
      receipt.approval_kind !== "shift" ||
      !identifier(receipt.approval_id) ||
      !identifier(receipt.actor_id) ||
      !dates(receipt.approved_at) ||
      receipt.expires_at !== pending.body.expires_at
    )
      throw Error("invalid_shift_receipt");
  } else if (action === "failback-approve") {
    if (
      receipt.approval_kind !== "failback" ||
      !identifier(receipt.approval_id) ||
      !identifier(receipt.actor_id) ||
      !dates(receipt.approved_at) ||
      canonical(failbackBinding(receipt.binding, pending.plan)) !==
        canonical(pending.body.binding)
    )
      throw Error("invalid_failback_receipt");
  } else if (
    ["failback-submit", "failback-reconcile", "failback-edge"].includes(action)
  ) {
    if (receipt.snapshot && receipt.snapshot.plan_id !== pending.plan)
      throw Error("foreign_failback_snapshot");
    if (
      !identifier(receipt.command_id) ||
      (pending.commandId && receipt.command_id !== pending.commandId)
    )
      throw Error("invalid_failback_command");
  } else if (action.startsWith("rehearsal-")) {
    if (
      !identifier(receipt.rehearsal_id) ||
      receipt.attempt !== pending.attempt ||
      (receipt.plan_version !== undefined &&
        receipt.plan_version !==
          (pending.admissionVersion ?? pending.version)) ||
      (pending.rehearsalId && receipt.rehearsal_id !== pending.rehearsalId) ||
      (receipt.task_id !== undefined && receipt.task_id !== pending.taskId)
    )
      throw Error("invalid_rehearsal_receipt");
  } else if (action.startsWith("initial-")) {
    if (
      !["DONE", "PENDING", "HOLD", "FAILED", "UNKNOWN"].includes(
        receipt.status,
      ) ||
      (receipt.request_id !== undefined && !identifier(receipt.request_id))
    )
      throw Error("invalid_initial_receipt");
  } else throw Error("invalid_operation");
  return structuredClone(receipt);
}

export function mountOperations(
  { getModel, getPanel, mainPending, setBusy, refresh, onForbidden },
  root = document,
) {
  const $ = (id) => root.getElementById(id);
  let pending = null,
    approved = null,
    commandId = null,
    rehearsal = null,
    receiptPlan = null,
    active = false,
    contextKey = null,
    epoch = 0;
  const status = (message) => {
    $("operation-status").textContent = message;
  };
  const snapshotKey = (model) =>
    canonical({
      plan: model.plan,
      digest: model.snapshot.plan_digest,
      version: model.snapshot.version,
      source: model.snapshot.source_environment_id,
      target: model.snapshot.target_environment_id,
    });
  function configured(action) {
    const model = getModel(),
      panel = getPanel();
    return (
      ready(model, Date.now()) &&
      panel?.operations?.actions[action] === true &&
      (!["failback-approve", "failback-submit"].includes(action) ||
        Date.parse(panel.operations.failback_binding?.expires_at) > Date.now())
    );
  }
  function render() {
    const model = getModel(),
      panel = getPanel(),
      context = panel?.operations;
    const key = canonical({
      plan: model?.plan,
      version: model?.snapshot?.version,
      context,
    });
    if (key !== contextKey) {
      contextKey = key;
      $("operations-confirm").checked = false;
    }
    if (receiptPlan && receiptPlan !== model?.plan) {
      approved = null;
      commandId = null;
      rehearsal = null;
      receiptPlan = null;
    }
    if (
      context?.failback_command_id &&
      model?.snapshot &&
      configured("failback-reconcile")
    ) {
      commandId = context.failback_command_id;
      receiptPlan = model.plan;
    }
    const runs = context?.rehearsal_runs ?? [];
    const selectedRun = $("rehearsal-run-select").value;
    const runKeys = runs.map((run) => `${run.rehearsal_id}:${run.attempt}`);
    if (
      canonical(
        [...$("rehearsal-run-select").options].map((option) => option.value),
      ) !== canonical(runKeys)
    ) {
      $("rehearsal-run-select").replaceChildren(
        ...runs.map((run) => {
          const option = root.createElement("option");
          option.value = `${run.rehearsal_id}:${run.attempt}`;
          option.textContent = `${run.task_id} · ${run.rehearsal_id} · 시도 ${run.attempt}`;
          return option;
        }),
      );
      if (runKeys.includes(selectedRun))
        $("rehearsal-run-select").value = selectedRun;
    }
    if (!rehearsal && runs.length && configured("rehearsal-reconcile")) {
      const run = runs.find(
        (run) =>
          `${run.rehearsal_id}:${run.attempt}` ===
          $("rehearsal-run-select").value,
      );
      rehearsal = {
        id: run.rehearsal_id,
        attempt: run.attempt,
        task: run.task_id,
        version: run.plan_version ?? model.snapshot.version,
      };
      receiptPlan = model.plan;
    }
    const usable = ready(model, Date.now()) && context;
    $("operation-context").textContent = usable
      ? `계획 버전 ${model.snapshot.version} · 최종 권한과 근거는 서버에서 재검증합니다.`
      : "현재 계획의 운영 연결을 확인해야 조작할 수 있습니다.";
    const binding = context?.failback_binding;
    $("failback-binding-view").textContent = binding
      ? `복귀 ${binding.source_environment_id} → ${binding.target_environment_id}\n컷오버 실행 ${binding.cutover_execution_id} / epoch ${binding.cutover_epoch}\n무결성 ${binding.integrity_receipt_id}\n만료 ${binding.expires_at}`
      : "서버가 검증한 현재 복귀 대상을 기다립니다.";
    const tasks = context?.rehearsal_tasks ?? [];
    if (
      canonical([...$("rehearsal-task").options].map((o) => o.value)) !==
      canonical(tasks)
    ) {
      $("rehearsal-task").replaceChildren(
        ...tasks.map((task) => {
          const option = root.createElement("option");
          option.value = task;
          option.textContent = task;
          return option;
        }),
      );
    }
    $("rehearsal-run").textContent = rehearsal
      ? `${rehearsal.id} · 시도 ${rehearsal.attempt}`
      : "아직 이 창에서 확인한 리허설 실행 없음";
    $("failback-command").textContent =
      commandId ?? "아직 이 창에서 확인한 복귀 명령 없음";
    const blocked = active || Boolean(pending) || Boolean(mainPending());
    $("operations-confirm").disabled = !usable || blocked;
    for (const action of ACTIONS) {
      const button = $("op-" + action);
      let disabled =
        !configured(action) || blocked || !$("operations-confirm").checked;
      if (action === "failback-submit")
        disabled ||=
          !approved ||
          approved.plan !== model?.plan ||
          approved.version !== model?.snapshot?.version ||
          canonical(approved.binding) !== canonical(binding) ||
          Date.now() >= Date.parse(approved.binding.expires_at);
      if (["failback-reconcile", "failback-edge"].includes(action))
        disabled ||= !commandId;
      if (action === "rehearsal-reconcile") disabled ||= !rehearsal;
      button.disabled = disabled;
    }
    $("operation-lookup").hidden = !pending;
    $("operation-lookup").disabled =
      active || !ready(model, Date.now()) || pending?.plan !== model?.plan;
    $("operation-repeat").hidden = !pending?.lookupUnknown;
    $("operation-repeat").disabled =
      active || !pending?.lookupUnknown || !canRepeat();
  }
  function lock(value) {
    active = value;
    setBusy(value || Boolean(pending));
    render();
  }
  function canRepeat() {
    const model = getModel();
    return Boolean(
      pending &&
        configured(pending.action) &&
        !mainPending() &&
        snapshotKey(model) === pending.snapshotKey &&
        (!pending.body.binding ||
          canonical(getPanel()?.operations?.failback_binding) ===
            canonical(pending.body.binding)) &&
        (!pending.body.expires_at ||
          Date.now() < Date.parse(pending.body.expires_at)),
    );
  }
  function accepted(value, item) {
    const receipt = operationReceipt(item.action, value, item);
    if (item.action.startsWith("initial-") && receipt.status === "UNKNOWN") {
      status("실행 결과 미확인 · 같은 요청 결과 조회만 허용합니다.");
      return receipt;
    }
    receiptPlan = item.plan;
    if (item.action === "failback-approve")
      approved = {
        plan: item.plan,
        version: item.version,
        binding: receipt.binding,
        id: receipt.approval_id,
      };
    if (item.action.startsWith("failback-") && receipt.command_id)
      commandId = receipt.command_id;
    if (item.action.startsWith("rehearsal-"))
      rehearsal = {
        id: receipt.rehearsal_id,
        attempt: receipt.attempt,
        task: item.taskId,
        version: receipt.plan_version ?? item.admissionVersion ?? item.version,
      };
    pending = null;
    status(
      `서버 접수·조회 확인: ${item.action}${receipt.status ? " · " + receipt.status : ""}. 작업 완료는 서버 이력과 검증 결과로 확인하세요.`,
    );
    return receipt;
  }
  async function send() {
    const item = pending,
      model = getModel(),
      token = epoch;
    if (!item || active || item.plan !== model?.plan) return;
    lock(true);
    status("운영 요청의 서버 응답을 확인하고 있습니다.");
    try {
      const value = await request(item.path, {
        body: item.body,
        key: item.key,
        signal: AbortSignal.timeout(
          item.action.startsWith("initial-") ? 25000 : 12000,
        ),
      });
      if (token !== epoch || getModel() !== model) return;
      accepted(value, item);
    } catch (error) {
      if (token !== epoch || getModel() !== model) return;
      if (onForbidden(error)) return;
      if (error.status >= 400 && error.status < 500) {
        pending = null;
        status(
          "서버가 요청을 거부했습니다. 현재 권한·대상·계획 버전을 다시 확인하세요.",
        );
      } else
        status(
          "응답을 확인하지 못했습니다. 자동으로 재실행하지 않습니다. 같은 요청 결과 확인을 눌러 주세요.",
        );
    } finally {
      if (token === epoch) {
        lock(false);
        await refresh();
      }
    }
  }
  function build(action) {
    const model = getModel(),
      base = `/v1/plans/${model.plan}`,
      version = model.snapshot.version;
    let path,
      body,
      extra = {};
    if (action === "shift") {
      path = base + "/approvals";
      body = {
        approval_kind: "shift",
        expected_version: version,
        expires_at: new Date(Date.now() + 300000).toISOString(),
      };
    }
    if (action === "failback-approve") {
      path = base + "/approvals";
      body = {
        approval_kind: "failback",
        expected_version: version,
        binding: failbackBinding(
          getPanel().operations.failback_binding,
          model.plan,
        ),
      };
    }
    if (action === "failback-submit") {
      path = base + "/failback";
      body = { approval_id: approved.id };
    }
    if (["failback-reconcile", "failback-edge"].includes(action)) {
      path = `${base}/failback/${commandId}/${action === "failback-edge" ? "edge" : "reconcile"}`;
      body = {};
      extra.commandId = commandId;
    }
    if (action === "rehearsal-start") {
      const task = $("rehearsal-task").value,
        attempt = Number($("rehearsal-attempt").value);
      if (
        !getPanel().operations.rehearsal_tasks.includes(task) ||
        ![1, 2, 3].includes(attempt)
      )
        throw Error("invalid_registered_task");
      path = base + "/rehearsals";
      body = { task_id: task, expected_version: version, attempt };
      extra = { taskId: task, attempt };
    }
    if (action === "rehearsal-reconcile") {
      path = `${base}/rehearsals/${rehearsal.id}/attempts/${rehearsal.attempt}/reconcile`;
      body = {};
      extra = {
        taskId: rehearsal.task,
        admissionVersion: rehearsal.version,
        rehearsalId: rehearsal.id,
        attempt: rehearsal.attempt,
      };
    }
    if (action.startsWith("initial-")) {
      path =
        base +
        "/initial-proposal/" +
        (action === "initial-start" ? "start" : "collect");
      body = {
        expected_version: version,
        plan_digest: model.snapshot.plan_digest,
      };
    }
    if (!path || !body) throw Error("invalid_operation");
    return {
      action,
      path,
      body,
      key: crypto.randomUUID(),
      plan: model.plan,
      version,
      snapshotKey: snapshotKey(model),
      ...extra,
    };
  }
  for (const action of ACTIONS)
    $("op-" + action).addEventListener("click", async () => {
      if (
        !configured(action) ||
        active ||
        pending ||
        mainPending() ||
        !$("operations-confirm").checked ||
        $("op-" + action).disabled
      )
        return;
      const model = getModel(),
        key = snapshotKey(model);
      if (
        !root.defaultView.confirm(
          `현재 계획 ${model.plan} · 버전 ${model.snapshot.version}에서 ${$("op-" + action).textContent.trim()} 요청을 보낼까요? 승인과 실행은 별도입니다.`,
        )
      )
        return;
      if (!configured(action) || snapshotKey(getModel()) !== key) return;
      try {
        pending = build(action);
      } catch {
        status("현재 운영 대상과 연결을 다시 확인해 주세요.");
        return;
      }
      $("operations-confirm").checked = false;
      await send();
    });
  $("operations-confirm").addEventListener("change", render);
  $("rehearsal-run-select").addEventListener("change", () => {
    const run = getPanel()?.operations?.rehearsal_runs?.find(
      (run) =>
        `${run.rehearsal_id}:${run.attempt}` ===
        $("rehearsal-run-select").value,
    );
    if (run)
      rehearsal = {
        id: run.rehearsal_id,
        attempt: run.attempt,
        task: run.task_id,
        version: run.plan_version ?? getModel()?.snapshot?.version,
      };
    render();
  });
  $("operation-lookup").addEventListener("click", async () => {
    const model = getModel(),
      item = pending,
      token = epoch;
    if (
      !item ||
      active ||
      !ready(model, Date.now()) ||
      item.plan !== model.plan
    )
      return;
    lock(true);
    try {
      // Reconcile endpoints only read the execution or retry Edge, never re-submit a DB command.
      if (
        ["failback-reconcile", "failback-edge", "rehearsal-reconcile"].includes(
          item.action,
        )
      ) {
        status(
          "이 조회 요청의 접수 여부는 자동 추정하지 않습니다. 서버 이력을 확인한 뒤 동일 요청을 명시적으로 다시 제출할 수 있습니다.",
        );
        item.lookupUnknown = true;
      } else {
        const value = await request(
          `/v1/plans/${item.plan}/operation-receipts/${item.key}`,
          { signal: AbortSignal.timeout(8000) },
        );
        if (token !== epoch || getModel() !== model) return;
        if (
          value.kind !== "console-operation-receipt.v1" ||
          value.plan_id !== item.plan ||
          value.plan_digest !== model.snapshot.plan_digest ||
          value.command_key !== item.key ||
          !["FOUND", "UNKNOWN"].includes(value.state)
        )
          throw Error("invalid_receipt_lookup");
        if (value.state === "FOUND" && value.operation === item.action)
          accepted(value.receipt, item);
        else if (value.state === "UNKNOWN") {
          item.lookupUnknown = true;
          status(
            "이번 요청의 접수 여부는 미확정입니다. 조회 결과만으로 실행되지 않았다고 판단하지 않습니다.",
          );
        } else throw Error("foreign_operation_receipt");
      }
    } catch (error) {
      if (token === epoch && getModel() === model && !onForbidden(error))
        status("요청 결과를 확인할 수 없습니다. 변경 잠금을 유지합니다.");
    } finally {
      if (token === epoch) {
        lock(false);
        await refresh();
      }
    }
  });
  $("operation-repeat").addEventListener("click", async () => {
    if (!pending?.lookupUnknown || active || !canRepeat()) return;
    if (
      !root.defaultView.confirm(
        "같은 본문과 멱등 키로 요청을 다시 제출합니다. 첫 요청이 도착하지 않았다면 이번 제출로 실행될 수 있습니다. 계속할까요?",
      )
    )
      return;
    if (canRepeat()) await send();
  });
  return {
    render,
    get pending() {
      return pending;
    },
    canConnect(plan) {
      return !pending || (!active && pending.plan === plan);
    },
    revoke() {
      epoch++;
      active = false;
      approved = null;
      commandId = null;
      rehearsal = null;
      receiptPlan = null;
      $("operations-confirm").checked = false;
      status(
        pending
          ? "요청 결과 미확인 · 재인증 후 같은 계획의 서버 기록을 확인하세요."
          : "운영 조작 권한을 확인할 수 없습니다.",
      );
      setBusy(Boolean(pending));
      render();
    },
  };
}
