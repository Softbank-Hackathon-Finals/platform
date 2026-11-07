import { test } from "node:test";
import assert from "node:assert/strict";
import {
  linkGeometry,
  interpolatePositions,
} from "../src/bravo_console/static/workload-motion.js";
import { workloadLayout } from "../src/bravo_console/static/workload-layout.js";
import { scenarioDocument } from "../src/bravo_console/static/workload-scenario.js";
const doc = (step) =>
  scenarioDocument(step, { now: Date.parse("2026-11-07T00:00:00Z") });

test("a request stays in its registered lane; only confirmation moves its presentation", () => {
  const pending = workloadLayout(doc(4).nodes),
    isolated = workloadLayout(doc(5).nodes);
  assert.equal(pending.quarantined.size, 0);
  assert.deepEqual([...isolated.quarantined].sort(), ["api", "worker"]);
  assert.notDeepEqual(
    pending.positions.get("api"),
    isolated.positions.get("api"),
  );
  assert.deepEqual(
    pending.positions.get("database"),
    isolated.positions.get("database"),
  );
  assert.deepEqual(
    pending.positions.get("storage"),
    isolated.positions.get("storage"),
  );
  assert.equal(doc(5).nodes.find((n) => n.id === "api").lane, "source");
});
test("motion interpolation has stable endpoints and a real intermediate position", () => {
  const a = new Map([["api", { x: 0, y: 20 }]]),
    b = new Map([["api", { x: 100, y: 120 }]]);
  assert.deepEqual(interpolatePositions(a, b, 0), a);
  assert.deepEqual(interpolatePositions(a, b, 1), b);
  assert.deepEqual(interpolatePositions(a, b, 0.5).get("api"), {
    x: 50,
    y: 70,
  });
});
test("blocked curves have a visible gap in either direction without invalid SVG coordinates", () => {
  for (const a of [
    { x: 100, y: 100 },
    { x: 500, y: 100 },
  ])
    for (const b of [
      { x: 200, y: 400 },
      { x: 50, y: 100 },
    ]) {
      const geometry = linkGeometry(a, b);
      assert.ok(geometry.full.startsWith("M "));
      assert.notEqual(geometry.before, geometry.full);
      assert.notEqual(geometry.after, geometry.full);
      assert.ok(
        Number.isFinite(geometry.middle.x) &&
          Number.isFinite(geometry.middle.y),
      );
      assert.doesNotMatch(JSON.stringify(geometry), /NaN|Infinity/);
    }
});
