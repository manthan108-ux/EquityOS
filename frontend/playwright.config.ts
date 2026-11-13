import { defineConfig } from "@playwright/test";

// Browser tests run against the real Next.js app with a MOCKED backend (see e2e/mockApi.ts), so
// they are deterministic and need no database. They check what the dashboard shows and does.
const PORT = process.env.E2E_PORT ?? "3100"; // set E2E_PORT=3000 to reuse a dev server already running

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: `http://localhost:${PORT}`, trace: "retain-on-failure" },
  webServer: {
    command: `npx next dev -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEXT_PUBLIC_API_URL: "http://localhost:8000" },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
