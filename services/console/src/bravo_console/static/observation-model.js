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
      "kind authorizes_execution plan_connected application_id source_revision stream_id revision queried_at environments graph",
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
  if (doc.graph === null) return { ...doc, view: null };
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
  return { ...doc, view, facets: currentFacets };
}
