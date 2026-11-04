import { test, expect } from "@playwright/test";

test("actual T37 observations render without granting execution and disappear on logout", async ({
  page,
  context,
}) => {
  await context.addCookies([
    {
      name: "local-validation",
      value: process.env.BRAVO_BROWSER_COOKIE,
      url: process.env.BRAVO_BROWSER_ORIGIN,
      sameSite: "Strict",
    },
  ]);
  const writes = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.method());
  });
  await page.goto("/console/");
  await page.locator("#plan").fill("local-observation-validation");
  await page.locator("#connect").click();
  await expect(page.locator("#workload")).toHaveText("demo-chat");
  await expect(page.locator("#source-http")).toContainText("%", {
    timeout: 15000,
  });
  const result = await page.evaluate(async () =>
    (await fetch("/v1/plans/local-observation-validation/observations")).json(),
  );
  expect(result.for_gate_decisions).toBe(false);
  const target = result.environments.find(
    (row) => row.environment === "onprem",
  );
  if (process.env.BRAVO_BROWSER_PHASE === "target-stopped") {
    expect(target.state).toBe("UNOBSERVABLE");
    await expect(page.locator("#target-http")).toHaveText("미확인");
  } else {
    expect(target.state).toBe("OK");
    await expect(page.locator("#target-http")).toContainText("%");
  }
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
  expect(writes).toEqual([]);
  await page.screenshot({
    path: test.info().outputPath("actual-observations.png"),
    fullPage: true,
  });
  await context.clearCookies();
  // The normal connected notice also contains "권한". Wait for the actual
  // revocation response rather than treating that earlier text as revocation.
  await expect(page.locator("#notice")).toHaveText(
    "접근 권한 또는 로그인 상태를 확인해 주세요.",
    {
      timeout: 15000,
    },
  );
  await expect(page.locator("#source-http")).toHaveText("미확인");
  await expect(page.locator("#target-http")).toHaveText("미확인");
  await expect(page.locator("#execute")).toBeDisabled();
});
