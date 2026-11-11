import { expect, test, type Page } from "@playwright/test";
import { readiness, session } from "../src/features/research/fixtures";
import { freshState, mockApi, type State } from "./mockApi";

const KEY = "test-key";

/** Sign in with the access key (as a user would, in Settings) and open the SBI workspace by search. */
async function signIn(page: Page, state: State, key = KEY) {
  await mockApi(page, state);
  if (key) await page.addInitScript((k) => sessionStorage.setItem("equityos.apiKey", k), key);
}
async function openCompany(page: Page) {
  await page.goto("/");
  await page.locator("#company-search").fill("SBIN");
  const card = page.getByRole("region", { name: "Confirm company" });
  await expect(card).toContainText("State Bank of India");
  await expect(card).toContainText("INE062A01020");
  await card.getByRole("link", { name: "Open research workspace" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "SBI" })).toBeVisible();
}
const tab = (page: Page, name: string | RegExp) => page.getByRole("tab", { name });
const alertBox = (page: Page) => page.locator("[role=alert]:not(#__next-route-announcer__)").first();
const start = (page: Page) => page.getByRole("button", { name: "Start research" }).first();

test("the whole app is light-themed", async ({ page }) => {
  await signIn(page, freshState());
  await page.goto("/");
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(bg).toBe("rgb(245, 247, 250)");
  expect(await page.evaluate(() => matchMedia("(prefers-color-scheme: dark)").matches || document.documentElement.classList.contains("dark"))).toBe(false);
});

test("search shows the match, asks for confirmation, and only then opens the workspace", async ({ page }) => {
  await signIn(page, freshState());
  await page.goto("/");
  await page.locator("#company-search").fill("SBIN");
  await expect(page.getByRole("region", { name: "Confirm company" })).toBeVisible();
  await expect(page).toHaveURL(/\/$/); // nothing opened by itself
  await page.getByRole("link", { name: "Open research workspace" }).click();
  await expect(page).toHaveURL(/\/companies\/c1/);
  await expect(page.getByRole("tab", { selected: true })).toHaveText("Overview");
});

test("every sidebar link leads to a working page", async ({ page }) => {
  await signIn(page, freshState());
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const [label, heading] of [["Companies", "Companies"], ["Research Workspace", "Research workspace"], ["Research Readiness", "Research readiness"], ["Documents", "Documents"], ["Reports", "Reports"], ["Settings", "Settings"]] as const) {
    await nav.getByRole("link", { name: label }).click();
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  }
});

