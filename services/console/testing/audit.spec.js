import { test, expect } from "@playwright/test";
for (const failure of [503, 403])
  test(`stored T11 denial displayed and cleared on ${failure}`, async ({
    page,
    context,
  }) => {
    const errors = [],
      writes = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => {
      if (request.method() !== "GET") writes.push(request.url());
    });
    await context.addCookies([
      {
        name: "local-validation",
        value: "browser-test-only",
        url: "http://127.0.0.1:18124",
        sameSite: "Strict",
      },
    ]);
    await page.goto("/console/");
    await page.locator("#plan").fill("plan-audit");
    await page.locator("#connect").click();
    await expect(page.locator("#audit-rows li")).toHaveCount(1);
    await expect(page.locator("#audit-rows")).toContainText(
      "버킷 삭제 시도 차단 — 에이전트 도구 정책",
    );
    await expect(page.locator("#audit-rows")).toContainText("run-browser:1");
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const response = await page.request.get("/v1/plans/plan-audit/audit");
    const record = await response.json();
    expect(record.rows[0].reason).toBe("DESTRUCTIVE_ATTEMPT_BLOCKED");
    expect(record.retention_verified).toBe(false);
    expect(JSON.stringify(record)).not.toContain("aws s3 rb");
    await expect(page.locator("#approve")).toBeDisabled();
    await expect(page.locator("#execute")).toBeDisabled();
    await page.route("**/v1/plans/plan-audit/audit", (route) =>
      route.fulfill({
        status: failure,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "audit_unavailable" } }),
      }),
    );
    await expect(page.locator("#audit-status")).toContainText("확인 불가", {
      timeout: 15000,
    });
    await expect(page.locator("#audit-rows li")).toHaveCount(0);
    expect(errors).toEqual([]);
    expect(writes).toEqual([]);
  });
