import { defineConfig } from "@playwright/test";

// LIVE journey: the real frontend and the real backend (the Docker Compose stack), no mocks.
//   E2E_LIVE_URL=http://localhost:3001 E2E_API_KEY=<admin key> npx playwright test -c playwright.live.config.ts
export default defineConfig({
  testDir: "./e2e-live",
  timeout: 180_000,
  workers: 1,
  reporter: "list",
  use: { baseURL: process.env.E2E_LIVE_URL ?? "http://localhost:3000", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
