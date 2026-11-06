import { test, expect } from "@playwright/test";
test.use({ locale: "ko-KR" });
async function openNetwork(page, context) {
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
  await page.locator('[data-view="network"]').click();
  await expect(page.locator("#network-connection")).toContainText(
    "네트워크 관측 수신",
  );
}
test("network features stay in the original Sova shell without a new page or POST", async ({
  page,
  context,
}) => {
  const errors = [],
    posts = [],
    popups = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => {
    if (r.method() === "POST") posts.push(r.url());
  });
  page.on("popup", (p) => popups.push(p));
  await openNetwork(page, context);
  await expect(page).toHaveURL("http://127.0.0.1:18121/console/");
  await expect(page).toHaveTitle("Sova · 이전 운영 콘솔");
  await expect(page.locator(".sidebar")).toHaveCount(1);
  await expect(page.locator(".topbar")).toHaveCount(1);
  await expect(page.locator(".brand strong")).toHaveText("Sova.");
  expect(await page.locator("iframe").count()).toBe(0);
  await page.locator('[data-network-tab="resources"]').click();
  await expect(
    page.locator("#network-pane-resources tr[data-network-row]"),
  ).toHaveCount(9);
  await expect(page.locator("#network-pane-resources")).toContainText(
    "<img src=x",
  );
  expect(await page.locator("#network-pane-resources img").count()).toBe(0);
  expect(await page.evaluate(() => window.__networkAttack)).toBeUndefined();
  await page.locator('[data-view="overview"]').click();
  await expect(page.locator("#map-source")).toBeVisible();
  await expect(page.locator("#connect-form")).toBeVisible();
  expect(posts).toEqual([]);
  expect(popups).toEqual([]);
  expect(errors).toEqual([]);
});
test("original diagrams, zoom, protocols and local language controls work inside Sova cards", async ({
  page,
  context,
}) => {
  await openNetwork(page, context);
  await page.locator('[data-network-tab="diagrams"]').click();
  await expect(page.locator("#network-image-caption")).toContainText(
    "3800 × 2980",
  );
  await expect
    .poll(() =>
      page
        .locator("#network-original-image")
        .evaluate((img) => img.naturalWidth),
    )
    .toBe(3800);
  const old = await page.locator("#network-image-scale").textContent();
  await page
    .locator("#network-pane-diagrams")
    .getByRole("button", { name: "＋", exact: true })
    .click();
  await expect(page.locator("#network-image-scale")).not.toHaveText(old);
  await page.locator("#network-sheet").selectOption("1");
  await expect(page.locator("#network-image-caption")).toContainText(
    "3200 × 4408",
  );
  await page.locator('[data-network-tab="protocols"]').click();
  await page.locator("#network-protocol-select").selectOption("human-target");
  await expect(page.locator("#network-pane-protocols")).toContainText("TLS");
  for (const language of ["en", "ja", "ko"]) {
    await page.locator("#network-language").selectOption(language);
    await expect(page.locator("#network-language")).toHaveValue(language);
    await expect(page.locator("html")).toHaveAttribute("lang", "ko");
    await expect(page.locator(".brand strong")).toHaveText("Sova.");
  }
});
test("a late network HTTP response cannot restore data after terminal WebSocket denial", async ({
  page,
  context,
}) => {
  await page.addInitScript(() => {
    const Native = window.WebSocket;
    window.WebSocket = class extends Native {
      constructor(...args) {
        super(...args);
        if (String(args[0]).endsWith("/network-stream"))
          window.__networkSocket = this;
      }
    };
  });
  let release;
  const hold = new Promise((resolve) => (release = resolve));
  await page.route("**/network-status", async (route) => {
    const response = await route.fetch();
    await hold;
    await route.fulfill({ response });
  });
  await openNetwork(page, context);
  await page.evaluate(() =>
    window.__networkSocket.close(4403, "fixture-denied"),
  );
  await expect(page.locator("#network-connection")).toContainText("조회 권한");
  release();
  await expect(
    page.locator("#network-pane-resources tr[data-network-row]"),
  ).toHaveCount(0);
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await expect(page.locator("#plan")).toHaveValue("plan-demo");
});
test("old atlas URLs return to the same Sova UI and missing login exposes no network rows", async ({
  page,
}) => {
  await page.goto("/console/network/?plan=plan-demo");
  await expect(page).toHaveURL(/\/console\/\?view=network&plan=plan-demo/);
  await expect(page).toHaveTitle("Sova · 이전 운영 콘솔");
  await expect(page.locator("#notice")).toContainText("접근 권한");
  expect(
    await page.locator("#network-pane-resources tr[data-network-row]").count(),
  ).toBe(0);
  await expect(page.locator(".sidebar")).toHaveCount(1);
});
test("all native network panels fit a mobile screen", async ({
  page,
  context,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openNetwork(page, context);
  for (const name of [
    "map",
    "resources",
    "transport",
    "routes",
    "health",
    "protocols",
    "permissions",
    "diagrams",
  ]) {
    await page.locator(`[data-network-tab="${name}"]`).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
  }
});
