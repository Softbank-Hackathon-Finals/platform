import { test, expect } from "@playwright/test";
import { createHash } from "node:crypto";

async function connect(page, context, plan = "plan-demo") {
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
}

// Browser contract fixtures only: real T10 native-service/permission tests are in test_delivery_native.py.
async function routes(page) {
  const state = {
    subject: "requester",
    createCount: 0,
    prCount: 0,
    loseCreate: false,
    losePr: false,
    doc: null,
    pull: null,
    blocked: false,
    sync: "SYNC_RETRYING",
  };
  await page.route("**/v1/plans/plan-demo/delivery", async (route) => {
    const snapshot = await (
        await page.request.get("/v1/plans/plan-demo")
      ).json(),
      now = Date.now();
    await route.fulfill({
      json: {
        kind: "console-delivery.v1",
        plan_id: snapshot.plan_id,
        plan_digest: snapshot.plan_digest,
        plan_version: snapshot.version,
        subject_id: state.subject,
        queried_at: new Date(now).toISOString(),
        expires_at: new Date(now + 15000).toISOString(),
        authorizes_execution: false,
        iac: {
          configured: true,
          actions: {
            apply_infra: !state.blocked,
            approve_infra: !state.blocked,
          },
          modules: [
            {
              environment_id: snapshot.target_environment_id,
              environment: "target-dev",
              module: "target-object-exposure",
              params: { state: "closed", bucket: "demo-chat-attachments" },
            },
          ],
        },
        customers: [
          {
            environment_id: snapshot.target_environment_id,
            environment: "target-dev",
            expected_revision: "b".repeat(40),
            status: state.sync,
            provenance: "synthetic",
            observed_at: new Date(now).toISOString(),
            valid_until: new Date(now + 300000).toISOString(),
            cluster_identity_matches: true,
            authorizes_cutover: false,
            migration_verified: false,
            control_equivalence_verified: false,
            reasons: ["OPERATION_RETRYING"],
            retry_count: 2,
          },
        ],
      },
    });
  });
  await page.route("**/v1/plans/plan-demo/iac/plans", async (route) => {
    const body = route.request().postDataJSON();
    state.createCount++;
    const id =
      "iac-" +
      createHash("sha256")
        .update(`plan-demo\0${route.request().headers()["idempotency-key"]}`)
        .digest("hex")
        .slice(0, 24);
    state.doc = {
      iac_plan_id: id,
      environment: body.environment,
      module: body.module,
      base_sha: "b".repeat(40),
      plan_digest: "c".repeat(64),
      accepted: true,
      refusals: [],
      effects: [{ anonymous_access: { before: ["Read"], after: [] } }],
      changes: 1,
      requested_by: state.subject,
      binding: {
        infra_plan_id: id,
        plan_digest: "c".repeat(64),
        requested_by: state.subject,
      },
    };
    if (state.loseCreate) {
      state.loseCreate = false;
      await route.abort();
    } else await route.fulfill({ status: 201, json: state.doc });
  });
  await page.route("**/v1/plans/plan-demo/iac/plans/*", async (route) => {
    await route.fulfill({ json: { ...state.doc, pull_request: state.pull } });
  });
  await page.route("**/v1/plans/plan-demo/infra-approvals/*", async (route) => {
    await route.fulfill({
      json: {
        approval_id: "approval-demo",
        approval_kind: "infra",
        actor_id: "other-human",
        binding: state.doc.binding,
        approved_at: new Date(Date.now() - 1000).toISOString(),
        expires_at: new Date(Date.now() + 300000).toISOString(),
        revoked: false,
        claimed_run_id: null,
      },
    });
  });
  await page.route("**/v1/plans/plan-demo/approvals", async (route) => {
    const body = route.request().postDataJSON();
    expect(body.approval_kind).toBe("infra");
    expect(body.binding).toEqual(state.doc.binding);
    await route.fulfill({
      json: {
        approval_id: "approval-demo",
        approval_kind: "infra",
        actor_id: state.subject,
        binding: body.binding,
        approved_at: new Date().toISOString(),
        expires_at: body.expires_at,
      },
    });
  });
  await page.route(
    "**/v1/plans/plan-demo/iac/plans/*/pull-request",
    async (route) => {
      state.prCount++;
      expect(route.request().postDataJSON()).toEqual({
        approval_id: "approval-demo",
      });
      state.pull = {
        iac_plan_id: state.doc.iac_plan_id,
        approval_id: "approval-demo",
        plan_digest: state.doc.plan_digest,
        number: 701,
        url: "https://github.com/Softbank-Hackathon-Finals/platform/pull/701",
        base_sha: state.doc.base_sha,
        head_sha: "d".repeat(40),
      };
      if (state.losePr) {
        state.losePr = false;
        await route.abort();
      } else await route.fulfill({ status: 201, json: state.pull });
    },
  );
  return state;
}
async function create(page) {
  await page.locator('[data-view="delivery"]').click();
  await expect(page.locator("#iac-capability")).toContainText("현재 사용자");
  await page.locator("#iac-confirm").check();
  page.once("dialog", (d) => d.accept());
  await page.locator("#iac-create").click();
}
test("original Sova shell shows the same security report on security page and workload map", async ({
  page,
  context,
}) => {
  await connect(page, context, "plan-initial-controls");
  await page.locator('[data-view="security"]').click();
  await expect(page.locator("#security-progress")).toContainText(
    "T19 비교 판정",
  );
  await expect(page.locator("#security-progress")).toContainText("BLOCK");
  await expect(page.locator("#security-progress")).toContainText("미확인");
  await page.locator('[data-view="topology"]').click();
  await expect(page.locator(".workload-test-progress")).toContainText("BLOCK");
  await expect(page.locator(".workload-test-progress")).toContainText(
    "격리를 뜻하지 않습니다",
  );
  await expect(
    page.getByRole("link", { name: "Sova 운영 콘솔 홈" }),
  ).toHaveCount(1);
});
test("lost IaC create response is recovered by deterministic ID, without a second POST or self-approval", async ({
  page,
  context,
}) => {
  const state = await routes(page);
  state.loseCreate = true;
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await connect(page, context);
  await create(page);
  await expect(page.locator("#iac-status")).toContainText("응답 미확인");
  await expect(page.locator("#iac-create")).toBeDisabled();
  await page.locator("#iac-recover").click();
  await expect(page.locator("#iac-plan-detail")).toContainText(
    state.doc.iac_plan_id,
  );
  expect(state.createCount).toBe(1);
  await page.locator("#iac-confirm").check();
  await expect(page.locator("#iac-approve")).toBeDisabled();
  await expect(page.locator("#iac-pr")).toBeDisabled();
  await expect(page.locator("#customer-sync-list")).toContainText(
    "동기화 재시도 중",
  );
  await expect(page.locator("#customer-sync-status")).toContainText(
    "별도로 확인",
  );
  expect(errors).toEqual([]);
});
test("existing other-human approval opens the exact PR and response loss reads the saved PR only", async ({
  page,
  context,
}) => {
  const state = await routes(page);
  state.losePr = true;
  await connect(page, context);
  await create(page);
  await expect(page.locator("#iac-plan-detail")).toContainText("계획 해시");
  await page.locator("#iac-approval-id").fill("approval-demo");
  await page.locator("#iac-approval-read").click();
  await expect(page.locator("#iac-approval-detail")).toContainText(
    "other-human",
  );
  await page.locator("#iac-confirm").check();
  page.once("dialog", (d) => d.accept());
  await page.locator("#iac-pr").click();
  await expect(page.locator("#iac-status")).toContainText("응답 미확인");
  await page.locator("#iac-recover").click();
  await expect(page.locator("#iac-pr-link")).toHaveAttribute(
    "href",
    "https://github.com/Softbank-Hackathon-Finals/platform/pull/701",
  );
  expect(state.prCount).toBe(1);
  expect(state.createCount).toBe(1);
  await expect(page.locator("#iac-status")).toContainText(
    "병합·고객 반영은 별도",
  );
});
test("unconfigured real host keeps IaC changes disabled and customer success absent", async ({
  page,
  context,
}) => {
  await connect(page, context);
  await page.locator('[data-view="delivery"]').click();
  await expect(page.locator("#iac-capability")).toContainText(
    "운영 연결을 기다립니다",
  );
  await expect(page.locator("#iac-create")).toBeDisabled();
  await expect(page.locator("#customer-sync-status")).toContainText(
    "수집 연결을 기다립니다",
  );
});

