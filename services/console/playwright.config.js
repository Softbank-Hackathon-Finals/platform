import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "tests/browser",
  workers: 1,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:18121", headless: true },
  webServer: {
    command: "uv run python tests/browser_server.py",
    url: "http://127.0.0.1:18121/console/",
    reuseExistingServer: false,
    timeout: 30000,
  },
  reporter: "list",
});
