import { test, expect } from "@playwright/test";
import { timeline } from "../failback-fixtures.js";
import { snap, plan } from "../fixtures.js";
for (const [status, label, state] of [
  ["edge_pending", "트래픽 복귀 확인 대기", "복귀"],
  ["rolled_back", "복구 시간 9000 ms", "복귀 완료"],
  ["aborted", "쓰기 전 복귀 중단", "관측"],
]) {
  test(`proposed T20 contract renders ${status} without any mutation`, async ({
    page,
    context,
  }) => {
    // Contract fixture only, not evidence of a real DB/Edge failback.
    await context.addCookies([
      {
        name: "fixture-session",
        value: "test-only",
        url: "http://127.0.0.1:18121",
        sameSite: "Strict",
      },
    ]);
    const rows = timeline(status),
      final = rows.at(-1).data.to ?? "FAILBACK";
    let revoked = false;
    const mutations = [];
    await page.route(`**/v1/plans/${plan}**`, async (route) => {
      const req = route.request(),
        path = new URL(req.url()).pathname;
      if (req.method() !== "GET") mutations.push(path);
      if (revoked)
        return route.fulfill({
          status: 403,
          contentType: "application/json",
          body: '{"detail":{"code":"forbidden"}}',
        });
      if (path.endsWith("/events"))
        return route.fulfill({
          contentType: "text/event-stream",
          body: rows
            .map(
              (e) =>
                `id: ${e.event_id}\nevent: ${e.event_type}\ndata: ${JSON.stringify(e)}\n\n`,
            )
            .join(""),
        });
      if (path === `/v1/plans/${plan}`)
        return route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            ...snap,
            state: final,
            version: 5,
            last_event_id: `${plan}:${rows.length}`,
          }),
        });
      return route.fulfill({
        status: 503,
        contentType: "application/json",
        body: '{"detail":{"code":"unavailable"}}',
      });
    });
    await page.goto("/console/");
    await page.locator("#plan").fill(plan);
    await page.locator("#connect").click();
    // The finite contract stream ends; historical records remain readable with execution locked.
    await expect(page.locator("#connection")).toHaveText(
      "연결 끊김 · 실행 잠금",
    );
    await expect(page.locator("#state")).toHaveText(state);
    await expect(page.locator("#failback-status")).toContainText(label);
    await expect(page.locator("#binding")).toContainText("복귀 (조회 전용)");
    await expect(page.locator("#approve")).toBeDisabled();
    await expect(page.locator("#execute")).toBeDisabled();
    await expect(page.locator("#failback-detail")).toContainText(
      "failback-one",
    );
    await page.locator("#connect").click();
    await expect(page.locator("#failback-status")).toContainText(label);
    revoked = true;
    await expect(page.locator("#notice")).toContainText("권한", {
      timeout: 12000,
    });
    await expect(page.locator("#failback-detail")).toBeEmpty();
    await expect(page.locator("#failback-status")).toHaveText(
      "복귀 검증 기록 없음",
    );
    expect(mutations).toEqual([]);
  });
}
