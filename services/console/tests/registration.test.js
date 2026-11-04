import test from "node:test";
import assert from "node:assert/strict";
import {
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
  for (const body of [
    { ...input, token: "secret" },
    { ...input, connection_ref: "https://foreign.invalid" },
    { ...input, roles: ["target"] },
    { ...input, kind: "gcp" },
  ])
    assert.throws(() => registrationInput("environment", body));
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
  ])
    assert.throws(() =>
      registrationReceipt("plan", body, { ...value, ...change }),
    );
});
