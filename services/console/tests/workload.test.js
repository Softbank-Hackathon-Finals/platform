import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { workloadLayout } from "../src/bravo_console/static/workload-layout.js";
import { scenarioDocument } from "../src/bravo_console/static/workload-scenario.js";
import {
  workloadDocument,
  projectedWorkload,
  WorkloadProjection,
  possibleImpact,
  t10ResponseContext,
} from "../src/bravo_console/static/workload-model.js";

const now = Date.parse("2026-11-07T00:00:00Z");
const plan = {
  plan_id: "local-security-story",
  plan_digest: "d".repeat(64),
  version: 1,
};
const document = (step = 5) => scenarioDocument(step, { now });

test("all storyboard stages satisfy the same display contract", () => {
  for (const variant of ["normal", "verification_failure"])
    for (let step = 0; step < 10; step++)
      assert.equal(
        workloadDocument(scenarioDocument(step, { now, variant }), plan, now)
          .provenance,
        "synthetic",
      );
});
test("a request does not become a confirmed isolation", () => {
  const pending = projectedWorkload(document(4), { now });
  assert.equal(pending.nodes.find((n) => n.id === "api").security, "hold");
  assert.equal(pending.edges.find((e) => e.id === "api-db").state, "requested");
  assert.equal(
    pending.responses.find((r) => r.stage === "isolate").status,
    "requested",
  );
  const confirmed = projectedWorkload(document(5), { now });
  assert.equal(
    confirmed.nodes.find((n) => n.id === "api").security,
    "isolated",
  );
  assert.equal(confirmed.edges.find((e) => e.id === "api-db").state, "blocked");
});
test("failed replacement keeps original isolated and route unconfirmed", () => {
  const view = projectedWorkload(
    scenarioDocument(9, { now, variant: "verification_failure" }),
    { now },
  );
  assert.equal(view.nodes.find((n) => n.id === "api").security, "isolated");
  assert.equal(
    view.responses.find((r) => r.stage === "verify").status,
    "failed",
  );
  assert.equal(
    view.responses.find((r) => r.stage === "route").status,
    "pending",
  );
  assert.equal(view.edges.find((e) => e.id === "edge-green").state, "unknown");
});
for (const mode of ["expired", "disconnected", "historical"])
  test(`${mode} removes current claims while retaining historical evidence`, () => {
    const doc = document();
    if (mode === "historical") doc.provenance = "historical";
    const view = projectedWorkload(doc, {
      now: mode === "expired" ? now + 30000 : now,
      connected: mode !== "disconnected",
    });
    assert.ok(
      view.nodes.every(
        (n) => n.health === "unknown" && n.security === "unknown",
      ),
    );
    assert.ok(view.edges.every((e) => e.state === "unknown"));
    assert.ok(view.responses.every((r) => !r.current));
    assert.ok(view.evidence.length > 0);
  });
const mutations = {
  other_plan: (d) => (d.plan_id = "other"),
  other_digest: (d) => (d.plan_digest = "a".repeat(64)),
  newer_plan: (d) => d.plan_version++,
  replaced_resource: (d) =>
    (d.nodes.find((n) => n.id === "api").resource_uid = "new-pod"),
  missing_isolation: (d) =>
    (d.evidence = d.evidence.filter((e) => e.id !== "demo-api-isolation")),
  borrowed_stage_proof: (d) =>
    (d.responses.find((r) => r.stage === "isolate").evidence_id =
      "demo-stage-approve"),
  unrelated_stage_resource: (d) =>
    (d.responses.find((r) => r.stage === "isolate").node_ids = ["database"]),
  unproven_group: (d) =>
    d.responses.find((r) => r.stage === "isolate").node_ids.push("database"),
  node_edge_id_collision: (d) => (d.edges[0].id = d.nodes[0].id),
  invalid_unknown_reference: (d) => {
    d.nodes[0].health = "unknown";
    d.nodes[0].health_evidence = "absent";
  },
  duplicated_resource: (d) => d.nodes.push({ ...d.nodes[0] }),
  dangling_edge: (d) => (d.edges[0].target = "absent"),
  repeated_sequence: (d) => (d.events[1].sequence = d.events[0].sequence),
  future_observation: (d) => (d.observed_at = new Date(now + 1).toISOString()),
  excessive_validity: (d) =>
    (d.expires_at = new Date(now + 300001).toISOString()),
  executable: (d) => (d.authorizes_execution = true),
};
for (const [name, mutate] of Object.entries(mutations))
  test(`rejects ${name}`, () => {
    const doc = document();
    mutate(doc);
    assert.throws(
      () => workloadDocument(doc, plan, now),
      /invalid_workload_projection/,
    );
  });
