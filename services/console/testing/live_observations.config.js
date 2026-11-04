import { defineConfig } from "@playwright/test";
const origin = new URL(process.env.BRAVO_BROWSER_ORIGIN);
if (
  origin.protocol !== "http:" ||
  origin.hostname !== "127.0.0.1" ||
  !process.env.BRAVO_BROWSER_COOKIE ||
  !process.env.BRAVO_BROWSER_OUTPUT
)
  throw new Error("Explicit owned loopback observation trial required");
export default defineConfig({
  testDir: ".",
  testMatch: "live_observations.spec.js",
  workers: 1,
  timeout: 25000,
  retries: 0,
  reporter: "list",
  outputDir: process.env.BRAVO_BROWSER_OUTPUT,
  use: { baseURL: origin.origin, headless: true, trace: "off", video: "off" },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
});
