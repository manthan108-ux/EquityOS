import { expect, test } from "@playwright/test";

const KEY = process.env.E2E_API_KEY ?? "";
const SYMBOL = process.env.E2E_SYMBOL ?? "TCS";

test.skip(!KEY, "set E2E_API_KEY to an admin research key");

test("without a key the app says access is needed and the API refuses", async ({ page, request }) => {
  await page.goto("/");
  await page.locator("#company-search").fill(SYMBOL);
  await page.getByRole("link", { name: "Open research workspace" }).click();
  await expect(page.getByRole("heading", { name: "Research access key needed" })).toBeVisible();
  const api = process.env.E2E_API_URL ?? "http://localhost:8000";
  expect((await request.get(`${api}/api/pipeline/overview`)).status()).toBe(401);
});

test("company selection to a generated, viewable and downloadable report on the real stack", async ({ page }) => {
  await page.addInitScript((k) => sessionStorage.setItem("equityos.apiKey", k), KEY);
  await page.goto("/");
  await page.locator("#company-search").fill(SYMBOL);
  const card = page.getByRole("region", { name: "Confirm company" });
  await expect(card).toBeVisible({ timeout: 20_000 });
  await card.getByRole("link", { name: "Open research workspace" }).click();
  await expect(page.getByRole("link", { name: "Access key set" })).toBeVisible();

  // Let the session load: a company with research already done shows its pipeline, a new one offers a start button.
  await page.waitForLoadState("networkidle");
  const stages = page.getByRole("list", { name: "Research stages" });
  const startBtn = page.getByRole("button", { name: "Start research" }).first();
  await expect(stages.or(startBtn)).toBeVisible({ timeout: 30_000 });
  if (!(await stages.isVisible())) await startBtn.click();
  await expect(stages.getByRole("listitem").first()).toBeVisible({ timeout: 60_000 });

  await page.getByRole("tab", { name: "Reports" }).click();
  await page.getByRole("button", { name: "Generate available-evidence report" }).click();
  await expect(page.getByRole("heading", { name: /Report version \d+/ })).toBeVisible({ timeout: 150_000 });
  await expect(page.getByRole("navigation", { name: "Report sections" })).toBeVisible();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download PDF" }).last().click(),
  ]);
  const path = await download.path();
  const fs = await import("node:fs");
  const bytes = fs.readFileSync(path);
  expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
  expect(bytes.length).toBeGreaterThan(50_000);
});

test("pipeline monitoring shows real runs and their processing state", async ({ page }) => {
  await page.addInitScript((k) => sessionStorage.setItem("equityos.apiKey", k), KEY);
  await page.goto("/pipeline");
  await page.waitForLoadState("networkidle");
  await expect(page.getByText(/completed/i).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/Run #?\d+|run \d+/i).first()).toBeVisible();
});

test("a wrong key is refused by the backend and the pipeline page says so", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("equityos.apiKey", "not-a-real-key"));
  await page.goto("/pipeline");
  await page.waitForLoadState("networkidle");
  await expect(page.getByText(/not valid|rejected|access key/i).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/completed/i)).toHaveCount(0);
});
