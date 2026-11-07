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

test("partial isolation moves the same node, severs its paths and preserves unrelated work", async ({
  page,
  context,
  baseURL,
}) => {
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await connect(page, context, baseURL);
  await demo(page);
  await map(page).getByLabel("로컬 격리 대상").selectOption("api");
  const api = map(page).locator('[data-resource="api"]');
  const database = map(page).locator('[data-resource="database"]');
  const initial = await api.getAttribute("transform");
  const databasePosition = await database.getAttribute("transform");
  await map(page)
    .getByRole("button", { name: "보안 이상 주입", exact: true })
    .click();
  await expect(database).toHaveClass(/possible-impact/);
  await api.evaluate((n) => n.setAttribute("data-test-retained", "same-node"));
  await next(page, 3);
  await expect(api).toHaveAttribute("data-placement", "source");
  await next(page);
  await expect(api).toHaveAttribute("data-placement", "quarantine");
  await expect(api).toHaveAttribute("data-test-retained", "same-node");
  await expect(map(page).locator("svg[data-moving=true]")).toBeVisible();
  await expect(map(page).locator("svg[data-moving=true]")).toHaveCount(0);
  expect(await api.getAttribute("transform")).not.toBe(initial);
  expect(await database.getAttribute("transform")).toBe(databasePosition);
  await expect(map(page).locator('[data-resource="worker"]')).toHaveAttribute(
    "data-placement",
    "source",
  );
  await expect(map(page).locator('[data-link="worker-db"]')).toHaveClass(
    /observed/,
  );
  const blocked = map(page).locator('[data-link="api-db"]');
  await expect(blocked.locator(".response-cut")).toBeVisible();
  await expect(blocked.locator(".response-path-after")).toBeVisible();
  await expect(blocked.locator(".response-packet")).not.toBeVisible();
  await expect(map(page).locator(".response-map-summary")).toContainText(
    "격리 확인 1",
  );
  await next(page, 3);
  await expect(map(page).locator('[data-link="edge-green"]')).toHaveClass(
    /observed/,
  );
  await expect(
    map(page).locator('[data-link="green-existing-worker"]'),
  ).toHaveClass(/observed/);
  await expect(api).toHaveAttribute("data-placement", "quarantine");
  await api.click();
  await expect(map(page).locator(".response-inspector")).toContainText(
    "연결 상태",
  );
  expect(pageErrors).toEqual([]);
});

test("expiry keeps historical placement without implying release, and restart resets the storyboard", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  await demo(page);
  await next(page, 5);
  const api = map(page).locator('[data-resource="api"]');
  await expect(map(page).locator("svg[data-moving=true]")).toHaveCount(0);
  const position = await api.getAttribute("transform");
  await map(page).getByRole("button", { name: "근거 만료 보기" }).click();
  await expect(api).toHaveAttribute("data-placement", "historical");
  await expect(api).not.toHaveClass(/security-isolated/);
  await expect(map(page).locator("svg[data-moving=true]")).toHaveCount(0);
  expect(await api.getAttribute("transform")).toBe(position);
  await expect(map(page).locator(".response-map-summary")).toContainText(
    "격리 확인 0",
  );
  await map(page)
    .getByRole("button", { name: "처음부터", exact: true })
    .click();
  await expect(api).toHaveAttribute("data-placement", "source");
});

test("expanded response map retains the Sova shell and closes with Escape", async ({
  page,
  context,
  baseURL,
}) => {
  await connect(page, context, baseURL);
  await demo(page);
  await map(page)
    .getByRole("button", { name: "지도 확대", exact: true })
    .click();
  await expect(map(page)).toHaveClass(/response-expanded/);
  await expect(page.locator(".sidebar")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(map(page)).not.toHaveClass(/response-expanded/);
});

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
