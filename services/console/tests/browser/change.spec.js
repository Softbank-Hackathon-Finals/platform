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
  await expect(page.locator("#state")).toHaveText("리허설");
  await expect(page.locator("#binding")).toContainText("change-run");
  await expect(page.locator("#approve-change")).toBeDisabled();
  await page.locator("#confirm").check();
  await expect(page.locator("#approve-change")).toBeEnabled();
  await expect(page.locator("#approve")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
}

test("S2 bound approval records without deployment or cutover", async ({
  page,
  context,
}) => {
  const plan = "plan-change";
  await connect(page, context, plan);
  await page.locator("#approve-change").click();
  await expect(page.locator("#action-status")).toContainText(
    "코드 변경 승인 기록 완료",
  );
  await expect(page.locator("#action-status")).toContainText(
    "배포·전환을 실행한 것은 아닙니다",
  );
  await expect(page.locator("#execute")).toBeDisabled();
  await expect(page.locator("#state")).toHaveText("리허설");
  expect((await page.request.get(`/v1/plans/${plan}/fixture-stats`)).ok()).toBe(
    true,
  );
  expect(
    await (await page.request.get(`/v1/plans/${plan}/fixture-stats`)).json(),
  ).toEqual({ submitted: 0, reads: 0 });
  await page.reload();
  await page.locator("#plan").fill(plan);
  await page.locator("#connect").click();
  await expect(page.locator("#connection")).toHaveText("서버 이벤트 연결됨");
  await expect(page.locator("#approve-change")).toBeDisabled();
  await expect(page.locator("#execute")).toBeDisabled();
});

test("S2 lost approval response repeats only same body and key after explicit lookup", async ({
  page,
  context,
}) => {
  const plan = "plan-change-loss",
    requests = [];
  let lost = true;
  await connect(page, context, plan);
  await page.route(`**/v1/plans/${plan}/approvals`, async (route) => {
    requests.push({
      body: route.request().postData(),
      key: route.request().headers()["idempotency-key"],
    });
    if (lost) {
      await route.fetch();
      lost = false;
      await route.abort("failed");
    } else await route.continue();
  });
  await page.locator("#approve-change").click();
  await expect(page.locator("#action-status")).toContainText(
    "응답을 확인하지 못했습니다",
  );
  expect(requests).toHaveLength(1);
  await page.locator("#retry").click();
  await expect(page.locator("#action-status")).toContainText(
    "이번 요청의 접수 여부는 미확정",
  );
  expect(requests).toHaveLength(1);
  await page.locator("#confirm").check();
  await page.locator("#approve-change").click();
  await expect(page.locator("#action-status")).toContainText(
    "코드 변경 승인 기록 완료",
  );
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(JSON.parse(requests[0].body).run_id).toBe("change-run");
  await expect(page.locator("#execute")).toBeDisabled();
  expect(
    await (await page.request.get(`/v1/plans/${plan}/fixture-stats`)).json(),
  ).toEqual({ submitted: 0, reads: 0 });
});

for (const changed of [false, true]) {
  test(`S2 pre-server loss lookup writes nothing; changed version=${changed}`, async ({
    page,
    context,
  }) => {
    const plan = changed ? "plan-change-version" : "plan-change-pre",
      requests = [];
    let blocked = true;
    await connect(page, context, plan);
    await page.route(`**/v1/plans/${plan}/approvals`, async (route) => {
      requests.push({
        body: route.request().postData(),
        key: route.request().headers()["idempotency-key"],
      });
      if (blocked) await route.abort("failed");
      else await route.continue();
    });
    await page.locator("#approve-change").click();
    await expect(page.locator("#action-status")).toContainText(
      "응답을 확인하지 못했습니다",
    );
    const decisions = async () =>
      (
        await (
          await page.request.get(`/v1/plans/${plan}/events?follow=false`)
        ).text()
      ).split('"event_type":"approval.decided"').length - 1;
    expect(await decisions()).toBe(0);
    if (changed)
      await page.route(`**/v1/plans/${plan}`, async (route) => {
        const result = await route.fetch(),
          body = await result.json();
        await route.fulfill({
          response: result,
          json: { ...body, version: body.version + 1 },
        });
      });
    await page.locator("#retry").click();
    await expect(page.locator("#action-status")).toContainText(
      "이번 요청의 접수 여부는 미확정",
    );
    expect(requests).toHaveLength(1);
    expect(await decisions()).toBe(0);
    await expect(page.locator("#execute")).toBeDisabled();
    if (changed) await expect(page.locator("#approve-change")).toBeDisabled();
    else {
      blocked = false;
      await page.locator("#confirm").check();
      await page.locator("#approve-change").click();
      await expect(page.locator("#action-status")).toContainText(
        "코드 변경 승인 기록 완료",
      );
      expect(requests).toHaveLength(2);
      expect(requests[0]).toEqual(requests[1]);
      expect(await decisions()).toBe(1);
    }
  });
}