test("another human can approve the frozen binding; permission loss disables changes", async ({
  page,
  context,
}) => {
  const state = await routes(page);
  await connect(page, context);
  await create(page);
  await expect(page.locator("#iac-plan-detail")).toContainText("계획 해시");
  state.subject = "other-human";
  await page.locator("#delivery-refresh").click();
  await expect(page.locator("#iac-capability")).toContainText("other-human");
  await page.locator("#iac-confirm").check();
  page.once("dialog", (d) => d.accept());
  await page.locator("#iac-approve").click();
  await expect(page.locator("#iac-status")).toContainText(
    "승인을 저장했습니다",
  );
  await expect(page.locator("#iac-pr")).toBeDisabled(); // POST receipt is not fresh approval readback
  state.blocked = true;
  await page.locator("#delivery-refresh").click();
  await expect(page.locator("#iac-capability")).toContainText("승인 권한 없음");
  await page.locator("#iac-confirm").check();
  await expect(page.locator("#iac-create")).toBeDisabled();
  await expect(page.locator("#iac-pr")).toBeDisabled();
});

test("Sova cards retain the existing navigation and fit desktop and mobile", async ({
  page,
  context,
}) => {
  await routes(page);
  await connect(page, context);
  await create(page);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(
      page.getByRole("heading", { name: "IaC 변경 승인", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.evaluate(() => window.scrollTo(0, 0));
    if (process.env.SOVA_UI_SCREENSHOTS)
      await page.screenshot({
        path: `${process.env.SOVA_UI_SCREENSHOTS}/iac-${width}.png`,
        fullPage: true,
      });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('[data-view="security"]').click();
  if (process.env.SOVA_UI_SCREENSHOTS)
    await page.screenshot({
      path: `${process.env.SOVA_UI_SCREENSHOTS}/security-1440.png`,
      fullPage: true,
    });
});
