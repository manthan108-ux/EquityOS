import type { Page, Route } from "@playwright/test";
import { missing, readiness, report, session, upload } from "../src/features/research/fixtures";

export const COMPANY = {
  id: "c1",
  legal_name: "State Bank of India",
  common_name: "SBI",
  sector: "Banking",
  industry: "Public sector bank",
  listings: [
    { exchange: "NSE", symbol: "SBIN", security_code: null, isin: "INE062A01020", status: "ACTIVE" },
    { exchange: "BSE", symbol: "SBIN", security_code: "500112", isin: "INE062A01020", status: "ACTIVE" },
  ],
};

export type State = {
  session: ReturnType<typeof session> | null;
  readiness: ReturnType<typeof readiness>;
  missing: ReturnType<typeof missing>;
  uploads: ReturnType<typeof upload>[];
  reports: ReturnType<typeof report>[];
  failures: Record<string, number>; // path fragment -> HTTP status to answer with
  calls: string[];
};

export const freshState = (): State => ({
  session: null,
  readiness: readiness(),
  missing: missing(),
  uploads: [],
  reports: [],
  failures: {},
  calls: [],
});

const json = (route: Route, body: unknown, status = 200) =>
  route.fulfill({
    status,
    contentType: "application/json",
    headers: { "access-control-allow-origin": "*", "access-control-allow-headers": "*" },
    body: JSON.stringify(body),
  });

/** Answer every backend call from `state`. Tests mutate `state` to script a scenario. */
export async function mockApi(page: Page, state: State) {
  await page.route("http://localhost:8000/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    if (method === "OPTIONS") return json(route, {});
    state.calls.push(`${method} ${path}`);
    for (const [fragment, status] of Object.entries(state.failures)) {
      if (path.includes(fragment) && method !== "GET")
        return json(route, { detail: `scripted failure ${status}` }, status);
    }
    if (path === "/health") return json(route, { status: "ok", service: "equityos", version: "test" });
    if (path === "/health/ready") return json(route, { status: "ok", database: "ok", detail: null });
    if (path === "/api/companies/search")
      return json(route, {
        query: url.searchParams.get("q"),
        status: "RESOLVED",
        matches: [{ company: COMPANY, match_type: "TICKER", score: 1, matched_on: "SBIN" }],
        notes: [],
      });
    if (path === "/api/companies/c1") return json(route, COMPANY);
    if (path === "/api/research/overview")
      return json(route, { companies: [], automatic: 0, assisted: 0, blocked: 0 });
    if (path === "/api/research/sessions" && method === "GET")
      return json(
        route,
        state.session
          ? [{ id: state.session.id, company_id: "c1", company: "SBI", status: state.session.status, eligibility: state.session.eligibility, readiness_score: state.session.readiness_score, latest_report_version: state.reports.length || null, updated_at: state.session.updated_at }]
          : [],
      );
    if (path === "/api/research/reports" && method === "GET")
      return json(route, state.reports.map((r) => ({ ...r, session_id: "s1", company_id: "c1" })));
    const pdf = path.match(/^\/api\/research\/reports\/[^/]+\/pdf$/);
    if (pdf) {
      state.calls.push("PDF");
      return route.fulfill({ status: 200, contentType: "application/pdf", headers: { "access-control-allow-origin": "*" }, body: Buffer.from("%PDF-1.4 test") });
    }
    if (path === "/api/research/sessions" && method === "POST") {
      state.session ??= session();
      return json(route, state.session);
    }
    const m = path.match(/^\/api\/research\/sessions\/[^/]+(\/.*)?$/);
    if (m) {
      const tail = m[1] ?? "";
      if (tail === "") return json(route, state.session);
      if (tail === "/readiness") return json(route, state.readiness);
      if (tail === "/missing") return json(route, state.missing);
      if (tail === "/uploads" && method === "GET") return json(route, state.uploads);
      if (tail === "/uploads" && method === "POST") {
        const u = upload({ id: `u${state.uploads.length + 1}`, filename: "results.pdf" });
        state.uploads.push(u);
        return json(route, { uploads: [u] });
      }
      if (tail === "/reports" && method === "GET") return json(route, state.reports);
      if (tail === "/reports" && method === "POST") {
        const body = JSON.parse(req.postData() ?? "{}");
        const r = report({
          id: `r${state.reports.length + 1}`,
          version: state.reports.length + 1,
          mode: body.mode,
          complete: body.mode === "COMPLETE",
        });
        state.reports.push(r);
        return json(route, r);
      }
      if (tail === "/resume" || tail === "/continue") {
        if (state.session && state.session.status === "FAILED") {
          state.session = { ...state.session, status: "AWAITING_DOCUMENTS", last_error: null, steps: state.session.steps.map((x) => ({ ...x, status: "DONE", error: null })) };
        }
        return json(route, state.session);
      }
    }
    const rm = path.match(/^\/api\/research\/reports\/([^/]+)$/);
    if (rm) return json(route, state.reports.find((r) => r.id === rm[1]) ?? {});
    // anything else the page asks for (scores, peers, evidence ...) is simply not available here
    return json(route, { detail: "not available in the browser test" }, 404);
  });
}
