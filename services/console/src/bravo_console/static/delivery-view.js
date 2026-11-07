import { request } from "./transport.js";
import { identifier } from "./model.js";
import { canonical } from "./panels.js";
import { ready } from "./operations.js";
import {
  deliveryDocument,
  deliveryFresh,
  iacPlan,
  infraApproval,
  approvalUsable,
  pullRequest,
  createdIacId,
  SYNC_NAMES,
} from "./delivery-model.js";

export function mountDelivery(
  { getModel, mainPending, setBusy, onForbidden },
  root = document,
) {
  const $ = (id) => root.getElementById(id);
  let value = null,
    plan = null,
    approval = null,
    planAt = 0,
    approvalAt = 0,
    context = null,
    pending = null,
    active = false,
    loading = false,
    epoch = 0,
    lastRead = 0;
  const key = (m) =>
    m?.snapshot &&
    canonical({
      id: m.plan,
      digest: m.snapshot.plan_digest,
      version: m.snapshot.version,
    });
  const status = (message) => {
    $("iac-status").textContent = message;
  };
  const usable = () => deliveryFresh(value, getModel());
  const actionAllowed = (permission) =>
    usable() && value.iac.configured && value.iac.actions[permission] === true;
  const currentPlan = () =>
    usable() && plan && Date.now() >= planAt && Date.now() - planAt < 15000;
  const base = () => `/v1/plans/${getModel().plan}`;
  function node(tag, text, className) {
    const n = root.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (className) n.className = className;
    return n;
  }
  function render() {
    const model = getModel(),
      next = key(model),
      fresh = usable();
    if (context !== next) {
      context = next;
      plan = null;
      approval = null;
      planAt = 0;
      approvalAt = 0;
      $("iac-id").value = "";
      $("iac-approval-id").value = "";
      $("iac-confirm").checked = false;
    }
    const modules = fresh ? value.iac.modules : [];
    const selected = $("iac-module").value;
    const keys = modules.map((m) => `${m.environment_id}:${m.module}`);
    if (
      canonical([...$("iac-module").options].map((o) => o.value)) !==
      canonical(keys)
    ) {
      $("iac-module").replaceChildren(
        ...modules.map((m) => {
          const o = node("option", `${m.environment} · ${m.module}`);
          o.value = `${m.environment_id}:${m.module}`;
          return o;
        }),
      );
      if (keys.includes(selected)) $("iac-module").value = selected;
      setParams();
    }
    $("iac-capability").textContent =
      fresh && value.iac.configured
        ? `현재 사용자 ${value.subject_id} · 승인 ${value.iac.actions.approve_infra ? "권한 있음" : "권한 없음"} · PR 요청 ${value.iac.actions.apply_infra ? "권한 있음" : "권한 없음"}`
        : "IaC 운영 연결을 기다립니다. 서버에 등록된 모듈과 현재 권한을 확인하기 전에는 변경할 수 없습니다.";
    const blocked = active || Boolean(pending) || Boolean(mainPending());
    $("iac-module").disabled = blocked || !fresh;
    $("iac-params").disabled = blocked || !actionAllowed("apply_infra");
    $("iac-confirm").disabled = blocked || !fresh;
    const confirmed = $("iac-confirm").checked;
    $("iac-create").disabled =
      blocked || !confirmed || !actionAllowed("apply_infra") || !modules.length;
    $("iac-approve").disabled =
      blocked ||
      !confirmed ||
      !actionAllowed("approve_infra") ||
      !currentPlan() ||
      !plan.accepted ||
      plan.requested_by === value?.subject_id;
    $("iac-pr").disabled =
      blocked ||
      !confirmed ||
      !actionAllowed("apply_infra") ||
      !currentPlan() ||
      !plan.accepted ||
      !approvalUsable(approval, plan) ||
      Date.now() - approvalAt >= 15000;
    for (const id of ["iac-read", "iac-approval-read"])
      $(id).disabled = active || !ready(model, Date.now());
    $("iac-recover").hidden = !pending;
    $("iac-recover").disabled =
      active || !fresh || pending?.planId !== model?.plan;
    $("iac-repeat").hidden = !pending?.lookedUp;
    $("iac-repeat").disabled = active || !canRepeat();
    $("iac-plan-detail").textContent = plan
      ? `IaC 계획 ${plan.iac_plan_id}\n환경 ${plan.environment} · 모듈 ${plan.module}\n요청자 ${plan.requested_by}\n변경 ${plan.changes}건 · 서버 수용 ${plan.accepted ? "예" : "아니오"}\n기준 커밋 ${plan.base_sha}\n계획 해시 ${plan.plan_digest}\n거부 사유 ${plan.refusals.join(", ") || "없음"}\n영향 요약\n${JSON.stringify(plan.effects, null, 2)}\n${currentPlan() ? "최근 조회됨" : "조회 유효기간 만료 · 다시 조회 필요"}`
      : "서버가 저장한 IaC 계획을 만들거나 계획 ID로 조회해 주세요. 전체 파일 차이는 생성한 PR에서 확인합니다.";
    $("iac-approval-detail").textContent = approval
      ? `승인 ${approval.approval_id}\n승인자 ${approval.actor_id}\n만료 ${approval.expires_at}\n회수 ${approval.revoked === false ? "미회수" : approval.revoked === true ? "회수됨" : "서버 재조회 필요"}\n사용한 실행 ${approval.claimed_run_id ?? "없음 또는 재조회 필요"}\n${approvalUsable(approval, plan) && Date.now() - approvalAt < 15000 ? "조회 시점의 승인 정보 확인 · 실행 시 서버 재검증" : "사용 전 서버 재조회 필요"}`
      : "다른 담당자의 승인 ID를 조회하거나, 승인 권한이 있는 다른 사용자에게 이 IaC 계획 ID를 전달해 주세요.";
    const link = $("iac-pr-link");
    link.hidden = true;
    link.removeAttribute("href");
    if (plan?.pull_request) {
      const p = pullRequest(plan.pull_request, plan);
      link.href = p.url;
      link.textContent = `PR #${p.number} · 파일 변경 확인`;
      link.hidden = false;
    }
    const rows = fresh ? value.customers : [];
    $("customer-sync-list").replaceChildren(
      ...rows.map((row) => {
        const card = node("article", undefined, "security-progress-step");
        const current =
          ["UNCONFIGURED", "UNKNOWN"].includes(row.status) ||
          Date.now() < Date.parse(row.valid_until);
        card.dataset.sync = current ? row.status : "UNKNOWN";
        card.append(
          node(
            "h3",
            `${row.environment} · ${current ? SYNC_NAMES[row.status] : "관측 만료"}`,
          ),
          node("p", `검토한 커밋 ${row.expected_revision}`),
          node(
            "p",
            {
              live: "등록된 실환경 수집",
              synthetic: "합성 시험 관측 · 운영 결과 아님",
              historical: "과거 관측 · 현재 상태 아님",
            }[row.provenance],
          ),
        );
        if (row.observed_at)
          card.append(
            node(
              "p",
              `관측 ${new Date(row.observed_at).toLocaleString("ko-KR")}`,
            ),
          );
        if (row.retry_count)
          card.append(
            node(
              "p",
              `재시도 ${row.retry_count}회 · 완료로 처리하지 않습니다.`,
            ),
          );
        if (row.reasons?.length)
          card.append(node("p", `확인 항목: ${row.reasons.join(", ")}`));
        return card;
      }),
    );
    $("customer-sync-status").textContent = rows.length
      ? "고객 GitOps의 지정 커밋 반영 상태입니다. 데이터 이전·보안 동등성·무중단 전환 성공과 별도로 확인합니다."
      : "현재 계획의 고객 동기화 수집 연결을 기다립니다. 미수집을 동기화 성공으로 표시하지 않습니다.";
    setBusy(active || Boolean(pending));
  }
  function setParams() {
    const m = value?.iac.modules.find(
      (m) => `${m.environment_id}:${m.module}` === $("iac-module").value,
    );
    $("iac-params").value = m ? JSON.stringify(m.params, null, 2) : "";
    $("iac-confirm").checked = false;
  }
  async function refresh(force = false) {
    const m = getModel(),
      token = epoch;
    if (!m?.snapshot || loading || (!force && Date.now() - lastRead < 5000))
      return;
    loading = true;
    lastRead = Date.now();
    const captured = key(m);
    try {
      const raw = await request(`/v1/plans/${m.plan}/delivery`, {
        signal: AbortSignal.timeout(10000),
      });
      if (token === epoch && m === getModel() && captured === key(m))
        value = deliveryDocument(raw, m);
    } catch (error) {
      if (token === epoch) {
        value = null;
        onForbidden(error);
      }
    } finally {
      if (token === epoch) {
        loading = false;
        render();
      }
    }
  }
  function selection() {
    return value?.iac.modules.find(
      (m) => `${m.environment_id}:${m.module}` === $("iac-module").value,
    );
  }
  function checkPlan(v, id) {
    const p = iacPlan(v, id);
    if (
      !value?.iac.modules.some(
        (m) => m.environment === p.environment && m.module === p.module,
      )
    )
      throw Error("unregistered_iac_plan");
    return p;
  }
  async function readPlan(id) {
    if (!identifier(id)) throw Error("invalid_iac_id");
    return checkPlan(
      await request(`${base()}/iac/plans/${id}`, {
        signal: AbortSignal.timeout(8000),
      }),
      id,
    );
  }
  async function readApproval(id) {
    if (!identifier(id) || !plan) throw Error("invalid_approval_id");
    return infraApproval(
      await request(`${base()}/infra-approvals/${id}`, {
        signal: AbortSignal.timeout(8000),
      }),
      plan,
      Date.now(),
      id,
    );
  }
  function accept(v, item) {
    if (item.action === "create") {
      const checked = checkPlan(v, item.iacId);
      if (checked.requested_by !== item.subjectId)
        throw Error("foreign_iac_requester");
      plan = checked;
      planAt = Date.now();
      approval = null;
      $("iac-id").value = plan.iac_plan_id;
    }
    if (item.action === "approve") {
      const checked = infraApproval(v, plan);
      if (checked.actor_id !== item.subjectId)
        throw Error("foreign_iac_approver");
      approval = checked;
      approvalAt = 0;
      $("iac-approval-id").value = approval.approval_id;
    }
    if (item.action === "pr") {
      const p = pullRequest(v, plan, item.body.approval_id);
      plan = { ...plan, pull_request: p };
      planAt = Date.now();
    }
    pending = null;
    status(
      item.action === "approve"
        ? "승인을 저장했습니다. 승인 ID를 다시 조회한 뒤 PR을 요청할 수 있습니다."
        : item.action === "pr"
          ? "PR 생성 확인. 병합·고객 반영은 별도로 확인하세요."
          : "IaC 계획 저장 확인. 다른 담당자의 승인이 필요합니다.",
    );
  }
  function canRepeat() {
    return (
      pending?.lookedUp &&
      key(getModel()) === pending.context &&
      value?.subject_id === pending.subjectId &&
      !mainPending() &&
      actionAllowed(
        pending.action === "approve" ? "approve_infra" : "apply_infra",
      ) &&
      (pending.action !== "approve" ||
        Date.now() < Date.parse(pending.body.expires_at)) &&
      (pending.action !== "pr" ||
        (approvalUsable(approval, plan) && Date.now() - approvalAt < 15000))
    );
  }
  async function send() {
    const item = pending,
      m = getModel(),
      token = epoch;
    if (!item || active) return;
    active = true;
    render();
    try {
      const v = await request(item.path, {
        body: item.body,
        key: item.key,
        signal: AbortSignal.timeout(12000),
      });
      if (token !== epoch || m !== getModel() || key(m) !== item.context)
        return;
      accept(v, item);
    } catch (error) {
      if (token !== epoch) return;
      if (onForbidden(error)) return;
      if (error.status >= 400 && error.status < 500) {
        pending = null;
        status(
          "서버가 요청을 거부했습니다. 현재 대상·권한·승인 정보를 다시 확인해 주세요.",
        );
      } else
        status(
          "서버 응답 미확인. 자동으로 재전송하지 않습니다. 기존 요청 확인 후 동일 요청으로만 복구해 주세요.",
        );
    } finally {
      if (token === epoch) {
        active = false;
        render();
        await refresh(true);
      }
    }
  }
  for (const action of ["create", "approve", "pr"])
    $("iac-" + action).addEventListener("click", async () => {
      render();
      if ($("iac-" + action).disabled) return;
      const m = getModel(),
        captured = key(m);
      let body,
        path,
        iacId = plan?.iac_plan_id;
      const commandKey = crypto.randomUUID();
      try {
        if (action === "create") {
          const module = selection(),
            params = JSON.parse($("iac-params").value);
          if (
            !params ||
            Array.isArray(params) ||
            typeof params !== "object" ||
            JSON.stringify(params).length > 8192
          )
            throw Error();
          body = {
            environment: module.environment,
            module: module.module,
            params,
          };
          path = `${base()}/iac/plans`;
          iacId = await createdIacId(m.plan, commandKey);
        } else if (action === "approve") {
          body = {
            approval_kind: "infra",
            binding: structuredClone(plan.binding),
            expires_at: new Date(Date.now() + 300000).toISOString(),
          };
          path = base() + "/approvals";
        } else {
          body = { approval_id: approval.approval_id };
          path = `${base()}/iac/plans/${iacId}/pull-request`;
        }
      } catch {
        status("등록된 모듈의 비밀값 없는 JSON 설정을 확인해 주세요.");
        return;
      }
      if (
        !root.defaultView.confirm(
          `${m.plan}의 IaC ${action === "create" ? "계획 저장" : action === "approve" ? "승인" : "PR 생성"} 요청을 보낼까요? 클러스터에 직접 적용하지 않습니다.`,
        )
      )
        return;
      render();
      if (captured !== key(getModel()) || $("iac-" + action).disabled) return;
      pending = {
        action,
        body: structuredClone(body),
        path,
        key: commandKey,
        iacId,
        planId: m.plan,
        context: captured,
        subjectId: value.subject_id,
      };
      $("iac-confirm").checked = false;
      await send();
    });
  async function lookup(kind) {
    if (active || !usable()) return;
    const token = epoch,
      captured = key(getModel());
    active = true;
    render();
    try {
      const result =
        kind === "plan"
          ? await readPlan($("iac-id").value.trim())
          : await readApproval($("iac-approval-id").value.trim());
      if (token !== epoch || captured !== key(getModel())) return;
      if (kind === "plan") {
        plan = result;
        planAt = Date.now();
        approval = null;
        approvalAt = 0;
      } else {
        approval = result;
        approvalAt = Date.now();
      }
      status(
        "서버 저장 기록을 조회했습니다. 권한과 승인 유효성은 요청 시 서버가 다시 검사합니다.",
      );
    } catch (error) {
      if (token === epoch && !onForbidden(error)) {
        if (kind === "plan") planAt = 0;
        else approvalAt = 0;
        status("현재 계획의 저장 기록을 확인할 수 없습니다.");
      }
    } finally {
      if (token === epoch) {
        active = false;
        render();
      }
    }
  }
  $("iac-read").addEventListener("click", () => lookup("plan"));
  $("iac-approval-read").addEventListener("click", () => lookup("approval"));
  $("iac-recover").addEventListener("click", async () => {
    const item = pending,
      token = epoch;
    if (!item || active || !usable() || item.planId !== getModel().plan) return;
    active = true;
    render();
    try {
      if (item.action === "approve") {
        // A session revoke may have erased the displayed plan while keeping the
        // uncertain command. Re-read the native plan before any explicit replay.
        const found = await readPlan(item.iacId);
        if (token !== epoch || pending !== item) return;
        if (canonical(found.binding) !== canonical(item.body.binding))
          throw Error("changed_iac_binding");
        plan = found;
        planAt = Date.now();
        status(
          "응답에 승인 ID가 없어 직접 조회할 수 없습니다. 자동 재승인하지 않습니다. 서버의 멱등 저장을 사용하는 동일 요청 재전송만 명시적으로 선택할 수 있습니다.",
        );
      } else {
        const found = await readPlan(item.iacId);
        if (token !== epoch || pending !== item) return;
        if (item.action === "create") accept(found, item);
        else {
          plan = found;
          planAt = Date.now();
          if (found.pull_request) {
            accept(found.pull_request, item);
            return;
          }
          approval = await readApproval(item.body.approval_id);
          if (token !== epoch || pending !== item) return;
          approvalAt = Date.now();
          status(
            "저장된 PR 결과 없음. 실행되지 않았다는 증거는 아닙니다. 같은 요청의 복구만 허용합니다.",
          );
        }
      }
    } catch (error) {
      if (token === epoch && !onForbidden(error))
        status(
          "저장 기록 조회 실패. 새 요청은 잠근 채 동일 요청을 보존합니다.",
        );
    } finally {
      if (token === epoch) {
        if (pending === item) item.lookedUp = true;
        active = false;
        render();
      }
    }
  });
  $("iac-repeat").addEventListener("click", async () => {
    if (!canRepeat() || active) return;
    if (
      root.defaultView.confirm(
        "기존 본문과 멱등 키로만 재전송합니다. 첫 요청이 도착하지 않았다면 이번에 처리될 수 있습니다. 계속할까요?",
      ) &&
      canRepeat()
    )
      await send();
  });
  $("iac-confirm").addEventListener("change", render);
  $("iac-module").addEventListener("change", setParams);
  $("delivery-refresh").addEventListener("click", () => refresh(true));
  return {
    render,
    refresh,
    get pending() {
      return pending;
    },
    canConnect(id) {
      return !pending || (!active && pending.planId === id);
    },
    clear() {
      epoch++;
      value = null;
      plan = null;
      approval = null;
      loading = false;
      active = false;
      lastRead = 0;
      $("iac-confirm").checked = false;
      render();
    },
  };
}
