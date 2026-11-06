import { ApiError, request } from "./transport.js";
import { evaluationProfile, identifier } from "./model.js";

const paths = {
  environment: "/v1/environments",
  workload: "/v1/workloads",
  plan: "/v1/plans",
};
const environmentChoices = new Map([
  ["aws", { kind: "aws", role: "source" }],
  ["onprem", { kind: "onprem", role: "target" }],
  ["onprem-source", { kind: "onprem", role: "source" }],
]);
const fields = {
  environment: ["name", "kind", "roles", "connection_ref"],
  workload: [
    "name",
    "repo_url",
    "app_path",
    "iac_path",
    "source_environment_id",
    "namespace",
  ],
  plan: [
    "workload_id",
    "target_environment_id",
    "profile_id",
    "profile_version",
  ],
};
const safeText = (value, max) =>
  typeof value === "string" &&
  value.length > 0 &&
  value.length <= max &&
  !/[\x00-\x1f\x7f]/.test(value);
const hash = (value) =>
  typeof value === "string" && /^[a-f0-9]{64}$/.test(value);

export function registrationInput(kind, input) {
  const hasRecheck =
    kind === "plan" &&
    input &&
    typeof input === "object" &&
    ("recheck_profile_id" in input || "recheck_profile_version" in input);
  const expected = hasRecheck
    ? [...fields.plan, "recheck_profile_id", "recheck_profile_version"]
    : fields[kind];
  if (
    !fields[kind] ||
    !input ||
    typeof input !== "object" ||
    Array.isArray(input) ||
    Object.keys(input).length !== expected.length ||
    expected.some((key) => !(key in input))
  )
    throw new ApiError("invalid_registration_input");
  const value = structuredClone(input);
  for (const [key, item] of Object.entries(value)) {
    if (key === "roles") continue;
    if (
      !safeText(
        item,
        key === "repo_url"
          ? 512
          : ["app_path", "iac_path"].includes(key)
            ? 256
            : 128,
      )
    )
      throw new ApiError("invalid_registration_input");
  }
  for (const key of [
    "connection_ref",
    "source_environment_id",
    "namespace",
    "workload_id",
    "target_environment_id",
  ])
    if (key in value && !identifier(value[key]))
      throw new ApiError("invalid_registration_input");
  if (
    kind === "plan" &&
    (![value.profile_id, value.profile_version].every(evaluationProfile) ||
      (hasRecheck &&
        ![value.recheck_profile_id, value.recheck_profile_version].every(
          evaluationProfile,
        )))
  )
    throw new ApiError("invalid_registration_input");
  if (
    kind === "environment" &&
    (!Array.isArray(value.roles) ||
      value.roles.length !== 1 ||
      !Array.from(environmentChoices.values()).some(
        ({ kind, role }) => kind === value.kind && role === value.roles[0],
      ))
  )
    throw new ApiError("invalid_registration_input");
  if (kind === "workload") {
    let url;
    try {
      url = new URL(value.repo_url);
    } catch {
      throw new ApiError("invalid_registration_input");
    }
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.search ||
      url.hash
    )
      throw new ApiError("invalid_registration_input");
    for (const key of ["app_path", "iac_path"])
      if (
        value[key].startsWith("/") ||
        value[key].split("/").includes("..") ||
        value[key].includes("\\")
      )
        throw new ApiError("invalid_registration_input");
  }
  return value;
}

export function registrationReceipt(kind, input, value) {
  const warnings =
    kind === "environment" ? value?.checks?.warnings : value?.warnings;
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !safeText(value.registered_at, 64) ||
    !Number.isFinite(Date.parse(value.registered_at)) ||
    !Array.isArray(warnings) ||
    warnings.length > 32 ||
    warnings.some((w) => !safeText(w, 128))
  )
    throw new ApiError("invalid_registration_receipt");
  const result = {
    kind,
    registered_at: value.registered_at,
    warnings: [...warnings],
  };
  if (kind === "environment") {
    if (
      !identifier(value.environment_id) ||
      value.kind !== input.kind ||
      value.role !== input.roles[0] ||
      value.connection_ref !== input.connection_ref ||
      value.checks?.read_access !== "verified"
    )
      throw new ApiError("invalid_registration_receipt");
    return { ...result, id: value.environment_id, role: value.role };
  }
  if (kind === "workload") {
    if (
      !identifier(value.workload_id) ||
      value.source_environment_id !== input.source_environment_id ||
      value.repo_url !== input.repo_url ||
      value.app_path !== input.app_path ||
      value.iac_path !== input.iac_path ||
      value.namespace !== input.namespace ||
      value.repo_deploy_match !== true
    )
      throw new ApiError("invalid_registration_receipt");
    return { ...result, id: value.workload_id };
  }
  if (
    !identifier(value.plan_id) ||
    !identifier(value.source_environment_id) ||
    value.workload_id !== input.workload_id ||
    value.target_environment_id !== input.target_environment_id ||
    ![
      input.profile_id,
      input.profile_version,
      value.profile_id,
      value.profile_version,
    ].every(evaluationProfile) ||
    value.profile_id !== input.profile_id ||
    value.profile_version !== input.profile_version ||
    Object.hasOwn(input, "recheck_profile_id") !==
      Object.hasOwn(input, "recheck_profile_version") ||
    ["recheck_profile_id", "recheck_profile_version"].some(
      (key) =>
        Object.hasOwn(value, key) !== Object.hasOwn(input, key) ||
        value[key] !== input[key] ||
        (Object.hasOwn(value, key) && !evaluationProfile(value[key])),
    ) ||
    value.state !== "COLLECT" ||
    value.version !== 1 ||
    !hash(value.plan_digest) ||
    !hash(value.collection_scope_digest)
  )
    throw new ApiError("invalid_registration_receipt");
  return {
    ...result,
    id: value.plan_id,
    state: value.state,
    profile_id: value.profile_id,
    profile_version: value.profile_version,
    ...(Object.hasOwn(value, "recheck_profile_id")
      ? {
          recheck_profile_id: value.recheck_profile_id,
          recheck_profile_version: value.recheck_profile_version,
        }
      : {}),
  };
}

