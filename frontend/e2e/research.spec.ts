import { expect, test, type Page } from "@playwright/test";
import { readiness, session } from "../src/features/research/fixtures";
import { freshState, mockApi, type State } from "./mockApi";

async function open(page: Page, state: State, key = "test-key") {
  await mockApi(page, state);
  await page.goto("/");
  await page.locator("#company-search").fill("SBIN");
  await expect(page.getByRole("region", { name: "Confirm company" })).toBeVisible();
  if (key) {
    await page.getByLabel("Research access key").fill(key);
    await page.getByRole("button", { name: "Use key" }).click();
  }
}

const start = (page: Page) => page.getByRole("button", { name: "Start research with the defaults" });

test("company selection shows the confirmation card and the research panel", async ({ page }) => {
  await open(page, freshState());
  const card = page.getByRole("region", { name: "Confirm company" });
  await expect(card).toContainText("State Bank of India");
  await expect(card).toContainText("INE062A01020");
  await expect(page.getByRole("region", { name: "Research", exact: true })).toContainText("Research: SBI");
});

test("without an access key research cannot be started", async ({ page }) => {
  await open(page, freshState(), "");
  await expect(start(page)).toBeDisabled();
});

test("partial research shows readiness, missing evidence and the choices", async ({ page }) => {
  await open(page, freshState());
  await start(page).click();
  await expect(page.getByText("Research readiness")).toBeVisible();
  await expect(page.getByText("Missing data assessment")).toBeVisible();
  await expect(page.getByText("Interest earned").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume research" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Generate complete report" })).toBeDisabled();
});

test("an available-evidence report can be generated and is labelled as incomplete", async ({ page }) => {
  const state = freshState();
  await open(page, state);
  await start(page).click();
  await page.getByRole("button", { name: "Generate available-evidence report" }).click();
  await expect(page.getByText("Available-evidence report", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Download this report as PDF" })).toBeVisible();
  expect(state.calls).toContain("POST /api/research/sessions/s1/reports");
});

test("complete research enables the complete report", async ({ page }) => {
  const state = freshState();
  state.readiness = readiness({ eligibility: "AUTOMATIC", score: 88.6, reasons: [] });
  state.missing.missing = { CRITICAL: [], IMPORTANT: [], OPTIONAL: [] };
  state.missing.eligibility = "AUTOMATIC";
  await open(page, state);
  await start(page).click();
  await expect(page.getByRole("button", { name: "Generate complete report" })).toBeEnabled();
  await page.getByRole("button", { name: "Generate complete report" }).click();
  await expect.poll(() => state.reports.at(-1)?.mode).toBe("COMPLETE");
});

test("Resume research calls the resume endpoint", async ({ page }) => {
  const state = freshState();
  await open(page, state);
  await start(page).click();
  await page.getByRole("button", { name: "Resume research" }).click();
  await expect.poll(() => state.calls.some((c) => c.endsWith("/resume"))).toBe(true);
});

test("a document upload is sent to the backend", async ({ page }) => {
  const state = freshState();
  await open(page, state);
  await start(page).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: "results.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4 test"),
  });
  const send = page.getByRole("button", { name: /upload/i }).last();
  if (await send.isEnabled()) await send.click();
  await expect
    .poll(() => state.calls.some((c) => c.startsWith("POST") && c.endsWith("/uploads")))
    .toBe(true);
});

test("an API failure is shown as an alert and the page stays usable", async ({ page }) => {
  const state = freshState();
  state.failures["/api/research/sessions"] = 500;
  await open(page, state);
  await start(page).click();
  await expect(page.getByRole("alert").first()).toBeVisible();
  await expect(start(page)).toBeEnabled();
});

test("a rejected access key (401) is reported, not hidden", async ({ page }) => {
  const state = freshState();
  state.failures["/api/research/sessions"] = 401;
  await open(page, state);
  await start(page).click();
  await expect(page.getByRole("alert").first()).toBeVisible();
});

test("a session whose extraction step failed shows the failure", async ({ page }) => {
  const state = freshState();
  const s = session({ status: "FAILED", last_error: "extraction failed: document unreadable" });
  s.steps = s.steps.map((x) =>
    x.step === "EXTRACTION" ? { ...x, status: "FAILED", error: "document unreadable" } : x,
  );
  state.session = s;
  await open(page, state);
  await start(page).click();
  await expect(page.getByText("document unreadable").first()).toBeVisible();
});
