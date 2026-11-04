import { test, expect } from "@playwright/test";

test("actual retained full report shows unknown controls and no cutover authority", async ({
  page,
  context,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await context.addCookies([
    {
      name: "local-validation",
      value: "browser-test-only",
      url: "http://127.0.0.1:18122",
      sameSite: "Strict",
    },
  ]);
  const writes = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.url());
  });
  await page.goto("/console/");
  await page.locator("#plan").fill("live-audit-validation");
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await expect(page.locator("#control-rows tr")).toHaveCount(20, {
    timeout: 15000,
  });
  await expect(page.locator("#control-status")).toContainText("BLOCK");
  await expect(page.locator("#gate-status")).toHaveText("BLOCK");
  const report = await page.evaluate(async () =>
    (await fetch("/v1/plans/live-audit-validation/controls")).json(),
  );
  expect(report.decision).toBe("BLOCK");
  expect(report.authorizes_cutover).toBe(false);
  expect(report.report_digest).toBe(process.env.BRAVO_TEST_GATE_SHA256);
  if (["restore-partial", "source-scan-partial"].includes(process.env.BRAVO_TEST_REPORT_KIND)) {
    expect(report.rows.every((row) => row.decision === "BLOCK")).toBe(true);
    const testId = process.env.BRAVO_TEST_REPORT_KIND === "source-scan-partial" ? "SEC-01.2" : "DR-01.1";
    const observed = report.rows.find((row) => row.test_id === testId);
    expect(observed.source_verdict).toBe("PASS");
    expect(observed.target_verdict).toBe("UNKNOWN");
    expect(observed.reason).toBe("TARGET_UNKNOWN");
    expect(
      report.rows.filter((row) => row.source_verdict === "UNKNOWN"),
    ).toHaveLength(19);
  } else {
    expect(report.rows.filter((row) => row.decision === "PASS")).toHaveLength(
      3,
    );
    expect(
      report.rows.filter((row) => row.source_verdict === "UNKNOWN"),
    ).toHaveLength(17);
  }
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
  await expect(page.locator("#confirm")).toBeDisabled();
  expect(writes).toEqual([]);
  await page.screenshot({
    path: test.info().outputPath("actual-audit-block.png"),
    fullPage: true,
  });
  await context.clearCookies();
  await expect(page.locator("#notice")).toHaveText("접근 권한 또는 로그인 상태를 확인해 주세요.", {
    timeout: 15000,
  });
  expect(pageErrors).toEqual([]);
  await expect(page.locator("#control-rows tr")).toHaveCount(0);
  await expect(page.locator("#approve")).toBeDisabled();
});
