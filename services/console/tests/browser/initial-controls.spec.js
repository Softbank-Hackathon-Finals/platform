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
