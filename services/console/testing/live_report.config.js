import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";
if (
  !process.env.BRAVO_TEST_GATE_REPORT ||
  !process.env.BRAVO_TEST_GATE_SHA256 ||
  !process.env.BRAVO_TEST_PLAN_DIGEST
)
  throw new Error("Explicit local T19 report and fingerprint required");
export default defineConfig({
  testDir: ".",
  testMatch: "live_report.spec.js",
  workers: 1,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:18122", headless: true },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
  webServer: {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    command: "uv run python testing/live_report_server.py",
    url: "http://127.0.0.1:18122/console/",
    reuseExistingServer: false,
    timeout: 30000,
  },
  reporter: "list",
});
