import test from "node:test";
import assert from "node:assert/strict";
import {
  networkDocument,
  networkRows,
} from "../src/bravo_console/static/network-workspace.js";
import { now, snap } from "./fixtures.js";
function document() {
  return {
    schema_version: 1,
    server_now: new Date(now).toISOString(),
    received_at: new Date(now - 600000).toISOString(),
    sova_context: {
      plan_id: snap.plan_id,
      plan_digest: snap.plan_digest,
      plan_version: snap.version,
      source_environment_id: snap.source_environment_id,
      target_environment_id: snap.target_environment_id,
      scope: "shared-environment-network",
      authorizes_cutover: false,
    },
    sources: [
      {
        id: "source_k8s",
        status: "error",
        scope: "fixture",
        checked_at: new Date(now - 600000).toISOString(),
        last_success_at: new Date(now - 900000).toISOString(),
        retained: true,
        data: {
          resources: [{ name: "<img src=x onerror=alert(1)>", kind: "Pod" }],
          networks: [],
          links: [],
        },
      },
    ],
  };
}
test("native network view retains source age, failure and literal text", () => {
  const doc = document();
  assert.equal(networkDocument(doc, snap, now), doc);
  const rows = networkRows(doc, "resources", now);
  assert.equal(rows[0].retained, true);
  assert.equal(rows[0].stale, true);
  assert.equal(rows[0].source_status, "error");
  assert.equal(rows[0].observed_at, doc.sources[0].last_success_at);
  assert.equal(rows[0].name, "<img src=x onerror=alert(1)>");
});
for (const [name, mutate] of Object.entries({
  other_plan: (d) => (d.sova_context.plan_id = "other"),
  old_version: (d) => d.sova_context.plan_version++,
  wrong_pair: (d) => (d.sova_context.target_environment_id = "other"),
  execution_grant: (d) => (d.sova_context.authorizes_cutover = true),
  expired_response: (d) => (d.server_now = new Date(now - 16000).toISOString()),
  duplicate_source: (d) => d.sources.push(d.sources[0]),
  future_source: (d) =>
    (d.sources[0].checked_at = new Date(now + 61000).toISOString()),
  invalid_retained: (d) => (d.sources[0].last_success_at = null),
}))
  test(`native network view rejects ${name}`, () => {
    const doc = document();
    mutate(doc);
    assert.throws(() => networkDocument(doc, snap, now));
  });
