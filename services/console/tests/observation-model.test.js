// Display-boundary fixtures. They are not live collection or authenticated T10 plans.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { observedTopology } from "../src/bravo_console/static/observation-model.js";
const original = JSON.parse(
  readFileSync(new URL("./fixtures/workload-synthetic.json", import.meta.url)),
);
const NOW = Date.parse(original.observed_at);
function document() {
  const fields = [
    "nodes",
    "edges",
    "evidence",
    "responses",
    "events",
    "observed_at",
    "expires_at",
  ];
  const graph = Object.fromEntries(
    fields.map((k) => [k, structuredClone(original[k])]),
  );
  graph.nodes = graph.nodes
    .filter((n) => n.lane !== "control")
    .map((n) => ({
      ...n,
      health: "unknown",
      security: "unknown",
      health_evidence: null,
      security_evidence: null,
    }));
  const ids = new Set(graph.nodes.map((n) => n.id));
  graph.edges = graph.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ ...e, state: "unknown", evidence_id: null }));
  graph.evidence = [];
  graph.responses = [];
  graph.events = [];
  graph.facets = Object.fromEntries(
    graph.nodes.map((n) => [
      n.id,
      { ready: true, summary: "Kubernetes fixture readiness" },
    ]),
  );
  return {
    kind: "console-observed-topology.v1",
    authorizes_execution: false,
    plan_connected: false,
    application_id: "registered-chat",
    source_revision: "a".repeat(40),
    stream_id: "fixture-read-only",
    revision: 1,
    queried_at: original.observed_at,
    environments: ["source", "target"].map((lane) => ({
      environment_id: "registered-" + lane,
      lane,
      status: "CURRENT",
      observed_at: original.observed_at,
      namespace_policy_count: 1,
      registered_service_count: 1,
      observed_service_count: 1,
    })),
    graph,
  };
}
test("observation is not an operational plan, control verdict, or communication proof", () => {
  const result = observedTopology(document(), NOW);
  assert.equal(result.plan_connected, false);
  assert.equal(result.view.plan_id, undefined);
  assert.equal(result.authorizes_execution, false);
  assert.ok(
    result.view.nodes.every(
      (n) => n.health === "unknown" && n.security === "unknown",
    ),
  );
  assert.ok(result.view.edges.every((e) => e.state === "unknown"));
  assert.ok(!result.view.responses.length && !result.view.events.length);
});
test("clock expiry withdraws readiness without a new HTTP response", () => {
  const result = observedTopology(document(), NOW + 91000);
  assert.ok(result.environments.every((e) => e.status === "STALE"));
  assert.ok(Object.values(result.facets).every((f) => f.ready === null));
  assert.ok(result.view.edges.every((e) => e.state === "unknown"));
});
for (const mutate of [
  (d) => (d.authorizes_execution = true),
  (d) => (d.plan_connected = true),
  (d) => (d.plan_id = "not-a-T10-plan"),
  (d) => (d.graph.nodes[0].security = "isolated"),
  (d) => (d.graph.nodes[0].security = "clear"),
  (d) => (d.graph.nodes[0].id = 'bad"selector'),
  (d) => (d.graph.facets.extra = { ready: true, summary: "other-resource" }),
  (d) => (d.environments[1].lane = d.environments[0].lane),
])
  test("invalid or unsupported observed claim is rejected", () =>
    assert.throws(() => {
      const value = document();
      mutate(value);
      observedTopology(value, NOW);
    }));
test("a failed environment cannot keep current readiness", () => {
  const value = document();
  value.environments[0].status = "STALE";
  const result = observedTopology(value, NOW);
  for (const n of result.view.nodes.filter((n) => n.lane === "source"))
    assert.equal(result.facets[n.id].ready, null);
});
function configured() {
  const d = document(),
    node = d.graph.nodes[0];
  d.security_config = {
    [node.id]: {
      resource_uid: node.resource_uid,
      checks: [
        { id: "privileged", value: true },
        { id: "seccomp", value: null },
        { id: "ingress_policy_count", value: 1 },
      ],
    },
  };
  d.control_history = [
    {
      environment_id: "registered-source",
      lane: "source",
      profile_id: "demo-gate",
      profile_version: "1",
      authorizes_execution: false,
      current_resource_verified: false,
      status: "HISTORY",
      finished_at: d.queried_at,
      document_sha256: "a".repeat(64),
      rows: [
        {
          test_id: "IAM-01.1",
          control_id: "IAM-01",
          recorded_verdict: "PASS",
          evidence_state: "EXPIRED",
          unknown_reason: null,
        },
      ],
    },
  ];
  return d;
}
test("configuration attention and retained PASS never become security or gate authority", () => {
  const value = observedTopology(configured(), NOW);
  const checks = Object.values(value.securityConfig)[0];
  assert.equal(checks[0].state, "ATTENTION");
  assert.equal(checks[1].state, "UNSPECIFIED");
  assert.equal(checks[2].state, "DECLARED");
  assert.ok(value.view.nodes.every((n) => n.security === "unknown"));
  assert.equal(value.controlHistory[0].current_resource_verified, false);
  assert.equal(value.authorizes_execution, false);
});
test("browser clock independently withdraws expired configuration", () => {
  assert.deepEqual(
    observedTopology(configured(), NOW + 91000).securityConfig,
    {},
  );
});
for (const mutate of [
  (d) => (Object.values(d.security_config)[0].resource_uid = "old-resource"),
  (d) => (Object.values(d.security_config)[0].checks[0].value = "true"),
  (d) => (Object.values(d.security_config)[0].checks[2].value = true),
  (d) => (Object.values(d.security_config)[0].checks[0].id = "approved"),
  (d) => (d.control_history[0].authorizes_execution = true),
  (d) => (d.control_history[0].current_resource_verified = true),
  (d) => (d.control_history[0].environment_id = "other-app"),
  (d) => d.control_history[0].rows.push(d.control_history[0].rows[0]),
])
  test("forged settings and retained authority are rejected", () => {
    const d = configured();
    mutate(d);
    assert.throws(() => observedTopology(d, NOW));
  });
