import { test, expect } from "@playwright/test";

async function approved(page, context, plan) {
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
  await page.locator("#confirm").check();
  await page.locator("#approve").click();
  await expect(page.locator("#action-status")).toContainText("승인 기록 완료");
}

async function execute(page) {
  await page.locator("#confirm").check();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#execute").click();
}

async function counters(page, plan) {
  return (await page.request.get(`/v1/plans/${plan}/fixture-stats`)).json();
}

test("cutover approval lookup cannot create a missing approval", async ({
  page,
  context,
}) => {
  const plan = "plan-approve-loss",
    posts = [];
  let blocked = true;
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
  await page.route(`**/v1/plans/${plan}/approvals`, async (route) => {
    posts.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    if (blocked) await route.abort("failed");
    else await route.continue();
  });
  await page.locator("#confirm").check();
  await page.locator("#approve").click();
  await expect(page.locator("#action-status")).toContainText(
    "응답을 확인하지 못했습니다",
  );
  await page.locator("#retry").click();
  await expect(page.locator("#action-status")).toContainText(
    "이번 요청의 접수 여부는 미확정",
  );
  expect(posts).toHaveLength(1);
  const replay = await (
    await page.request.get(`/v1/plans/${plan}/events?follow=false`)
  ).text();
  expect(replay).not.toContain('"event_type":"approval.decided"');
  await expect(page.locator("#execute")).toBeDisabled();
  blocked = false;
  await page.locator("#confirm").check();
  await page.locator("#approve").click();
  await expect(page.locator("#action-status")).toContainText("승인 기록 완료");
  expect(posts).toHaveLength(2);
  expect(posts[0]).toEqual(posts[1]);
  expect(await counters(page, plan)).toEqual({ submitted: 0, reads: 0 });
});

test("lost response after server receipt recovers through lookup only", async ({
  page,
  context,
}) => {
  const plan = "plan-after";
  await approved(page, context, plan);
  let submits = 0;
  await page.route(`**/v1/plans/${plan}/cutover`, async (route) => {
    submits++;
    await route.fetch();
    await route.abort("failed");
  });
  await execute(page);
  await expect(page.locator("#action-status")).toContainText(
    "응답을 확인하지 못했습니다",
  );
  expect(await counters(page, plan)).toEqual({ submitted: 1, reads: 0 });
  await page.locator("#retry").click();
  await expect(page.locator("#action-status")).toContainText(
    "실행 결과 미확인",
  );
  expect(await counters(page, plan)).toEqual({ submitted: 1, reads: 1 });
  expect(submits).toBe(1);
  await expect(page.locator("#execute")).toBeDisabled();
});

test("pre-server failure plus event gap cannot submit through result check", async ({
  page,
  context,
}) => {
  const plan = "plan-before";
  await approved(page, context, plan);
  const requests = [];
  let blocked = true;
  await page.route(`**/v1/plans/${plan}/cutover`, async (route) => {
    requests.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    if (blocked)
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"detail":{"code":"test_pre_server_loss"}}',
      });
    else await route.continue();
  });
  await execute(page);
  await expect(page.locator("#retry")).toBeVisible();
  expect(await counters(page, plan)).toEqual({ submitted: 0, reads: 0 });
  await page.route(`**/v1/plans/${plan}/events**`, (route) => {
    const event = {
      schema_version: "1.0",
      event_id: `${plan}:999`,
      sequence: 999,
      plan_id: plan,
      run_id: "gap-only",
      occurred_at: new Date().toISOString(),
      event_type: "state.changed",
      data: { from: "SHIFT", to: "CUTOVER" },
    };
    return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: `id: ${plan}:999\nevent: state.changed\ndata: ${JSON.stringify(event)}\n\n`,
    });
  });
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(page.locator("#connection")).toHaveText("연결 끊김 · 실행 잠금");
  await page.locator("#retry").click();
  await expect(page.locator("#notice")).toContainText(
    "이력을 완전히 확인할 수 없어",
  );
  expect(await counters(page, plan)).toEqual({ submitted: 0, reads: 0 });
  expect(requests).toHaveLength(1);
  await expect(page.locator("#execute")).toBeDisabled();
  // Pending requests no longer prevent reconnecting this same plan.
  await page.unroute(`**/v1/plans/${plan}/events**`);
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await page.locator("#retry").click();
  await expect(page.locator("#action-status")).toContainText(
    "결과 확인만으로 실행하지 않습니다",
  );
  expect(await counters(page, plan)).toEqual({ submitted: 0, reads: 0 });
  await expect(page.locator("#execute")).toBeDisabled();
  blocked = false;
  await execute(page);
  await expect(page.locator("#state")).toHaveText("안전 보류");
  expect(await counters(page, plan)).toEqual({ submitted: 1, reads: 0 });
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
});
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

