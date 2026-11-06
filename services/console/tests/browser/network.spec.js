import { test, expect } from "@playwright/test";

test.use({ locale: "ko-KR" });

async function openAtlas(page, context) {
  await context.addCookies([
    {
      name: "fixture-session",
      value: "test-only",
      url: "http://127.0.0.1:18121",
      sameSite: "Strict",
    },
  ]);
  await page.goto("/console/network/?plan=plan-demo");
  await expect(page.locator("#sova-network-scope")).toContainText(
    "공유 환경 네트워크",
  );
  await expect(page.locator("#live-resources tr")).toHaveCount(9);
}

test("atlas uses authenticated same-origin WebSocket and escapes observed resource names", async ({
  page,
  context,
}) => {
  const errors = [],
    posts = [],
    connections = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => {
    if (r.method() === "POST") posts.push(r.url());
  });
  page.on("websocket", (ws) => connections.push(ws.url()));
  await openAtlas(page, context);
  await expect(page).toHaveTitle(/Sova Network Atlas/);
  await expect(page.locator("#live-banner")).toContainText("WS 연결됨");
  await expect(page.locator("#live-resources")).toContainText("<img src=x");
  expect(await page.locator("#live-resources img").count()).toBe(0);
  expect(await page.evaluate(() => window.__networkAttack)).toBeUndefined();
  expect(connections).toEqual([
    "ws://127.0.0.1:18121/v1/plans/plan-demo/network-stream",
  ]);
  expect(posts).toEqual([]);
  expect(errors).toEqual([]);
});

test("original diagrams retain dimensions, zoom, protocol explorer and three locales", async ({
  page,
  context,
}) => {
  await openAtlas(page, context);
  await page.locator('[data-view="architecture"]').click();
  await expect(page.locator("#paper-title")).toContainText("3800 × 2980");
  await expect
    .poll(() =>
      page.locator("#paper-image").evaluate((img) => img.naturalWidth),
    )
    .toBe(3800);
  const old = await page.locator("#paper-scale").textContent();
  await page.locator("#paper-in").click();
  await expect(page.locator("#paper-scale")).not.toHaveText(old);
  await page.locator('[data-sheet="1"]').click();
  await expect(page.locator("#paper-title")).toContainText("3200 × 4408");
  await page.locator('[data-view="explorer"]').click();
  await expect(page.locator("#explorer-view")).toBeVisible();
  for (const locale of ["en", "ja", "ko"]) {
    await page.locator("#locale-toggle").click();
    await page.locator(`button[data-locale="${locale}"]`).click();
    await expect(page.locator("html")).toHaveAttribute("lang", locale);
  }
});

test("missing login never exposes actual network resources", async ({
  page,
}) => {
  await page.goto("/console/network/?plan=plan-demo");
  await expect(page.locator("#sova-network-scope")).toContainText("권한");
  expect(await page.locator("#live-resources tr[class]").count()).toBe(0);
});

test("network tabs and resource views fit a narrow screen", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openAtlas(page, context);
  for (const name of ["live", "l3", "permissions", "evidence"]) {
    await page.locator(`.main-tabs [data-view="${name}"]`).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
});