// One in-flight registration; payload and key survive ambiguous responses only in memory.
// This class never retries automatically and never authorizes a cutover.
export class RegistrationAttempt {
  constructor(kind, input, key = crypto.randomUUID()) {
    this.kind = kind;
    this.body = JSON.stringify(registrationInput(kind, input));
    this.key = key;
    this.running = false;
  }
  async send(options = {}) {
    if (this.running) throw new ApiError("registration_in_progress");
    this.running = true;
    try {
      const input = JSON.parse(this.body);
      const value = await request(paths[this.kind], {
        ...options,
        body: input,
        key: this.key,
      });
      return registrationReceipt(this.kind, input, value);
    } finally {
      this.running = false;
    }
  }
}

export function mountRegistration({ onPlan, onForbidden }) {
  const $ = (id) => document.getElementById(id);
  const forms = [...document.querySelectorAll("[data-registration]")];
  const output = $("registration-receipts");
  let attempt = null,
    epoch = 0,
    controller;
  function status(message) {
    $("registration-status").textContent = message;
  }
  function render() {
    for (const form of forms)
      for (const element of form.elements) element.disabled = Boolean(attempt);
    $("registration-retry").hidden = !attempt;
    $("registration-retry").disabled = Boolean(attempt?.running);
  }
  function revoke() {
    epoch++;
    controller?.abort();
    output.replaceChildren();
    for (const form of forms) form.reset();
    status(
      "로그인 또는 권한을 확인해 주세요. 접수된 요청이 취소됐다는 뜻은 아닙니다. 재인증 후 같은 요청의 결과를 확인하세요.",
    );
    render();
  }
  async function send() {
    if (!attempt || attempt.running) return;
    const current = attempt,
      activeEpoch = epoch;
    controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    status("서버가 등록 정보와 현재 읽기 권한을 확인하고 있습니다.");
    const sending = current.send({ signal: controller.signal });
    render();
    try {
      const receipt = await sending;
      if (activeEpoch !== epoch) return;
      attempt = null;
      const row = document.createElement("li");
      row.textContent = `${receipt.kind === "environment" ? (receipt.role === "source" ? "출발 환경" : "목적 환경") : receipt.kind === "workload" ? "앱" : "계획"}: ${receipt.id} · 등록 ${receipt.registered_at}${receipt.kind === "plan" ? " · 평가 프로필 " + receipt.profile_id + " / " + receipt.profile_version : ""}${receipt.warnings.length ? " · 권한 경고: " + receipt.warnings.join(", ") : ""}`;
      output.append(row);
      while (output.children.length > 20) output.firstChild.remove();
      if (receipt.kind === "environment") {
        $(
          receipt.role === "source"
            ? "registration-source"
            : "registration-target",
        ).value = receipt.id;
      } else if (receipt.kind === "workload")
        $("registration-workload").value = receipt.id;
      status(
        "등록 영수증을 확인했습니다. 등록만으로 이전이나 보안 검증이 완료되지는 않습니다.",
      );
      if (receipt.kind === "plan") onPlan(receipt.id);
    } catch (error) {
      if (activeEpoch !== epoch) return;
      if ([401, 403].includes(error.status)) {
        revoke();
        onForbidden(error);
        return;
      }
      if ([400, 404, 409, 422].includes(error.status)) {
        attempt = null;
        status(
          error.status === 404
            ? "연결 참조 또는 등록 API를 사용할 수 없습니다. 서버 담당자가 등록한 참조를 확인해 주세요."
            : "서버가 등록을 거부했습니다. 입력과 현재 배포 상태를 확인해 주세요.",
        );
      } else
        status(
          "접수 결과 미확인. 입력과 요청 키를 보존했습니다. 새 등록 대신 같은 요청의 결과를 확인하세요.",
        );
    } finally {
      clearTimeout(timer);
      render();
    }
  }
  for (const form of forms)
    form.onsubmit = (event) => {
      event.preventDefault();
      if (attempt) return;
      const data = Object.fromEntries(new FormData(form));
      if (
        data.recheck_profile_id === "" &&
        data.recheck_profile_version === ""
      ) {
        delete data.recheck_profile_id;
        delete data.recheck_profile_version;
      }
      try {
        if (form.dataset.registration === "environment") {
          const choice = environmentChoices.get(data.kind);
          if (!choice) throw new ApiError("invalid_registration_input");
          data.kind = choice.kind;
          data.roles = [choice.role];
        }
        attempt = new RegistrationAttempt(form.dataset.registration, data);
      } catch {
        status(
          "입력 형식을 확인해 주세요. 자격·토큰·API 주소 대신 등록된 연결 별칭을 사용합니다.",
        );
        return;
      }
      send();
    };
  $("registration-retry").onclick = send;
  return { revoke };
}
