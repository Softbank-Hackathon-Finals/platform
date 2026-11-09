import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".",
  testMatch: "audit.spec.js",
  workers: 1,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:18124", headless: true },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
  webServer: {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    command: ".venv/bin/python testing/audit_server.py",
    url: "http://127.0.0.1:18124/console/",
    reuseExistingServer: false,
    timeout: 30000,
  },
  reporter: "list",
});
