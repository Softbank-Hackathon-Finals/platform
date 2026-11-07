import test from "node:test";
import assert from "node:assert/strict";
import {
  panelDocument,
  currentPanels,
  FEATURES,
  ACTIONS,
} from "../src/bravo_console/static/panels.js";
import { now, snap } from "./fixtures.js";

function document() {
  return {
    kind: "console-workspace.v1",
    plan_id: snap.plan_id,
    plan_digest: snap.plan_digest,
    plan_version: snap.version,
    queried_at: new Date(now).toISOString(),
    expires_at: new Date(now + 15000).toISOString(),
    authorizes_cutover: false,
    features: Object.fromEntries(
      FEATURES.map((name) => [
        name,
        {
          state: "OK",
          tables: [
            {
              title: "Owner data",
              columns: ["value"],
              rows: [["<img src=x onerror=alert(1)>"]],
            },
          ],
        },
      ]),
    ),
    operations: {
      actions: Object.fromEntries(ACTIONS.map((name) => [name, false])),
      failback_binding: null,
      rehearsal_tasks: [],
    },
  };
}
test("a source expiry is not extended by the surrounding response lifetime", () => {
  const value = document();
  value.features.costs.valid_until = new Date(now + 1000).toISOString();
  const frame = panelDocument(value, snap, now);
  assert.equal(currentPanels(frame, now + 999).features.costs.state, "OK");
  const later = currentPanels(frame, now + 1000);
  assert.equal(later.features.costs.state, "UNKNOWN");
  assert.deepEqual(later.features.costs.tables, []);
  assert.equal(later.features.audit.state, "OK");
});
test("all feature panels consume text data without granting cutover permission", () => {
  const result = panelDocument(document(), snap, now);
  assert.equal(result.authorizes_cutover, false);
  assert.equal(
    result.features.audit.tables[0].rows[0][0],
    "<img src=x onerror=alert(1)>",
  );
});
for (const [name, mutate] of Object.entries({
  expired: (d) => {
    d.expires_at = new Date(now).toISOString();
  },
  future: (d) => {
    d.queried_at = new Date(now + 1).toISOString();
  },
  foreign_plan: (d) => {
    d.plan_id = "other-plan";
  },
  wrong_version: (d) => {
    d.plan_version++;
  },
  malformed_rows: (d) => {
    d.features.audit.tables[0].rows = [["a", "b"]];
  },
  unexpected_action: (d) => {
    d.operations.actions.delete = true;
  },
  omitted_feature: (d) => {
    delete d.features.costs;
  },
  privilege: (d) => {
    d.authorizes_cutover = true;
  },
  hidden_data: (d) => {
    d.features.costs.state = "FORBIDDEN";
  },
}))
  test(`workspace rejects ${name}`, () => {
    const value = document();
    mutate(value);
    assert.throws(() => panelDocument(value, snap, now));
  });
