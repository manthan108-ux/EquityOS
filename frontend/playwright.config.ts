import { defineConfig } from "@playwright/test";

// Browser tests run against the real Next.js app with a MOCKED backend (see e2e/mockApi.ts), so
// they are deterministic and need no database. They check what the dashboard shows and does.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: "http://localhost:3100", trace: "retain-on-failure" },
  webServer: {
    command: "npx next dev -p 3100",
    url: "http://localhost:3100",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { NEXT_PUBLIC_API_URL: "http://localhost:8000" },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
