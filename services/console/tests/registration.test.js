import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  environmentChoices,
  environmentKinds,
  environmentRoles,
  RegistrationAttempt,
  registrationInput,
  registrationReceipt,
} from "../src/bravo_console/static/registration.js";
import { request } from "../src/bravo_console/static/transport.js";
const input = {
  name: "source",
  kind: "aws",
  roles: ["source"],
  connection_ref: "source-reader",
};
const receipt = {
  ...input,
  environment_id: "env-source",
  role: "source",
  checks: { read_access: "verified", warnings: ["write_scope_unknown"] },
  registered_at: "2026-11-04T10:00:00Z",
};
const response = (value) =>
  new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    status: 201,
  });

test("registration accepts only fixed fields and registered aliases", () => {
  assert.deepEqual(registrationInput("environment", input), input);
  // ADR-0034: every kind x role pair is a valid request; the server catalog decides.
  for (const kind of ["aws", "gcp", "onprem"])
    for (const role of ["source", "target"]) {
      const body = { ...input, kind, roles: [role] };
      assert.deepEqual(registrationInput("environment", body), body);
    }
  for (const body of [
    { ...input, token: "secret" },
    { ...input, connection_ref: "https://foreign.invalid" },
    { ...input, kind: "azure" },
    { ...input, roles: ["both"] },
    { ...input, roles: [] },
    { ...input, kind: "onprem", roles: ["source", "target"] },
    { ...input, kind: "onprem-source" },
  ])
    assert.throws(() => registrationInput("environment", body));
  // The screen offers every kind x role once; the P0 choices keep their values.
  assert.deepEqual(
    Array.from(
      environmentChoices.values(),
      ({ kind, role }) => kind + "/" + role,
    ).sort(),
    environmentKinds
      .flatMap((kind) => environmentRoles.map((role) => kind + "/" + role))
      .sort(),
  );
  assert.deepEqual(Array.from(environmentChoices).slice(0, 3), [
    ["aws", { kind: "aws", role: "source" }],
    ["onprem", { kind: "onprem", role: "target" }],
    ["onprem-source", { kind: "onprem", role: "source" }],
  ]);
  const screen = readFileSync(
    new URL("../src/bravo_console/static/index.html", import.meta.url),
    "utf8",
  );
  const select = screen.match(/<select name="kind">([\s\S]*?)<\/select>/)[1];
  assert.deepEqual(
    Array.from(select.matchAll(/<option value="([^"]+)">/g), (m) => m[1]),
    Array.from(environmentChoices.keys()),
  );
  const app = {
    name: "app",
    repo_url: "https://example.invalid/app",
    app_path: "app",
    iac_path: "infra",
    source_environment_id: "env-source",
    namespace: "app",
  };
  for (const change of [
    { repo_url: "https://user:password@example.invalid/app" },
    { repo_url: "https://example.invalid/app?token=secret" },
    { app_path: "../private" },
  ])
    assert.throws(() => registrationInput("workload", { ...app, ...change }));
});

test("response loss preserves exact frozen input and key for explicit recovery", async () => {
  const original = structuredClone(input),
    calls = [];
  const attempt = new RegistrationAttempt(
    "environment",
    original,
    "stable-key",
  );
  original.connection_ref = "changed";
  await assert.rejects(
    attempt.send({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        throw new TypeError("response lost");
      },
    }),
  );
  assert.equal(calls.length, 1);
  const result = await attempt.send({
    fetcher: async (path, options) => {
      calls.push({ path, options });
      return response(receipt);
    },
  });
  assert.equal(result.id, "env-source");
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[1].options.credentials, "same-origin");
  assert.equal(calls[1].options.headers["Idempotency-Key"], "stable-key");
  assert.equal(
    JSON.parse(calls[1].options.body).connection_ref,
    "source-reader",
  );
});

test("duplicate click cannot send a second registration", async () => {
  let finish;
  const attempt = new RegistrationAttempt("environment", input, "one-key");
  const pending = attempt.send({
    fetcher: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  });
  await assert.rejects(
    attempt.send({ fetcher: () => assert.fail("second call") }),
  );
  finish(response(receipt));
  await pending;
});

test("unbound receipt or missing read verification stays uncertain", () => {
  for (const change of [
    { environment_id: "" },
    { role: "target" },
    { connection_ref: "wrong" },
    { checks: {} },
    { registered_at: "not-a-time" },
  ])
    assert.throws(() =>
      registrationReceipt("environment", input, { ...receipt, ...change }),
    );
  assert.deepEqual(
    registrationReceipt("environment", input, receipt).warnings,
    ["write_scope_unknown"],
  );
});

