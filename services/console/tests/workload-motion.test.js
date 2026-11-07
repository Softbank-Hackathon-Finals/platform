import { test } from "node:test";
import assert from "node:assert/strict";
import { workloadLayout } from "../src/bravo_console/static/workload-layout.js";
import { scenarioDocument } from "../src/bravo_console/static/workload-scenario.js";
import {
  workloadDocument,
  projectedWorkload,
} from "../src/bravo_console/static/workload-model.js";

const now = Date.parse("2026-11-07T00:00:00Z");
const doc = (step, scope = "application", variant = "normal") =>
  scenarioDocument(step, { now, scope, variant });

test("all partial scopes preserve the same evidence contract at every step", () => {
  for (const scope of ["application", "api", "worker"])
    for (const variant of ["normal", "verification_failure"])
      for (let step = 0; step <= 9; step++) {
        const d = doc(step, scope, variant);
        assert.equal(
          workloadDocument(
            d,
            { plan_id: d.plan_id, plan_digest: d.plan_digest, version: 1 },
            now,
          ).authorizes_execution,
          false,
        );
      }
});
test("only selected components are isolated and unrelated connections continue", () => {
  const d = doc(5, "api");
  assert.deepEqual(
    d.nodes.filter((n) => n.security === "isolated").map((n) => n.id),
    ["api"],
  );
  assert.equal(d.edges.find((e) => e.id === "worker-db").state, "observed");
  assert.equal(d.edges.find((e) => e.id === "api-db").state, "blocked");
  assert.equal(d.nodes.find((n) => n.id === "database").security, "clear");
});
test("worker-only response keeps the public API and replaces the selected worker path", () => {
  const d = doc(9, "worker");
  assert.equal(d.nodes.find((n) => n.id === "api").security, "clear");
  assert.equal(d.edges.find((e) => e.id === "edge-api").state, "observed");
  assert.equal(d.edges.find((e) => e.id === "edge-green").source, "api");
  assert.equal(d.nodes.find((n) => n.id === "green").kind, "worker");
});
test("verification failure never supplies observed replacement or recovery paths", () => {
  for (const scope of ["application", "api", "worker"]) {
    const d = doc(9, scope, "verification_failure");
    assert.equal(d.edges.find((e) => e.id === "edge-green").state, "unknown");
    assert.equal(d.edges.find((e) => e.id === "green-db").state, "unknown");
    assert.equal(
      d.responses.find((e) => e.stage === "recover").status,
      "pending",
    );
  }
});
test("historical placement can be retained while all current isolation claims are withdrawn", () => {
  const value = projectedWorkload(doc(5), { now: now + 30001 });
  const layout = workloadLayout(
    value.nodes.map((n) => ({ ...n, display_quarantined: n.id === "api" })),
  );
  assert.equal(value.nodes.find((n) => n.id === "api").security, "unknown");
  assert.equal(layout.quarantined.has("api"), true);
  assert.ok(value.edges.every((e) => e.state === "unknown"));
});
