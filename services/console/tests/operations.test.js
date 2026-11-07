import test from "node:test";
import assert from "node:assert/strict";
import {
  ready,
  operationReceipt,
} from "../src/bravo_console/static/operations.js";
import { now, snap } from "./fixtures.js";

test("new actions need fresh connected snapshot and complete event replay", () => {
  const model = {
    connected: true,
    error: null,
    snapshot: snap,
    state: snap.state,
    snapshotAt: now,
    cursor: 3,
    plan: snap.plan_id,
  };
  assert.equal(ready(model, now), true);
  for (const patch of [
    { connected: false },
    { error: "forbidden" },
    { cursor: 2 },
    { cursor: 4 },
    { snapshotAt: now + 1 },
    { state: "WATCH" },
  ])
    assert.equal(ready({ ...model, ...patch }, now), false);
  assert.equal(ready(model, now + 15000), false);
});

test("SHIFT receipt cannot masquerade as cutover approval", () => {
  const pending = { body: { expires_at: "2026-11-04T00:10:00Z" } };
  const receipt = {
    approval_kind: "shift",
    approval_id: "shift-one",
    actor_id: "operator",
    approved_at: "2026-11-04T00:00:00Z",
    expires_at: pending.body.expires_at,
  };
  assert.equal(
    operationReceipt("shift", receipt, pending).approval_id,
    "shift-one",
  );
  assert.throws(() =>
    operationReceipt(
      "shift",
      { ...receipt, approval_kind: "cutover" },
      pending,
    ),
  );
  assert.throws(() =>
    operationReceipt(
      "shift",
      { ...receipt, expires_at: "2026-12-01" },
      pending,
    ),
  );
});

test("rehearsal results must match the submitted task, version and attempt", () => {
  const pending = {
    taskId: "task-one",
    version: 3,
    attempt: 1,
    rehearsalId: "run-one",
  };
  const receipt = {
    task_id: "task-one",
    plan_version: 3,
    attempt: 1,
    rehearsal_id: "run-one",
  };
  operationReceipt("rehearsal-reconcile", receipt, pending);
  for (const patch of [
    { attempt: 2 },
    { task_id: "other-task" },
    { plan_version: 4 },
    { rehearsal_id: "other-run" },
  ])
    assert.throws(() =>
      operationReceipt(
        "rehearsal-reconcile",
        { ...receipt, ...patch },
        pending,
      ),
    );
});

test("a foreign failback command or remote plan is rejected", () => {
  const pending = { plan: "plan-demo", commandId: "command-one" };
  operationReceipt(
    "failback-reconcile",
    { command_id: "command-one" },
    pending,
  );
  assert.throws(() =>
    operationReceipt(
      "failback-reconcile",
      { command_id: "other-command" },
      pending,
    ),
  );
  assert.throws(() =>
    operationReceipt(
      "failback-reconcile",
      { command_id: "command-one", snapshot: { plan_id: "other-plan" } },
      pending,
    ),
  );
});
