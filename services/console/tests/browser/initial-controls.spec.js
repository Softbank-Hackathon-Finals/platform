import { test, expect } from "@playwright/test";

test("initial BLOCK table is visible before gate event without enabling actions", async ({ page, context }) => {
  await context.addCookies([{
    name: "fixture-session", value: "test-only",
    url: "http://127.0.0.1:18121", sameSite: "Strict",
  }]);
  const writes = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.url());
  });
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-initial-controls");
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await expect(page.locator("#control-status")).toHaveText(
    "demo-gate · 초기 비교 (실행 허가 아님) BLOCK",
  );
  await expect(page.locator("#control-rows tr")).toHaveCount(3);
  await expect(page.locator("#control-rows tr").first()).toContainText("UNKNOWN");
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
  const events = await (await page.request.get(
    "/v1/plans/plan-initial-controls/events?follow=false",
  )).text();
  expect(events).not.toContain('"event_type":"gate.completed"');
  expect(events).not.toContain('"event_type":"approval.decided"');
  expect(writes).toEqual([]);
});

test("known read expiry clears cached controls even when the same response arrives again", async ({ page, context }) => {
  await context.addCookies([{
    name: "fixture-session", value: "test-only",
    url: "http://127.0.0.1:18121", sameSite: "Strict",
  }]);
  let original;
  let reads = 0;
  await page.route("**/v1/plans/plan-initial-controls/controls", async (route) => {
    reads += 1;
    if (!original) {
      const response = await route.fetch();
      original = await response.json();
      original.read_valid_until = new Date(Date.parse(original.queried_at) + 7000).toISOString();
    }
    // Retry returns the same authenticated projection: arrival time must not
    // become a new read lease or keep a known expired comparison visible.
    await route.fulfill({ status: 200, json: original });
  });
  const writes = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.url());
  });
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-initial-controls");
  await page.locator("#connect").click();
  await expect(page.locator("#control-rows tr")).toHaveCount(3);
  // The active UI renders once per second. Do not let a later five-second
  // network poll hide a missing expiry check in the render path.
  const remaining = Date.parse(original.read_valid_until) - Date.now();
  await expect(page.locator("#control-status")).toHaveText("검증된 보고서 미연결", {
    timeout: Math.max(1, remaining + 1500),
  });
  await expect(page.locator("#control-rows tr")).toHaveCount(0);
  expect(reads).toBeGreaterThan(1);
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
  expect(writes).toEqual([]);
});
