import { test, expect } from "@playwright/test";

async function connect(page, context) {
  await context.addCookies([
    {
      name: "fixture-session",
      value: "test-only",
      url: "http://127.0.0.1:18121",
      sameSite: "Strict",
    },
  ]);
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-demo");
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
}

test("workspace navigation and resource selection issue no change requests", async ({
  page,
  context,
}) => {
  const changes = [];
  page.on("request", (request) => {
    if (request.method() === "POST") changes.push(request.url());
  });
  await connect(page, context);
  await page.locator('[data-view="topology"]').click();
  await expect(page.locator("#view-title")).toHaveText("워크로드 지도");
  await expect(page.locator(".topology-card")).toBeVisible();
  await expect(page.locator(".approval-card")).toBeHidden();
  await page.locator('[data-map-node="target"]').click();
  await expect(page.locator("#inspector-id")).toHaveText("env-target");
  await expect(
    page.getByRole("button", { name: "격리 실행", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "장애 주입", exact: true }),
  ).toBeDisabled();
  await page.locator('[data-view="security"]').click();
  await expect(page.locator(".controls-card")).toBeVisible();
  await expect(page.locator(".topology-card")).toBeHidden();
  await page.locator('[data-view="monitoring"]').click();
  await expect(page.locator(".evidence-card")).toBeVisible();
  await page.locator('[data-view="timeline"]').click();
  await expect(page.locator("#history")).toBeVisible();
  await page.locator('[data-view="overview"]').click();
  await expect(page.locator(".approval-card")).toBeVisible();
  expect(changes).toEqual([]);
});

test("permission loss clears the new map and selected resource inspector", async ({
  page,
  context,
}) => {
  await connect(page, context);
  await page.locator('[data-map-node="target"]').click();
  await expect(page.locator("#inspector-id")).toHaveText("env-target");
  await context.clearCookies();
  await expect(page.locator("#notice")).toContainText("접근 권한", {
    timeout: 12000,
  });
  await expect(page.locator("#map-source-id")).toHaveText("등록된 환경 없음");
  await expect(page.locator("#map-target-id")).toHaveText("등록된 환경 없음");
  await expect(page.locator("#map-workload-id")).toHaveText("등록된 앱 없음");
  await expect(page.locator("#inspector-id")).toHaveText("등록된 환경 없음");
  await expect(page.locator("#execute")).toBeDisabled();
});

test("plan input accepts hyphens and rejects punctuation with native form validation", async ({
  page,
}) => {
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-valid_1");
  expect(
    await page.locator("#plan").evaluate((input) => input.checkValidity()),
  ).toBe(true);
  await page.locator("#plan").fill("plan/invalid");
  expect(
    await page
      .locator("#plan")
      .evaluate((input) => input.validity.patternMismatch),
  ).toBe(true);
});
