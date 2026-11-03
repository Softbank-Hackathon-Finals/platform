import { test, expect } from "@playwright/test";
test("unauthenticated browser sees no plan data or approval permission", async ({
  page,
}) => {
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-demo");
  await page.locator("#connect").click();
  await expect(page.locator("#notice")).toContainText("권한");
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#workload")).toHaveText("—");
});
test("real T10 HTTP, SSE, bound approval and unknown execution remain separate", async ({
  page,
  context,
}) => {
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
  await expect(page.locator("#workload")).toHaveText("demo-chat");
  await expect(page.locator("#binding")).toContainText("sha256:");
  await expect(page.locator("#control-rows tr")).toHaveCount(3, {
    timeout: 15000,
  });
  await expect(page.locator("#control-status")).toContainText(
    "기록된 보안 판정 PASS",
  );
  await expect(page.locator("#approve")).toBeDisabled();
  await page.locator("#confirm").check();
  await page.locator("#approve").click();
  await expect(page.locator("#action-status")).toContainText("승인 기록 완료");
  await expect(page.locator("#state")).toHaveText("트래픽 전환");
  await page.locator("#confirm").check();
  await expect(page.locator("#execute")).toBeEnabled();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#execute").click();
  await expect(page.locator("#state")).toHaveText("안전 보류");
  await expect(page.locator("#action-status")).toContainText(
    "실행 결과 미확인",
  );
  await expect(page.locator("#execute")).toBeDisabled();
  await expect(page.locator("#reconcile")).toBeVisible();
  await page.locator("#reconcile").click();
  await expect(page.locator("#action-status")).toContainText(
    "실행 결과 미확인",
  );
  await page.screenshot({
    path: test.info().outputPath("console.png"),
    fullPage: true,
  });
});
test("mobile layout has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/console/");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
test("revoked browser session clears previously displayed plan data", async ({
  page,
  context,
}) => {
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
  await expect(page.locator("#workload")).toHaveText("demo-chat");
  await context.clearCookies();
  await expect(page.locator("#notice")).toHaveText(
    "접근 권한 또는 로그인 상태를 확인해 주세요.",
    { timeout: 15000 },
  );
  await expect(page.locator("#workload")).toHaveText("—");
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#history li")).toHaveCount(0);
  await expect(page.locator("#control-rows tr")).toHaveCount(0);
});

test("report permission loss clears the view even when plan reads still succeed", async ({
  page,
  context,
}) => {
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
  await expect(page.locator("#control-rows tr")).toHaveCount(3, {
    timeout: 15000,
  });
  await page.route("**/v1/plans/plan-demo/controls", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: '{"detail":{"code":"forbidden"}}',
    }),
  );
  await expect(page.locator("#notice")).toHaveText(
    "접근 권한 또는 로그인 상태를 확인해 주세요.",
    { timeout: 15000 },
  );
  await expect(page.locator("#workload")).toHaveText("—");
  await expect(page.locator("#control-rows tr")).toHaveCount(0);
  await expect(page.locator("#approve")).toBeDisabled();
});