test("old revisions never overwrite current, conflicting same revision becomes unknown", () => {
  const projection = new WorkloadProjection();
  projection.accept(document(5), plan, now);
  assert.equal(projection.accept(document(4), plan, now), false);
  assert.equal(projection.document.revision, 6);
  const conflict = document(5);
  conflict.source_label = "changed";
  assert.equal(projection.accept(conflict, plan, now), false);
  assert.equal(projection.view(true, now).fresh, false);
});
test("changed stream requires an explicit resynchronization", () => {
  const projection = new WorkloadProjection();
  projection.accept(document(), plan, now);
  const next = document(6);
  next.stream_id = "restarted-source";
  assert.equal(projection.accept(next, plan, now), false);
  assert.equal(projection.view(true, now).fresh, false);
  projection.clear();
  assert.equal(projection.accept(next, plan, now), true);
});
test("individual evidence expiry removes its claim before the envelope expires", () => {
  const doc = document();
  doc.evidence.find((e) => e.id === "demo-api-isolation").expires_at = new Date(
    now + 1,
  ).toISOString();
  const view = projectedWorkload(workloadDocument(doc, plan, now), {
    now: now + 2,
  });
  assert.equal(view.fresh, true);
  assert.equal(view.nodes.find((n) => n.id === "api").security, "unknown");
});
test("impact highlights dependency neighbors and excludes confirmed blocked edges", () => {
  assert.ok(
    possibleImpact(projectedWorkload(document(1), { now }), ["api"]).includes(
      "database",
    ),
  );
  assert.ok(
    !possibleImpact(projectedWorkload(document(5), { now }), ["api"]).includes(
      "database",
    ),
  );
});
test("T10 safety hold is not interpreted as actual attack or isolation", () => {
  const context = t10ResponseContext({ ...plan, state: "SAFE_HOLD" }, [], true);
  assert.equal(context.state, "hold");
  assert.match(context.summary, /별도 근거/);
  assert.equal(t10ResponseContext(plan, [], false).state, "unknown");
});
test("local storyboard has no network, persistent storage or operational command", () => {
  const source = readFileSync(
    new URL(
      "../src/bravo_console/static/workload-scenario.js",
      import.meta.url,
    ),
    "utf8",
  );
  assert.doesNotMatch(
    source,
    /\bfetch\s*\(|localStorage|sessionStorage|XMLHttpRequest|WebSocket\s*\(|\.post\s*\(/,
  );
});

test("all supported resources have visible distinct positions even when hints collide", () => {
  const nodes = Array.from({ length: 48 }, (_, i) => ({
    id: `node-${i}`,
    lane: i < 24 ? "source" : i < 40 ? "target" : "control",
    column: 4,
    row: 0,
  }));
  const layout = workloadLayout(nodes);
  assert.equal(
    new Set([...layout.positions.values()].map((p) => `${p.x}:${p.y}`)).size,
    48,
  );
  for (const p of layout.positions.values()) {
    assert.ok(p.x >= 71 && p.x + 71 <= layout.width);
    assert.ok(p.y >= 42 && p.y + 42 <= layout.height);
  }
});

test("existing security snapshot adapter renders warnings without isolation inference", () => {
  const d = JSON.parse(
    readFileSync(
      new URL("fixtures/security-projection.json", import.meta.url),
      "utf8",
    ),
  );
  const checked = workloadDocument(
    d,
    { plan_id: d.plan_id, plan_digest: d.plan_digest, version: d.plan_version },
    now,
  );
  const view = projectedWorkload(checked, { now });
  assert.equal(view.nodes.length, 20);
  assert.equal(view.nodes.filter((n) => n.security === "warning").length, 2);
  assert.ok(view.nodes.every((n) => n.health === "unknown"));
  assert.ok(view.edges.every((e) => e.state === "unknown"));
  assert.ok(view.responses.every((r) => r.status === "unknown"));
  const layout = workloadLayout(view.nodes);
  assert.equal(layout.positions.size, 20);
});
