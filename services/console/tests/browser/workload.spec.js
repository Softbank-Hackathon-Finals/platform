import { test, expect } from "@playwright/test";

async function connect(page, context, baseURL) {
  await context.addCookies([
    {
      name: "fixture-session",
      value: "test-only",
      url: baseURL,
      sameSite: "Strict",
    },
  ]);
  await page.goto("/console/");
  await page.locator("#plan").fill("plan-demo");
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await page.locator('[data-view="topology"]').click();
}
const map = (page) => page.locator("#response-workspace");
async function demo(page) {
  await map(page)
    .getByRole("button", { name: "로컬 시나리오", exact: true })
    .click();
  await expect(map(page).locator(".response-banner")).toContainText(
    "로컬 합성",
  );
}
async function next(page, times = 1) {
  for (let i = 0; i < times; i++)
    await map(page)
      .getByRole("button", { name: "다음 단계", exact: true })
      .click();
}

test("original Sova shell and actual T10 context coexist with separate response evidence", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  await expect(page.locator(".brand")).toContainText("Sova");
  await expect(map(page).locator(".response-banner")).toContainText(
    "서버 합성 시험",
  );
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-isolated/,
  );
  await expect(map(page).locator(".response-context")).toContainText(
    /T10 (현재 단계|계획이 보류 상태)/,
  );
  await expect(map(page).locator(".response-gate-record")).toContainText(
    "격리 완료를 뜻하지 않습니다",
  );
  await map(page).locator('[data-resource="api"]').click();
  await expect(map(page).locator(".response-inspector")).toContainText(
    "synthetic-api",
  );
  await expect(map(page).locator(".response-inspector")).toContainText(
    "합성 시나리오",
  );
});
test("request, confirmed isolation, replacement and recovery have distinct visible states and no POST", async ({
  page,
  context,
  baseURL,
}) => {
  const changes = [];
  page.on("request", (r) => {
    if (r.method() !== "GET") changes.push([r.method(), r.url()]);
  });
  await connect(page, context, baseURL);
  await demo(page);
  await next(page, 1);
  await map(page).locator('[data-resource="api"]').click();
  await expect(map(page).locator('[data-resource="database"]')).toHaveClass(
    /possible-impact/,
  );
  await next(page, 3);
  await expect(map(page).locator('[data-link="api-db"]')).toHaveClass(
    /requested/,
  );
  await expect(map(page).locator('[data-resource="api"]')).not.toHaveClass(
    /security-isolated/,
  );
  await next(page);
  await expect(map(page).locator('[data-link="api-db"]')).toHaveClass(
    /blocked/,
  );
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-isolated/,
  );
  await next(page, 4);
  await expect(map(page).locator('[data-link="edge-green"]')).toHaveClass(
    /observed/,
  );
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-isolated/,
  );
  await expect(map(page).locator(".response-phase").last()).toContainText(
    "확인됨",
  );
  expect(changes).toEqual([]);
});
test("replacement failure does not draw a successful route", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  await demo(page);
  await map(page)
    .getByLabel("로컬 시나리오 종류")
    .selectOption("verification_failure");
  await next(page, 9);
  await expect(map(page).locator('[data-resource="green"]')).toHaveClass(
    /health-unavailable/,
  );
  await expect(map(page).locator('[data-link="edge-green"]')).toHaveClass(
    /unknown/,
  );
  await expect(map(page).locator(".response-phase.failed")).toContainText(
    "독립 검증",
  );
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-isolated/,
  );
});
test("expired evidence visibly withdraws isolation and response success", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  await demo(page);
  await next(page, 5);
  await map(page).getByRole("button", { name: "근거 만료 보기" }).click();
  await expect(map(page).locator(".response-banner")).toContainText(
    "근거 만료",
  );
  await expect(
    map(page).locator(".response-node.security-isolated"),
  ).toHaveCount(0);
  await expect(map(page).locator(".response-link.blocked")).toHaveCount(0);
  await expect(map(page).locator(".response-phase.verified")).toHaveCount(0);
});
test("permission revocation erases graph identities and stale read cannot restore them", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  await expect(map(page).locator('[data-resource="api"]')).toBeVisible();
  await context.clearCookies();
  await expect(page.locator("#notice")).toContainText("접근 권한", {
    timeout: 12000,
  });
  await expect(map(page).locator(".response-node")).toHaveCount(0);
  await expect(map(page).locator(".response-inspector")).not.toContainText(
    "synthetic-api",
  );
});
test("malformed and unavailable source never becomes a healthy empty map", async ({
  page,
  context,
  baseURL,
}) => {
  await page.route("**/v1/plans/*/workload", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ kind: "console-workload.v1", nodes: [] }),
    }),
  );
  await connect(page, context, baseURL);
  await expect(map(page).locator(".response-banner")).toContainText("미연결");
  await expect(map(page).locator(".response-node")).toHaveCount(0);
  await expect(map(page).locator(".response-phase.verified")).toHaveCount(0);
});
test("keyboard selection and reduced motion keep response map usable", async ({
  page,
  context,
  baseURL,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await connect(page, context, baseURL);
  await demo(page);
  const node = map(page).locator('[data-resource="api"]');
  await node.focus();
  await page.keyboard.press("Enter");
  await expect(node).toHaveAttribute("aria-pressed", "true");
  await expect(map(page).locator(".response-inspector h4")).toHaveText(
    "업무 API",
  );
  const animation = await map(page)
    .locator(".response-link.observed path")
    .first()
    .evaluate((n) => getComputedStyle(n).animationName);
  expect(animation).toBe("none");
});

test("live refresh preserves keyboard focus and selection", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  const node = map(page).locator('[data-resource="api"]');
  await node.focus();
  await page.keyboard.press("Enter");
  await expect(node).toBeFocused();
  await page.waitForResponse(
    (r) => r.url().endsWith("/workload") && r.status() === 200,
  );
  await expect(node).toBeFocused();
  await expect(node).toHaveAttribute("aria-pressed", "true");
});

test("a conflicting producer stream stays unknown until the user reconnects", async ({
  page,
  context,
  baseURL,
}) => {
  let reads = 0;
  await page.route("**/v1/plans/*/workload", async (route) => {
    const response = await route.fetch();
    const document = await response.json();
    reads++;
    if (reads === 2) document.stream_id = "conflicting-producer-stream";
    await route.fulfill({ response, json: document });
  });
  await connect(page, context, baseURL);
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-isolated/,
  );
  await expect.poll(() => reads, { timeout: 16000 }).toBeGreaterThanOrEqual(3);
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-unknown/,
  );
  await expect(map(page).locator(".response-phase.verified")).toHaveCount(0);
  await map(page).getByRole("button", { name: "관측 다시 연결" }).click();
  await expect(map(page).locator('[data-resource="api"]')).toHaveClass(
    /security-isolated/,
  );
});
