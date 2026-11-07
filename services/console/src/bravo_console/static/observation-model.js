// Standalone read-only observations are never operational T10 plans.
import { workloadGraph, projectedWorkload } from "./workload-model.js";
const id = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const exact = (value, keys) =>
  value &&
  typeof value === "object" &&
  !Array.isArray(value) &&
  Object.keys(value).sort().join(" ") === keys.split(" ").sort().join(" ");
export function observedTopology(doc, now = Date.now()) {
  if (
    !exact(
      doc,
      "kind authorizes_execution plan_connected application_id source_revision stream_id revision queried_at environments graph" +
        ("security_config" in doc ? " security_config" : "") +
        ("control_history" in doc ? " control_history" : ""),
    ) ||
    doc.kind !== "console-observed-topology.v1" ||
    doc.authorizes_execution !== false ||
    doc.plan_connected !== false ||
    !id.test(doc.application_id) ||
    !/^[a-f0-9]{40}$/.test(doc.source_revision) ||
    !id.test(doc.stream_id) ||
    !Number.isSafeInteger(doc.revision) ||
    doc.revision < 1 ||
    !Array.isArray(doc.environments) ||
    doc.environments.length > 2
  )
    throw Error("invalid_observation");
  for (const env of doc.environments) {
    if (
      !exact(
        env,
        "environment_id lane status observed_at namespace_policy_count registered_service_count observed_service_count",
      ) ||
      !id.test(env.environment_id) ||
      !["source", "target"].includes(env.lane) ||
      !["CURRENT", "STALE", "UNAVAILABLE"].includes(env.status) ||
      (env.namespace_policy_count !== null &&
        (!Number.isSafeInteger(env.namespace_policy_count) ||
          env.namespace_policy_count < 0))
    )
      throw Error("invalid_observation_environment");
    if (
      env.status === "CURRENT" &&
      !(
        Number.isFinite(Date.parse(env.observed_at)) &&
        Date.parse(env.observed_at) <= now &&
        now - Date.parse(env.observed_at) < 90000
      )
    )
      env.status = "STALE";
  }
  if (
    new Set(doc.environments.map((e) => e.environment_id)).size !==
      doc.environments.length ||
    new Set(doc.environments.map((e) => e.lane)).size !==
      doc.environments.length
  )
    throw Error("ambiguous_observation_environment");
  for (const env of doc.environments)
    if (
      !Number.isSafeInteger(env.registered_service_count) ||
      env.registered_service_count < 1 ||
      (env.observed_service_count !== null &&
        (!Number.isSafeInteger(env.observed_service_count) ||
          env.observed_service_count < 0 ||
          env.observed_service_count > env.registered_service_count))
    )
      throw Error("invalid_service_count");
  const history = controlHistory(
    doc.control_history ?? [],
    doc.environments,
    now,
  );
  if (doc.graph === null) {
    if (Object.keys(doc.security_config ?? {}).length)
      throw Error("unbound_security_config");
    return { ...doc, view: null, securityConfig: {}, controlHistory: history };
  }
  if (
    !exact(
      doc.graph,
      "nodes edges evidence responses events facets observed_at expires_at",
    ) ||
    !exact(doc.graph.facets, Object.keys(doc.graph.facets).join(" "))
  )
    throw Error("invalid_observation_graph");
  if (
    Date.parse(doc.graph.expires_at) - Date.parse(doc.graph.observed_at) >
      90000 ||
    doc.graph.responses.length ||
    doc.graph.events.length ||
    doc.graph.nodes.some((n) => n.security !== "unknown")
  )
    throw Error("unsupported_observation_claim");
  const { facets, ...graph } = doc.graph;
  // Share graph validation without inventing any operational plan binding.
  const shaped = workloadGraph(graph, now);
  if (
    Object.keys(facets).sort().join(" ") !==
    shaped.nodes
      .map((n) => n.id)
      .sort()
      .join(" ")
  )
    throw Error("invalid_observation_facets");
  for (const facet of Object.values(facets))
    if (
      !exact(facet, "ready summary") ||
      ![true, false, null].includes(facet.ready) ||
      typeof facet.summary !== "string" ||
      facet.summary.length > 240
    )
      throw Error("invalid_readiness");
  const view = projectedWorkload(
    { ...shaped, observation_id: doc.application_id, stream_id: doc.stream_id },
    now,
  );
  const freshLanes = new Set(
    doc.environments.filter((e) => e.status === "CURRENT").map((e) => e.lane),
  );
  view.nodes = view.nodes.map((n) =>
    freshLanes.has(n.lane) || n.lane === "control"
      ? n
      : { ...n, health: "unknown", security: "unknown" },
  );
  const active = new Set(
    view.nodes
      .filter((n) => n.lane === "control" || freshLanes.has(n.lane))
      .map((n) => n.id),
  );
  view.edges = view.edges.map((e) =>
    active.has(e.source) && active.has(e.target)
      ? e
      : { ...e, state: "unknown" },
  );
  const currentFacets = Object.fromEntries(
    view.nodes.map((n) => [
      n.id,
      active.has(n.id)
        ? facets[n.id]
        : { ready: null, summary: "관측 만료·실패 · 현재 준비 상태 미확인" },
    ]),
  );
  const securityConfig = configuration(doc.security_config ?? {}, shaped.nodes);
  for (const key of Object.keys(securityConfig))
    if (!active.has(key)) delete securityConfig[key];
  return {
    ...doc,
    view,
    facets: currentFacets,
    securityConfig,
    controlHistory: history,
  };
}