test("a valid three-test report cannot appear under a twenty-test plan", async ({
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
  // Deliberately split the browser plan from the report. The real server-side
  // mismatch is covered by HTTP tests; this checks the independent UI boundary.
  await page.route("**/v1/plans/plan-demo", async (route) => {
    const response = await route.fetch();
    const plan = await response.json();
    await route.fulfill({
      response,
      json: { ...plan, profile_id: "full", profile_version: "1" },
    });
  });
  let reportSeen = false;
  page.on("response", (response) => {
    if (response.url().endsWith("/controls") && response.status() === 200)
      reportSeen = true;
  });
  const writes = [];
  page.on("request", (request) => {
    if (request.method() !== "GET") writes.push(request.url());
  });
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-demo");
  await page.locator("#connect").click();
  await expect.poll(() => reportSeen).toBe(true);
  await expect(page.locator("#workload")).toHaveText("demo-chat");
  await expect(page.locator("#control-status")).not.toContainText("PASS");
  await expect(page.locator("#control-rows tr")).toHaveCount(0);
  expect(writes).toEqual([]);
});

test("on-prem source selection sends one role and cannot promote a target alias", async ({
  page,
  context,
}) => {
  await context.addCookies([
    {
      name: "fixture-session",
      value: "test-only",
      url: "http://127.0.0.1:18121",
    },
  ]);
  await page.goto("/console/");
  await page.locator("#registration-panel summary").click();
  const form = page.locator("#registration-environment-form");
  await form.locator('[name="name"]').fill("On-prem source");
  await form
    .locator('[name="kind"]')
    .selectOption({ label: "온프렘 · 출발지" });
  await form.locator('[name="connection_ref"]').fill("target-reader");
  const response = page.waitForResponse("**/v1/environments");
  await form.locator("button").click();
  const result = await response;
  expect(result.request().postDataJSON()).toEqual({
    name: "On-prem source",
    kind: "onprem",
    roles: ["source"],
    connection_ref: "target-reader",
  });
  expect(result.status()).toBe(409);
  await expect(page.locator("#registration-receipts li")).toHaveCount(0);
});

test("registration recovers lost response and connects the real T10 COLLECT plan", async ({
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
  await page.locator("#registration-panel summary").click();
  const environment = page.locator("#registration-environment-form");
  const requests = [];
  let drop = true;
  await page.route("**/v1/environments", async (route) => {
    requests.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    const response = await route.fetch();
    if (drop) {
      drop = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await environment.locator('[name="name"]').fill("Source");
  await environment.locator('[name="connection_ref"]').fill("source-reader");
  await environment.locator("button").click();
  await expect(page.locator("#registration-status")).toContainText(
    "접수 결과 미확인",
  );
  await expect(environment.locator("button")).toBeDisabled();
  await page.locator("#registration-retry").click();
  await expect(page.locator("#registration-source")).toHaveValue("env-source");
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  await expect(page.locator("#registration-receipts")).toContainText(
    "write_scope_unknown",
  );
  await environment.locator('[name="name"]').fill("Target");
  await environment.locator('[name="kind"]').selectOption("onprem");
  await environment.locator('[name="connection_ref"]').fill("target-reader");
  await environment.locator("button").click();
  await expect(page.locator("#registration-target")).toHaveValue("env-target");
  const workload = page.locator("#registration-workload-form");
  for (const [name, value] of Object.entries({
    name: "chat",
    repo_url: "https://example.invalid/chat",
    app_path: "app",
    iac_path: "compose.yaml",
    namespace: "app-source",
  }))
    await workload.locator(`[name="${name}"]`).fill(value);
  await workload.locator("button").click();
  await expect(page.locator("#registration-workload")).not.toHaveValue("");
  await expect(page.locator("#registration-profile")).toHaveValue("");
  await expect(page.locator("#registration-profile-version")).toHaveValue("");
  // Native required validation must prevent a request with an implicit profile.
  let plans = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/v1/plans"
    )
      plans++;
  });
  await page.locator("#registration-plan-form button").click();
  await expect(page.locator("#registration-receipts li")).toHaveCount(3);
  expect(plans).toBe(0);
  await page.locator("#registration-profile").fill("fixture-profile");
  await page.locator("#registration-profile-version").fill("1.0");
  await page.locator('[name="recheck_profile_id"]').fill("fixture-profile");
  await page.locator('[name="recheck_profile_version"]').fill("1.0");
  await page.locator("#registration-plan-form button").click();
  await expect(page.locator("#state")).toHaveText("조사");
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
  await expect(page.locator("#registration-receipts li")).toHaveCount(4);
  await expect(page.locator("#registration-receipts li").last()).toContainText(
    "fixture-profile / 1.0",
  );
  expect(plans).toBe(1);
  const registeredId = await page.locator("#plan").inputValue();
  const registered = await (
    await page.request.get(`/v1/plans/${registeredId}`)
  ).json();
  expect(registered.recheck_profile_id).toBe("fixture-profile");
  expect(registered.recheck_profile_version).toBe("1.0");
  await context.clearCookies();
  await expect(page.locator("#notice")).toHaveText(
    "접근 권한 또는 로그인 상태를 확인해 주세요.",
    {
      timeout: 15000,
    },
  );
  await expect(page.locator("#registration-receipts li")).toHaveCount(0);
  await expect(page.locator("#registration-source")).toHaveValue("");
});

test("unauthenticated registration obtains no environment receipt", async ({
  page,
}) => {
  await page.goto("/console/");
  await page.locator("#registration-panel summary").click();
  const form = page.locator("#registration-environment-form");
  await form.locator('[name="name"]').fill("Not authorized");
  await form.locator('[name="connection_ref"]').fill("source-reader");
  await form.locator("button").click();
  await expect(page.locator("#registration-status")).toContainText(
    "로그인 또는 권한",
  );
  await expect(page.locator("#registration-receipts li")).toHaveCount(0);
  await expect(page.locator("#execute")).toBeDisabled();
});