test("registration endpoint is exact and POST only", async () => {
  for (const path of [
    "/v1/environments",
    "/v1/workloads",
    "/v1/plans",
    "/v1/environments/admin",
    "https://foreign.invalid/v1/plans",
  ])
    await assert.rejects(
      request(path, { fetcher: () => assert.fail("unexpected request") }),
    );
});

test("plan receipt is initial COLLECT and bound to selected app and destination", () => {
  const body = {
    workload_id: "workload-one",
    target_environment_id: "env-target",
    profile_id: "fixture-profile",
    profile_version: "1.0",
  };
  const value = {
    ...body,
    plan_id: "plan-one",
    source_environment_id: "env-source",
    state: "COLLECT",
    version: 1,
    plan_digest: "a".repeat(64),
    collection_scope_digest: "b".repeat(64),
    warnings: [],
    registered_at: receipt.registered_at,
  };
  assert.equal(registrationReceipt("plan", body, value).id, "plan-one");
  for (const change of [
    { state: "DONE" },
    { version: 2 },
    { workload_id: "other" },
    { target_environment_id: "other" },
    { plan_digest: "invalid" },
    { profile_id: "other" },
    { profile_version: "2.0" },
    { profile_id: undefined },
    { profile_version: undefined },
    { profile_version: 1 },
  ])
    assert.throws(() =>
      registrationReceipt("plan", body, { ...value, ...change }),
    );
});

test("plan registration requires an explicit complete profile without defaults", () => {
  const body = {
    workload_id: "workload-one",
    target_environment_id: "env-target",
    profile_id: "fixture-profile",
    profile_version: "1.0",
  };
  assert.deepEqual(registrationInput("plan", body), body);
  for (const key of ["profile_id", "profile_version"]) {
    const missing = { ...body };
    delete missing[key];
    assert.throws(() => registrationInput("plan", missing));
    for (const value of [
      "",
      null,
      1,
      "../profile",
      "contains spaces",
      "x".repeat(129),
    ])
      assert.throws(() => registrationInput("plan", { ...body, [key]: value }));
  }
});

test("plan response recovery keeps the explicitly chosen profile and request key", async () => {
  const body = {
    workload_id: "workload-one",
    target_environment_id: "env-target",
    profile_id: "fixture-profile",
    profile_version: "1.0",
  };
  const attempt = new RegistrationAttempt("plan", body, "one-plan-key");
  const calls = [];
  const value = {
    ...body,
    plan_id: "plan-one",
    source_environment_id: "env-source",
    state: "COLLECT",
    version: 1,
    plan_digest: "a".repeat(64),
    collection_scope_digest: "b".repeat(64),
    warnings: [],
    registered_at: receipt.registered_at,
  };
  body.profile_version = "2.0";
  await assert.rejects(
    attempt.send({
      fetcher: async (path, options) => {
        calls.push({ path, options });
        throw new TypeError("lost response");
      },
    }),
  );
  const result = await attempt.send({
    fetcher: async (path, options) => {
      calls.push({ path, options });
      return response(value);
    },
  });
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(result.profile_version, "1.0");
  assert.equal(result.profile_id, "fixture-profile");
});

test("recheck profile stays in registration payload and receipt, or both fields remain absent", () => {
  const body = {
    workload_id: "workload-one",
    target_environment_id: "env-target",
    profile_id: "demo-gate",
    profile_version: "1",
    recheck_profile_id: "apply-recheck",
    recheck_profile_version: "1",
  };
  const value = {
    ...body,
    plan_id: "plan-one",
    source_environment_id: "env-source",
    state: "COLLECT",
    version: 1,
    plan_digest: "a".repeat(64),
    collection_scope_digest: "b".repeat(64),
    warnings: [],
    registered_at: receipt.registered_at,
  };
  assert.deepEqual(registrationInput("plan", body), body);
  assert.equal(
    registrationReceipt("plan", body, value).recheck_profile_id,
    "apply-recheck",
  );
  const attempt = new RegistrationAttempt("plan", body, "stable-recheck-key");
  assert.deepEqual(JSON.parse(attempt.body), body);
  for (const key of ["recheck_profile_id", "recheck_profile_version"]) {
    const partial = { ...body };
    delete partial[key];
    assert.throws(() => registrationInput("plan", partial));
    for (const invalid of [undefined, "other", null, 1])
      assert.throws(() =>
        registrationReceipt("plan", body, { ...value, [key]: invalid }),
      );
    assert.throws(() => registrationReceipt("plan", partial, value));
  }
});
