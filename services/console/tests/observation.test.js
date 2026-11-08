import test from "node:test";
import assert from "node:assert/strict";
import { observation } from "../src/bravo_console/static/model.js";

const NOW = Date.parse("2026-11-07T00:00:10Z");
const current = {
  plan_id: "plan-demo",
  plan_digest: "sha256:" + "a".repeat(64),
  source_environment_id: "env-source",
  target_environment_id: "env-target",
};
const row = (side, extra = {}) => ({
  side,
  environment_id: `env-${side}`,
  state: "OK",
  queried_at: "2026-11-07T00:00:09Z",
  last_received_seconds_ago: 1,
  requests_per_second: 10,
  error_ratio: 0.01,
  p95_seconds: 0.2,
  ...extra,
});
const paused = {
  state: "PAUSED",
  last_received_seconds_ago: 2,
  databases: [
    { pooler: "target-pooler", name: "chat", paused: true, clients_waiting: 12 },
  ],
};
const doc = (target = {}, source = {}) => ({
  kind: "console-observation.v1",
  plan_id: current.plan_id,
  plan_digest: current.plan_digest,
  for_gate_decisions: false,
  queried_at: "2026-11-07T00:00:09Z",
  environments: [row("source", { pooler: null, ...source }), row("target", { pooler: paused, ...target })],
});

test("pooler state and heartbeat age are carried for display", () => {
  const [source, target] = observation(doc(), current, NOW);
  assert.equal(source.received, 1);
  assert.deepEqual(source.pooler, { state: "NOT_CONFIGURED" });
  assert.deepEqual(target.pooler, { state: "PAUSED", received: 2, paused: ["chat"], waiting: 12 });
});

test("older hosts without pooler or age fields still render", () => {
  const legacy = doc();
  for (const r of legacy.environments) {
    delete r.pooler;
    delete r.last_received_seconds_ago;
  }
  const rows = observation(legacy, current, NOW);
  assert.deepEqual(rows.map((r) => [r.received, r.pooler.state]), [[null, "NOT_CONFIGURED"], [null, "NOT_CONFIGURED"]]);
});

test("unknown waiting count stays unknown instead of becoming zero", () => {
  const value = doc({ pooler: { ...paused, databases: [{ ...paused.databases[0], clients_waiting: null }] } });
  assert.equal(observation(value, current, NOW)[1].pooler.waiting, null);
});

for (const [name, pooler] of [
  ["label disagrees with rows", { ...paused, state: "OK" }],
  ["unobservable with leftover rows", { ...paused, state: "UNOBSERVABLE" }],
  ["unknown state", { ...paused, state: "MAINTENANCE" }],
  ["old heartbeat", { ...paused, last_received_seconds_ago: 31 }],
  ["missing age", { ...paused, last_received_seconds_ago: null }],
  ["no databases", { ...paused, databases: [] }],
  ["paused not boolean", { ...paused, databases: [{ ...paused.databases[0], paused: "yes" }] }],
  ["negative waiting", { ...paused, databases: [{ ...paused.databases[0], clients_waiting: -1 }] }],
  ["too many databases", { ...paused, databases: Array.from({ length: 33 }, (_, i) => ({ ...paused.databases[0], name: `d${i}` })) }],
]) {
  test(`invalid pooler view is rejected: ${name}`, () => {
    assert.throws(() => observation(doc({ pooler }), current, NOW), /invalid_observation_pooler/);
  });
}

test("implausible HTTP heartbeat age is rejected", () => {
  assert.throws(() => observation(doc({ last_received_seconds_ago: 99 }), current, NOW), /invalid_observation_value/);
});

test("down pooler is shown as down even when databases are paused", () => {
  const down = { ...paused, state: "DOWN" };
  assert.equal(observation(doc({ pooler: down }), current, NOW)[1].pooler.state, "DOWN");
});
