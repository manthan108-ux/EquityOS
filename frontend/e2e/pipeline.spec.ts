import { expect, test, type Page, type Route } from "@playwright/test";
import { detail, failure, health, metrics, overview, run } from "../src/features/pipeline/fixtures";

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({
    status,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*" },
    body: JSON.stringify(body),
  });

type Mock = { calls: string[]; running: boolean; health: ReturnType<typeof health> };

async function open(page: Page, key = "test-key"): Promise<Mock> {
  const mock: Mock = { calls: [], running: false, health: health() };
  await page.route("http://localhost:8000/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace("/api/pipeline", "");
    if (req.method() === "OPTIONS") return json(route, {});
    mock.calls.push(`${req.method()} ${path}`);
    if (path === "/overview") return json(route, overview());
    if (path === "/runs") return json(route, [run({ status: mock.running ? "PAUSED" : "COMPLETED" })]);
    if (path === "/runs/7/pause") {
      mock.running = true;
      return json(route, run({ status: "PAUSED" }));
    }
    if (path === "/runs/7")
      return json(route, detail({ status: mock.running ? "PAUSED" : "RUNNING" }));
    if (path === "/failures") return json(route, [failure()]);
    if (path === "/health") return json(route, mock.health);
    if (path === "/metrics") return json(route, metrics());
    return json(route, {});
  });
  if (key) await page.addInitScript((k) => sessionStorage.setItem("equityos.apiKey", k), key);
  await page.goto("/pipeline");
  return mock;
}

test("the pipeline page asks for the access key before showing anything", async ({ page }) => {
  await open(page, "");
  await expect(page.getByRole("heading", { level: 1, name: "Data pipeline" })).toBeVisible();
  await expect(page.locator("#main").getByText("Access key needed")).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
});

test("it shows every company's persisted stage states and explains a failure", async ({ page }) => {
  await open(page);
  const grid = page.getByRole("table", { name: /stage status of every company in run 7/i });
  await expect(grid).toBeVisible();
  await expect(grid.getByRole("row", { name: /63 moons technologies/ })).toBeVisible();
  const infra = grid.getByRole("row", { name: /A B Infrabuild/ });
  await expect(infra.getByRole("button", { name: /Failed x2/ })).toBeVisible();
  await infra.getByRole("button", { name: /Failed/ }).click();
  const detailBox = page.getByRole("region", { name: "Stage detail" });
  await expect(detailBox).toContainText("Error: 1 extraction(s) failed");
  await expect(detailBox).toContainText("Next retry");
  await expect(page.getByRole("heading", { name: "Failed stages" })).toBeVisible();
  await expect(page.getByLabel("Stage log")).toContainText("extract failed");
});

test("an active run can be paused from the page and the page then shows it paused", async ({ page }) => {
  const mock = await open(page);
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
  expect(mock.calls).toContain("POST /runs/7/pause");
});

test("the navigation links to the pipeline", async ({ page }) => {
  await open(page);
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Data Pipeline" })).toHaveAttribute("href", "/pipeline");
});

test("it shows pipeline health and how long each stage takes", async ({ page }) => {
  await open(page);
  await expect(page.getByText("Healthy")).toBeVisible();
  const table = page.getByRole("table", { name: "Stage durations" });
  await expect(table).toBeVisible();
  await expect(table.getByRole("row", { name: /Documents/ })).toContainText("1 min 15 s");
});