test("without an access key research cannot be started and the user is told why", async ({ page }) => {
  await signIn(page, freshState(), "");
  await openCompany(page);
  await expect(page.getByRole("heading", { name: "Research access key needed" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Access key needed" })).toBeVisible(); // also flagged in the top bar
  await page.getByLabel("Research access key").fill(KEY);
  await page.getByRole("button", { name: "Use key" }).click();
  await expect(page.getByRole("link", { name: "Access key set" })).toBeVisible();
  await expect(start(page)).toBeEnabled();
});

test("starting research shows the pipeline, readiness, and the documents required", async ({ page }) => {
  await signIn(page, freshState());
  await openCompany(page);
  await start(page).click();
  await expect(page.getByRole("list", { name: "Research stages" }).getByRole("listitem")).toHaveCount(10);
  await tab(page, "Research readiness").click();
  await expect(page.getByLabel("Missing document summary")).toContainText("Critical documents missing");
  await expect(page.getByRole("progressbar", { name: "Overall research readiness" })).toBeVisible();
  await tab(page, /Documents/).click();
  await expect(page.getByRole("heading", { name: "Documents Required for Research" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Critical documents/ })).toBeVisible();
  await expect(page.getByText("bank-format profit and loss account").first()).toBeVisible();
  // the global pilot table is NOT part of an individual company's workflow
  await expect(page.getByText("Research readiness of every company")).toHaveCount(0);
});

test("a document can be uploaded for a requirement, processed, and research resumed", async ({ page }) => {
  const state = freshState();
  await signIn(page, state);
  await openCompany(page);
  await start(page).click();
  await tab(page, /Documents/).click();
  await page.getByRole("button", { name: "Upload document", exact: true }).first().click();
  await expect(page.getByText(/Uploading for:/)).toBeVisible();
  await page.locator("#file-input").setInputFiles({ name: "results.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4 test") });
  await expect(page.getByRole("list", { name: "Files ready to upload" })).toContainText("results.pdf");
  await page.getByRole("button", { name: /Upload 1 file/ }).click();
  await expect(page.getByText("Uploaded documents (1)")).toBeVisible();
  await expect(page.getByText("Validated").first()).toBeVisible();
  await page.getByRole("button", { name: "Resume research" }).last().click();
  await expect.poll(() => state.calls.filter((c) => c.endsWith("/resume")).length).toBeGreaterThan(0);
});

test("a rejected upload shows a clear failure and keeps the page usable", async ({ page }) => {
  const state = freshState();
  state.failures["/uploads"] = 413;
  await signIn(page, state);
  await openCompany(page);
  await start(page).click();
  await tab(page, /Documents/).click();
  await page.locator("#file-input").setInputFiles({ name: "big.pdf", mimeType: "application/pdf", buffer: Buffer.from("x") });
  await page.getByRole("button", { name: /Upload 1 file/ }).click();
  await expect(alertBox(page)).toContainText(/larger than the upload limit/i);
  await expect(page.getByRole("heading", { name: "Documents Required for Research" })).toBeVisible();
});

test("an available-evidence report can be generated, read, and downloaded as a PDF", async ({ page }) => {
  const state = freshState();
  await signIn(page, state);
  await openCompany(page);
  await start(page).click();
  await tab(page, "Reports").click();
  await page.getByRole("button", { name: "Generate available-evidence report" }).click();
  await expect(page.getByRole("heading", { name: "Report version 1" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Report sections" })).toBeVisible(); // table of contents
  await expect(page.getByRole("button", { name: "Generate complete report" })).toBeDisabled();
  expect(state.calls).toContain("POST /api/research/sessions/s1/reports");
  await page.getByRole("button", { name: "Download PDF" }).last().click();
  await expect.poll(() => state.calls.includes("PDF")).toBe(true);
});

test("complete research enables the complete report", async ({ page }) => {
  const state = freshState();
  state.readiness = readiness({ eligibility: "AUTOMATIC", score: 88.6, reasons: [] });
  state.missing.missing = { CRITICAL: [], IMPORTANT: [], OPTIONAL: [] };
  state.missing.eligibility = "AUTOMATIC";
  await signIn(page, state);
  await openCompany(page);
  await start(page).click();
  await tab(page, "Reports").click();
  await expect(page.getByRole("button", { name: "Generate complete report" })).toBeEnabled();
  await page.getByRole("button", { name: "Generate complete report" }).click();
  await expect.poll(() => state.reports.at(-1)?.mode).toBe("COMPLETE");
});

test("an API failure is explained in plain words and the page stays usable", async ({ page }) => {
  const state = freshState();
  state.failures["/api/research/sessions"] = 500;
  await signIn(page, state);
  await openCompany(page);
  await start(page).click();
  await expect(alertBox(page)).toContainText(/could not complete that request/i);
  await expect(page.getByText("Technical details")).toBeVisible();
  await expect(page.getByRole("tab", { name: "Financials" })).toBeVisible();
});

test("a rejected access key (401) is reported", async ({ page }) => {
  const state = freshState();
  state.failures["/api/research/sessions"] = 401;
  await signIn(page, state);
  await openCompany(page);
  await start(page).click();
  await expect(alertBox(page)).toContainText("access key was not accepted");
});

test("a stage that failed is explained without raw exceptions, and retry resumes it", async ({ page }) => {
  const state = freshState();
  const s = session({ status: "FAILED", last_error: "Inventory documents: InvalidRequestError while running this stage" });
  s.steps[2] = { ...s.steps[2], status: "FAILED", error: "InvalidRequestError while running this stage", attempts: 2 };
  state.session = s;
  await signIn(page, state);
  await page.goto("/companies/c1");
  await expect(page.getByText(/hit an unexpected problem/i)).toBeVisible();
  await expect(page.getByText("InvalidRequestError while running this stage")).toBeHidden(); // only inside the closed technical details
  await page.getByRole("button", { name: "Retry from the failed stage" }).first().click();
  await expect.poll(() => state.calls.some((c) => c.endsWith("/resume"))).toBe(true);
});

test("sessions are grouped by company and an unavailable report is said to be unavailable", async ({ page }) => {
  const state = freshState();
  state.session = session();
  await signIn(page, state);
  await page.goto("/research");
  await expect(page.getByRole("link", { name: "SBI" })).toBeVisible();
  await expect(page.getByText("Waiting for documents").first()).toBeVisible();
  await page.goto("/companies/c1?tab=reports");
  await expect(page.getByText("No report has been generated for this session yet.")).toBeVisible();
  await page.goto("/reports");
  await expect(page.getByText("No reports yet")).toBeVisible();
});

test("the mobile layout has no horizontal scroll and a collapsible navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page, freshState());
  await page.goto("/companies/c1");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
});
