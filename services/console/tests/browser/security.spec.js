import { test, expect } from "@playwright/test";
async function open(page, context) {
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
  await expect(page.locator("#security-status")).toHaveText("환경별 수집 상태");
  await expect(page.locator("#security-rows tr")).toHaveCount(7);
  await expect(page.locator("#security-rows tr").first()).toContainText(
    "미해결 탐지 3건",
  );
}
test("real console read shows collection counts and erases them on plan switch", async ({
  page,
  context,
}) => {
  await open(page, context);
  await expect(page.locator("#security-rows")).not.toContainText("PASS");
  await page.locator("#plan").fill("plan-before");
  await page.locator("#connect").click();
  await expect(page.locator("#security-rows")).not.toContainText("3건");
  await expect(page.locator("#security-status")).toContainText("미연결");
});
test("security permission loss clears all protected information", async ({
  page,
  context,
}) => {
  await open(page, context);
  await page.route("**/v1/plans/plan-demo/security", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "forbidden" } }),
    }),
  );
  await expect(page.locator("#notice")).toContainText("접근 권한", {
    timeout: 12000,
  });
  await expect(page.locator("#security-rows")).not.toContainText("3건");
  await expect(page.locator("#workload")).toHaveText("—");
  await expect(page.locator("#execute")).toBeDisabled();
});
test("upstream failure clears old counts; missing and stale never become zero", async ({
  page,
  context,
}) => {
  await open(page, context);
  await page.route("**/v1/plans/plan-demo/security", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    for (const row of body.environments) {
      row.cells.IAM = { state: "UNKNOWN", counts: null };
      row.cells.AUDIT = { state: "STALE", counts: null };
      row.cells.THREAT = { state: "NONE", counts: null };
    }
    await route.fulfill({ response, json: body });
  });
  const rows = page.locator("#security-rows");
  await expect(rows).toContainText("수집 확인 불가", { timeout: 12000 });
  await expect(rows).toContainText("수집 결과 오래됨");
  await expect(rows).toContainText("탐지 출처 없음");
  await expect(rows).not.toContainText("0건");
  await page.unroute("**/v1/plans/plan-demo/security");
  await page.route("**/v1/plans/plan-demo/security", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
  );
  await expect(page.locator("#security-status")).toContainText("미연결", {
    timeout: 12000,
  });
  await expect(rows).not.toContainText("3건");
});
