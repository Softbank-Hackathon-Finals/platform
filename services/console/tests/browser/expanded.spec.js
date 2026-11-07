import { test, expect } from "@playwright/test";

async function connect(page, context, plan) {
  await context.addCookies([
    {
      name: "fixture-session",
      value: "test-only",
      url: "http://127.0.0.1:18121",
      sameSite: "Strict",
    },
  ]);
  await page.goto("/console/");
  await page.locator("#plan").fill(plan);
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await expect(page.locator("#operation-context")).toContainText("계획 버전", {
    timeout: 15000,
  });
}
async function action(page, name) {
  await page.locator("#operations-confirm").check();
  await expect(page.locator("#op-" + name)).toBeEnabled();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#op-" + name).click();
}
const stats = async (page, plan) =>
  (await page.request.get(`/v1/plans/${plan}/fixture-stats`)).json();

test("all added panels show owner documents, including costs, T24 and DB metrics", async ({
  page,
  context,
}) => {
  await connect(page, context, "plan-workspace-console");
  for (const name of [
    "inventory",
    "recommendations",
    "exit",
    "costs",
    "ai",
    "rehearsal",
    "traffic",
    "monitoring",
    "audit",
  ]) {
    await expect(page.locator(`#panel-${name}-status`)).toContainText("조회됨");
    expect(
      await page.locator(`#panel-${name}-body table`).count(),
    ).toBeGreaterThan(0);
  }
  await expect(page.locator("#control-rows tr")).toHaveCount(3);
  await expect(page.locator("#source-rps")).toHaveText("12");
  await expect(page.locator("#security-rows")).toContainText("수집됨");
  await expect(page.locator("#traffic-weight")).toHaveText(
    "출발 90% / 목적 10%",
  );
  await expect(page.locator("#pause")).toHaveText("420 ms");
  await expect(page.locator("#panel-costs-body")).toContainText("미관측");
  await expect(page.locator("#panel-audit-body")).toContainText(
    "DESTRUCTIVE_ATTEMPT_BLOCKED",
  );
  await expect(page.locator("#panel-monitoring-body")).toContainText(
    "SovaFixture",
  );
  expect(await stats(page, "plan-workspace-console")).toEqual({
    initial: 0,
    starts: 0,
    failback_posts: 0,
  });
});

test("SHIFT approval calls the existing API and never dispatches a cutover", async ({
  page,
  context,
}) => {
  await connect(page, context, "plan-shift-console");
  const request = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/approvals"),
  );
  await action(page, "shift");
  expect((await request).postDataJSON().approval_kind).toBe("shift");
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: shift",
  );
  expect(await stats(page, "plan-shift-console")).toEqual({
    initial: 0,
    starts: 0,
    failback_posts: 0,
  });
});

test("failback requires a separate approval and execute click and preserves UNKNOWN", async ({
  page,
  context,
}) => {
  await connect(page, context, "plan-failback-console");
  await expect(page.locator("#failback-binding-view")).toContainText(
    "env-target → env-source",
  );
  await action(page, "failback-approve");
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: failback-approve",
  );
  expect((await stats(page, "plan-failback-console")).failback_posts).toBe(0);
  await action(page, "failback-submit");
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: failback-submit",
  );
  expect((await stats(page, "plan-failback-console")).failback_posts).toBe(1);
  await expect(page.locator("#state")).not.toHaveText("출발지 복귀");
});

test("AI initial start and collect are explicit separate operator requests", async ({
  page,
  context,
}) => {
  await connect(page, context, "plan-initial-console");
  await page.locator('[data-view="ai"]').click();
  await action(page, "initial-start");
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: initial-start",
  );
  expect((await stats(page, "plan-initial-console")).initial).toBe(1);
  await action(page, "initial-collect");
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: initial-collect",
  );
  expect((await stats(page, "plan-initial-console")).initial).toBe(2);
});

test("lost rehearsal response is recovered through GET without another submit", async ({
  page,
  context,
}) => {
  const plan = "plan-uncertain-console";
  await connect(page, context, plan);
  let submits = 0;
  await page.route(`**/v1/plans/${plan}/rehearsals`, async (route) => {
    submits++;
    await route.fetch();
    await route.abort("failed");
  });
  await action(page, "rehearsal-start");
  await expect(page.locator("#operation-status")).toContainText(
    "응답을 확인하지 못했습니다",
  );
  await expect(page.locator("#operation-lookup")).toBeVisible();
  await page.locator("#operation-lookup").click();
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: rehearsal-start",
  );
  expect(submits).toBe(1);
  expect((await stats(page, plan)).starts).toBe(1);
  await action(page, "rehearsal-reconcile");
  await expect(page.locator("#operation-status")).toContainText("UNCONFIRMED");
  expect((await stats(page, plan)).starts).toBe(1);
});

test("an unknown pre-server request can only be resubmitted by explicit same-key action", async ({
  page,
  context,
}) => {
  const plan = "plan-before-console";
  await connect(page, context, plan);
  let block = true;
  const sent = [];
  await page.route(`**/v1/plans/${plan}/rehearsals`, async (route) => {
    sent.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    if (block) await route.abort("failed");
    else await route.continue();
  });
  await action(page, "rehearsal-start");
  await expect(page.locator("#operation-status")).toContainText(
    "응답을 확인하지 못했습니다",
  );
  await page.locator("#operation-lookup").click();
  await expect(page.locator("#operation-status")).toContainText("미확정");
  expect(sent).toHaveLength(1);
  expect((await stats(page, plan)).starts).toBe(0);
  block = false;
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#operation-repeat").click();
  await expect(page.locator("#operation-status")).toContainText(
    "서버 접수·조회 확인: rehearsal-start",
  );
  expect(sent).toHaveLength(2);
  expect(sent[0]).toEqual(sent[1]);
  expect((await stats(page, plan)).starts).toBe(1);
});

test("revocation clears supplementary data and disables all operator actions", async ({
  page,
  context,
}) => {
  await connect(page, context, "plan-rehearsal-console");
  await expect(page.locator("#panel-costs-body table").first()).toBeVisible();
  await context.clearCookies();
  await expect(page.locator("#notice")).toContainText("접근 권한", {
    timeout: 12000,
  });
  for (const name of [
    "inventory",
    "recommendations",
    "exit",
    "costs",
    "ai",
    "rehearsal",
    "traffic",
    "monitoring",
    "audit",
  ])
    expect(await page.locator(`#panel-${name}-body`).textContent()).toBe("");
  for (const name of [
    "shift",
    "failback-approve",
    "failback-submit",
    "rehearsal-start",
    "initial-start",
  ])
    await expect(page.locator("#op-" + name)).toBeDisabled();
  await expect(page.locator("#failback-binding-view")).not.toContainText(
    "cutover-execution",
  );
});

test("expanded views fit mobile width", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await connect(page, context, "plan-workspace-console");
  for (const key of [
    "overview",
    "analysis",
    "ai",
    "costs",
    "monitoring",
    "security",
  ]) {
    await page.locator(`[data-view="${key}"]`).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
    ).toBe(false);
  }
});
