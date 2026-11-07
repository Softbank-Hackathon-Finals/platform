import test from "node:test";
import assert from "node:assert/strict";
import { workspaceView } from "../src/bravo_console/static/workspace.js";

const now = 1_800_000_000_000;
const snapshot = {
  workload_id: "chat",
  source_environment_id: "source",
  target_environment_id: "target",
};
const observed = {
  at: now,
  rows: [
    { side: "source", state: "OK", error_ratio: 0.25, p95_seconds: 0.2 },
    { side: "target", state: "UNKNOWN", error_ratio: null, p95_seconds: null },
  ],
};

test("HTTP collection does not imply healthy requests or a running destination", () => {
  const view = workspaceView(snapshot, observed, true, now, now);
  assert.equal(view.source.observed, true);
  assert.match(view.source.detail, /25.00%/);
  assert.equal(view.target.observed, false);
  assert.equal(view.target.id, "target");
});

test("expired, future and disconnected observations never show current collection", () => {
  for (const [connection, snapshotAt, sampleAt] of [
    [false, now, now],
    [true, now - 15000, now],
    [true, now, now - 15000],
    [true, now + 1, now],
    [true, now, now + 1],
  ]) {
    const view = workspaceView(
      snapshot,
      { ...observed, at: sampleAt },
      connection,
      snapshotAt,
      now,
    );
    assert.equal(view.source.observed, false);
    assert.equal(view.target.observed, false);
  }
});

test("permission-revoked model clears resource identities even with old metrics", () => {
  const view = workspaceView(null, observed, false, 0, now);
  assert.equal(view.workload, "등록된 앱 없음");
  assert.equal(view.source.id, "등록된 환경 없음");
  assert.equal(view.target.id, "등록된 환경 없음");
  assert.equal(view.source.observed, false);
});