const checkLabels = {
  host_network: "호스트 네트워크 공유",
  host_pid: "호스트 PID 공유",
  host_ipc: "호스트 IPC 공유",
  token_automount: "서비스계정 토큰 자동 마운트",
  privileged: "특권 컨테이너",
  privilege_escalation: "권한 상승 허용",
  non_root: "비루트 실행 강제",
  read_only_root: "루트 파일시스템 읽기 전용",
  drop_all_capabilities: "모든 Linux capability 제거",
  seccomp: "Seccomp 실행 제한",
  ingress_policy_count: "Ingress 선택 정책",
  egress_policy_count: "Egress 선택 정책",
};
const enabledAttention = new Set([
  "host_network",
  "host_pid",
  "host_ipc",
  "token_automount",
  "privileged",
  "privilege_escalation",
]);
function configuration(values, nodes) {
  if (
    !values ||
    typeof values !== "object" ||
    Array.isArray(values) ||
    Object.keys(values).length > 46
  )
    throw Error("invalid_security_config");
  const output = {};
  for (const [key, item] of Object.entries(values)) {
    const node = nodes.find((n) => n.id === key);
    if (
      !node ||
      !exact(item, "resource_uid checks") ||
      item.resource_uid !== node.resource_uid ||
      !Array.isArray(item.checks) ||
      item.checks.length < 1 ||
      item.checks.length > 12 ||
      new Set(item.checks.map((c) => c.id)).size !== item.checks.length
    )
      throw Error("unbound_security_config");
    output[key] = item.checks.map((check) => {
      if (!exact(check, "id value") || !Object.hasOwn(checkLabels, check.id))
        throw Error("invalid_security_check");
      const value = check.value,
        policy = check.id.endsWith("_policy_count");
      if (
        policy
          ? !Number.isSafeInteger(value) || value < 0 || value > 256
          : check.id === "seccomp"
            ? ![null, "RuntimeDefault", "Localhost", "Unconfined"].includes(
                value,
              )
            : ![null, true, false].includes(value)
      )
        throw Error("invalid_security_value");
      let state = value === null ? "UNSPECIFIED" : "DECLARED";
      if (
        value !== null &&
        (policy
          ? value === 0
          : check.id === "seccomp"
            ? value === "Unconfined"
            : enabledAttention.has(check.id)
              ? value === true
              : value === false)
      )
        state = "ATTENTION";
      const detail =
        value === null
          ? "미지정 · 상속·실제 효과 미확인"
          : policy
            ? `${value}개 선택 · 통신 거부 효과 미시험`
            : check.id === "seccomp"
              ? value
              : value
                ? "설정됨"
                : "설정 안 됨";
      return { id: check.id, label: checkLabels[check.id], state, detail };
    });
  }
  return output;
}

function controlHistory(rows, environments, now) {
  if (
    !Array.isArray(rows) ||
    rows.length > 2 ||
    new Set(rows.map((r) => r.environment_id)).size !== rows.length
  )
    throw Error("invalid_control_history");
  return rows.map((row) => {
    if (
      !exact(
        row,
        "environment_id lane profile_id profile_version authorizes_execution current_resource_verified status finished_at document_sha256 rows",
      ) ||
      !environments.some(
        (e) => e.environment_id === row.environment_id && e.lane === row.lane,
      ) ||
      !id.test(row.profile_id) ||
      !id.test(row.profile_version) ||
      row.authorizes_execution !== false ||
      row.current_resource_verified !== false ||
      !["HISTORY", "UNAVAILABLE"].includes(row.status) ||
      !Array.isArray(row.rows) ||
      row.rows.length > 64
    )
      throw Error("invalid_control_history");
    if (row.status === "UNAVAILABLE") {
      if (
        row.finished_at !== null ||
        row.document_sha256 !== null ||
        row.rows.length
      )
        throw Error("invalid_control_history");
    } else if (
      !Number.isFinite(Date.parse(row.finished_at)) ||
      Date.parse(row.finished_at) > now ||
      !/^[a-f0-9]{64}$/.test(row.document_sha256) ||
      !row.rows.length ||
      new Set(row.rows.map((r) => r.test_id)).size !== row.rows.length
    )
      throw Error("invalid_control_history");
    for (const test of row.rows) {
      if (
        !exact(
          test,
          "test_id control_id recorded_verdict evidence_state unknown_reason",
        ) ||
        !/^(IAM|ENC|SEC|AUD|NET|DR)-01\.[1-9]$/.test(test.test_id) ||
        test.control_id !== test.test_id.split(".")[0] ||
        !["PASS", "FAIL", "UNKNOWN"].includes(test.recorded_verdict) ||
        !["NO_EVIDENCE", "EXPIRED", "RECORDED"].includes(test.evidence_state) ||
        !(
          test.unknown_reason === null ||
          (typeof test.unknown_reason === "string" &&
            /^[A-Z_]{1,64}$/.test(test.unknown_reason))
        )
      )
        throw Error("invalid_control_history");
    }
    return structuredClone(row);
  });
}
