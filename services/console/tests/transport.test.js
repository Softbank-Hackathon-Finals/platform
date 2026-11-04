import test from "node:test";
import assert from "node:assert/strict";
import {
  SSEParser,
  request,
  readJson,
  stream,
} from "../src/bravo_console/static/transport.js";
import { events, plan } from "./fixtures.js";
const frame = (e) =>
  `id: ${e.event_id}\nevent: ${e.event_type}\ndata: ${JSON.stringify(e)}\n\n`;
test("fragmented SSE preserves cursor envelope, comments do not emit events", () => {
  const got = [];
  let beats = 0;
  const p = new SSEParser(
    (e) => got.push(e),
    () => beats++,
  );
  for (const ch of `retry: 1000\n\n: keep-alive\n\n${frame(events[0])}`)
    p.feed(ch);
  p.finish();
  assert.deepEqual(got, [events[0]]);
  assert.equal(beats, 1);
});
test("SSE envelope mismatch, duplicate ID, oversize and truncation rejected", () => {
  for (const text of [
    frame(events[0]).replace("id: plan-demo:1", "id: plan-demo:2"),
    frame(events[0]).replace("event:", "id: extra\nevent:"),
    "x".repeat(65537),
  ])
    assert.throws(() => new SSEParser(() => {}).feed(text));
  const p = new SSEParser(() => {});
  p.feed("data: {");
  assert.throws(() => p.finish());
});
test("POST uses same frozen idempotency key and no token in URL/storage", async () => {
  const calls = [];
  const fetcher = async (...args) => {
    calls.push(args);
    return Response.json({ ok: true });
  };
  for (let i = 0; i < 2; i++)
    await request("/v1/plans/demo/approvals", {
      body: { expected_version: 7 },
      key: "stable-key",
      fetcher,
    });
  assert.deepEqual(calls[0], calls[1]);
  assert.equal(calls[0][1].headers["X-Bravo-Console"], "1");
  assert.equal(calls[0][1].credentials, "same-origin");
  assert.equal(calls[0][1].redirect, "error");
});
test("JSON response limit and fixed-code errors do not expose private body", async () => {
  await assert.rejects(
    () => readJson(Response.json({ x: "x".repeat(140000) })),
    /response_limit/,
  );
  await assert.rejects(
    () =>
      readJson(
        Response.json(
          { detail: { code: "PRIVATE SECRET !" } },
          { status: 500 },
        ),
      ),
    /request_rejected/,
  );
  await assert.rejects(
    () => request("https://outside.invalid/", {}),
    /invalid_api_path/,
  );
});
test("resume sends exact Last-Event-ID and replays the next frame", async () => {
  let call;
  const got = [];
  await stream(plan, 2, {
    follow: false,
    onEvent: (e) => got.push(e),
    fetcher: async (...args) => {
      call = args;
      return new Response(frame(events[2]), {
        headers: { "content-type": "text/event-stream" },
      });
    },
  });
  assert.equal(call[1].headers["Last-Event-ID"], plan + ":2");
  assert.equal(call[0], `/v1/plans/${plan}/events?follow=false`);
  assert.deepEqual(got, [events[2]]);
});
test("stream permission rejection never enters event callback", async () => {
  await assert.rejects(
    () =>
      stream(plan, 0, {
        onEvent: () => assert.fail("leak"),
        fetcher: async () =>
          Response.json({ detail: { code: "forbidden" } }, { status: 403 }),
      }),
    (e) => e.status === 403,
  );
});

test("permission rejection remains authoritative without a JSON error body", async () => {
  for (const status of [401, 403])
    await assert.rejects(
      () =>
        readJson(
          new Response("<html>login</html>", {
            status,
            headers: { "content-type": "text/html" },
          }),
        ),
      (e) => e.status === status && e.code === "forbidden",
    );
});
test("invalid UTF8 locks the stream instead of treating it as a network retry", async () => {
  await assert.rejects(
    () =>
      stream(plan, 0, {
        onEvent: () => assert.fail("unexpected event"),
        fetcher: async () =>
          new Response(new Uint8Array([255]), {
            headers: { "content-type": "text/event-stream" },
          }),
      }),
    (e) => e.code === "invalid_stream_encoding",
  );
});
