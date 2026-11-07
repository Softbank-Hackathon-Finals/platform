import test from "node:test";
import assert from "node:assert/strict";
import {
  deliveryDocument,
  deliveryFresh,
  iacPlan,
  infraApproval,
  approvalUsable,
  pullRequest,
  createdIacId,
} from "../src/bravo_console/static/delivery-model.js";
import { snap, now } from "./fixtures.js";
const model = {
  plan: snap.plan_id,
  snapshot: snap,
  state: snap.state,
  snapshotAt: now,
  connected: true,
  cursor: 3,
};
const doc = {
  kind: "console-delivery.v1",
  plan_id: snap.plan_id,
  plan_digest: snap.plan_digest,
  plan_version: snap.version,
  subject_id: "requester",
  queried_at: new Date(now).toISOString(),
  expires_at: new Date(now + 15000).toISOString(),
  authorizes_execution: false,
  iac: {
    configured: true,
    actions: { apply_infra: true, approve_infra: false },
    modules: [
      {
        environment_id: "env-target",
        environment: "target-dev",
        module: "target-object-exposure",
        params: { state: "closed" },
      },
    ],
  },
  customers: [
    {
      environment_id: "env-target",
      environment: "target-dev",
      expected_revision: "b".repeat(40),
      status: "SYNC_RETRYING",
      provenance: "synthetic",
      retry_count: 2,
      reasons: ["OPERATION_RETRYING"],
      observed_at: new Date(now).toISOString(),
      valid_until: new Date(now + 300000).toISOString(),
      cluster_identity_matches: true,
      authorizes_cutover: false,
      migration_verified: false,
      control_equivalence_verified: false,
    },
  ],
};
const plan = {
  iac_plan_id: "iac-one",
  environment: "target-dev",
  module: "target-object-exposure",
  base_sha: "b".repeat(40),
  plan_digest: "c".repeat(64),
  accepted: true,
  refusals: [],
  effects: [],
  changes: 1,
  requested_by: "requester",
  binding: {
    infra_plan_id: "iac-one",
    plan_digest: "c".repeat(64),
    requested_by: "requester",
  },
};
const approved = {
  approval_id: "approved-one",
  approval_kind: "infra",
  actor_id: "other-operator",
  binding: plan.binding,
  approved_at: new Date(now).toISOString(),
  expires_at: new Date(now + 300000).toISOString(),
  revoked: false,
  claimed_run_id: null,
};
test("binds registered modules and customer status to the same plan with short read authority", () => {
  assert.deepEqual(deliveryDocument(doc, model, now), doc);
  assert.equal(deliveryFresh(doc, model, now + 14999), true);
  assert.equal(deliveryFresh(doc, model, now + 15000), false);
  assert.equal(deliveryFresh(doc, { ...model, connected: false }, now), false);
  assert.equal(doc.customers[0].migration_verified, false);
});
test("foreign, malformed, duplicate and fabricated customer success documents rejected", () => {
  const changes = [
    { plan_id: "foreign" },
    { plan_digest: "d".repeat(64) },
    { plan_version: 2 },
    { subject_id: null },
    { authorizes_execution: true },
    { expires_at: new Date(now + 16000).toISOString() },
    { customers: [{ ...doc.customers[0], environment_id: "foreign" }] },
    { customers: [...doc.customers, ...doc.customers] },
    { customers: [{ ...doc.customers[0], status: "SYNC_VERIFIED" }] },
    { customers: [{ ...doc.customers[0], retry_count: true }] },
    { customers: [{ ...doc.customers[0], migration_verified: true }] },
    { customers: [{ ...doc.customers[0], cluster_identity_matches: false }] },
    { iac: { ...doc.iac, modules: [...doc.iac.modules, ...doc.iac.modules] } },
  ];
  for (const change of changes)
    assert.throws(() => deliveryDocument({ ...doc, ...change }, model, now));
});
test("IaC identity and binding cannot be substituted, even for identical digests", () => {
  assert.deepEqual(iacPlan(plan, "iac-one"), plan);
  for (const change of [
    { iac_plan_id: "other" },
    { binding: { ...plan.binding, infra_plan_id: "other" } },
    { binding: { ...plan.binding, requested_by: "other" } },
    { plan_digest: "main" },
    { base_sha: "main" },
  ])
    assert.throws(() => iacPlan({ ...plan, ...change }, "iac-one"));
});
test("requester self approval, expired or revoked approval never enables PR", () => {
  assert.deepEqual(infraApproval(approved, plan, now), approved);
  assert.equal(approvalUsable(approved, plan, now), true);
  for (const change of [
    { actor_id: "requester" },
    { revoked: true },
    { revoked: undefined },
    { claimed_run_id: "other" },
    { binding: { ...approved.binding, infra_plan_id: "other" } },
    { expires_at: new Date(now).toISOString() },
  ])
    assert.equal(approvalUsable({ ...approved, ...change }, plan, now), false);
  assert.equal(
    approvalUsable(
      { ...approved, claimed_run_id: plan.iac_plan_id },
      plan,
      now,
    ),
    true,
  );
});
test("only the exact native PR receipt and fixed repository URL are linked", () => {
  const receipt = {
    iac_plan_id: plan.iac_plan_id,
    plan_digest: plan.plan_digest,
    approval_id: approved.approval_id,
    number: 701,
    url: "https://github.com/Softbank-Hackathon-Finals/platform/pull/701",
    base_sha: plan.base_sha,
    head_sha: "d".repeat(40),
  };
  assert.deepEqual(pullRequest(receipt, plan, approved.approval_id), receipt);
  for (const change of [
    { url: "javascript:alert(1)" },
    { url: "https://evil.invalid/701" },
    { iac_plan_id: "other" },
    { approval_id: "other" },
    { plan_digest: "d".repeat(64) },
    { number: true },
  ])
    assert.throws(() =>
      pullRequest({ ...receipt, ...change }, plan, approved.approval_id),
    );
});
test("lost creation responses use the native deterministic ID for readback, never a fresh key", async () => {
  const a = await createdIacId("plan-demo", "stable-key");
  assert.match(a, /^iac-[a-f0-9]{24}$/);
  assert.equal(a, await createdIacId("plan-demo", "stable-key"));
  assert.notEqual(a, await createdIacId("foreign-plan", "stable-key"));
});
