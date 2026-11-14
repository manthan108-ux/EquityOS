import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import PipelineDashboard, { duration } from "@/features/pipeline/PipelineDashboard";
import { detail, failure, health, metrics, overview, run } from "@/features/pipeline/fixtures";
import { setApiKey } from "@/services/research";

type Reply = { status?: number; body: unknown };
type Handler = (url: string, init?: RequestInit) => Reply;

const pathOf = (url: string) => url.replace(/^.*\/api\/pipeline/, "");

function serve(handler: Handler) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${pathOf(url)}`);
      const out = handler(url, init);
      const status = out.status ?? 200;
      return { ok: status < 400, status, json: async () => out.body } as Response;
    }),
  );
  return calls;
}
const base: Handler = (url) => {
  const path = pathOf(url);
  if (path.startsWith("/overview")) return { body: overview() };
  if (path.startsWith("/runs?")) return { body: [run()] };
  if (path.startsWith("/runs/7")) return { body: detail() };
  if (path.startsWith("/failures")) return { body: [failure()] };
  return { body: {} };
};

beforeEach(() => setApiKey("k"));
afterEach(() => {
  vi.unstubAllGlobals();
  setApiKey("");
});

describe("PipelineDashboard", () => {
  it("shows the persisted state of every stage of every company, not an aggregate", async () => {
    serve(base);
    render(<PipelineDashboard />);
    const grid = await screen.findByRole("table", { name: /stage status of every company in run 7/i });
    expect(within(grid).getByText("63 moons technologies")).toBeInTheDocument();
    const infra = within(grid).getByRole("row", { name: /A B Infrabuild/ });
    expect(within(infra).getByText(/Failed x2/)).toBeInTheDocument();
    expect(within(infra).getByText("Skipped")).toBeInTheDocument();
    expect(within(infra).getAllByLabelText("not run").length).toBeGreaterThan(0);
  });

  it("opens a stage to show its message, error, retry time and stored result", async () => {
    const user = userEvent.setup();
    serve(base);
    render(<PipelineDashboard />);
    const grid = await screen.findByRole("table", { name: /stage status/i });
    const infra = within(grid).getByRole("row", { name: /A B Infrabuild/ });
    await user.click(within(infra).getByRole("button", { name: /Failed/ }));
    const region = screen.getByRole("region", { name: "Stage detail" });
    expect(region).toHaveTextContent("Error: 1 extraction(s) failed");
    expect(region).toHaveTextContent("Next retry");
    const ok = within(grid).getByRole("row", { name: /63 moons/ });
    await user.click(within(ok).getAllByRole("button", { name: /Done/ })[1]);
    expect(screen.getByRole("region", { name: "Stage detail" })).toHaveTextContent(
      "3 document(s): 3 completed",
    );
  });

  it("lists failing stages with the reason and the next retry", async () => {
    serve(base);
    render(<PipelineDashboard />);
    const failed = (await screen.findByText("Failed stages")).closest("section")!;
    expect(failed).toHaveTextContent("A B Infrabuild - Extraction");
    expect(failed).toHaveTextContent("1 extraction(s) failed");
    expect(failed).toHaveTextContent("Next automatic retry");
  });

  it("reports the backlog from the database", async () => {
    serve(base);
    render(<PipelineDashboard />);
    const card = (await screen.findByText("Where the data stands")).closest("section")!;
    expect(card).toHaveTextContent("Unread documents");
    expect(card).toHaveTextContent("158");
  });

  it("starts a single-company, stage-specific run", async () => {
    const user = userEvent.setup();
    let body: Record<string, unknown> = {};
    const calls = serve((url, init) => {
      if (init?.method === "POST" && url.endsWith("/runs")) {
        body = JSON.parse(String(init.body));
        return { status: 201, body: run({ id: 8, status: "QUEUED" }) };
      }
      return base(url, init);
    });
    render(<PipelineDashboard />);
    await user.type(await screen.findByLabelText(/companies \(symbols\)/i), "63MOONS");
    for (const label of ["Industry label", "Website", "Upstox data", "Documents", "Calculation", "Enrichment", "Report"])
      await user.click(screen.getByRole("checkbox", { name: label }));
    await user.click(screen.getByRole("button", { name: "Run selected stages" }));
    await waitFor(() => expect(calls).toContain("POST /runs"));
    expect(body).toMatchObject({ symbols: ["63MOONS"], stages: ["extract", "evidence", "readiness"] });
  });

  it("pauses and cancels the shown run while it is active", async () => {
    const user = userEvent.setup();
    const calls = serve((url, init) => {
      if (pathOf(url).startsWith("/runs/7/")) return { body: run({ status: "PAUSED" }) };
      if (pathOf(url).startsWith("/runs/7")) return { body: detail({ status: "RUNNING" }) };
      return base(url, init);
    });
    render(<PipelineDashboard />);
    await user.click(await screen.findByRole("button", { name: "Pause" }));
    await waitFor(() => expect(calls).toContain("POST /runs/7/pause"));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(calls).toContain("POST /runs/7/cancel"));
  });

  it("offers no controls on a finished run", async () => {
    serve(base);
    render(<PipelineDashboard />);
    await screen.findByRole("table", { name: /stage status of every company/i });
    for (const name of ["Pause", "Resume", "Cancel"])
      expect(screen.queryByRole("button", { name })).toBeNull();
  });

  it("explains that control needs an admin key instead of failing silently", async () => {
    const user = userEvent.setup();
    serve((url, init) => {
      if (init?.method === "POST")
        return { status: 403, body: { detail: "starting or changing pipeline work needs an admin key" } };
      return base(url, init);
    });
    render(<PipelineDashboard />);
    await user.click(await screen.findByRole("button", { name: "Run full pipeline" }));
    expect(await screen.findByText(/did not work/i)).toBeInTheDocument();
  });

  it("saves the schedule", async () => {
    const user = userEvent.setup();
    let saved: Record<string, unknown> = {};
    serve((url, init) => {
      if (init?.method === "PUT") {
        saved = JSON.parse(String(init.body));
        return { body: overview().schedule };
      }
      return base(url, init);
    });
    render(<PipelineDashboard />);
    await user.click(await screen.findByRole("checkbox", { name: /scheduled runs enabled/i }));
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() =>
      expect(saved).toMatchObject({
        enabled: true,
        interval_minutes: 360,
        batch_size: 10,
        concurrency: 2,
      }),
    );
  });

  it("shows a clear error when the backend cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("offline");
      }),
    );
    render(<PipelineDashboard />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not reach the server/i);
  });
});

describe("monitoring", () => {
  const withMonitoring = (h = health(), m = metrics()): Handler => (url, init) => {
    const path = pathOf(url);
    if (path.startsWith("/health")) return { body: h };
    if (path.startsWith("/metrics")) return { body: m };
    return base(url, init);
  };

  it("says the pipeline is healthy when there are no alerts", async () => {
    serve(withMonitoring());
    render(<PipelineDashboard />);
    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText(/worker: running/i)).toBeInTheDocument();
    expect(screen.getByText(/no stuck stages/i)).toBeInTheDocument();
  });

  it("lists each alert with its level, worst first", async () => {
    serve(
      withMonitoring(
        health({
          status: "CRITICAL",
          worker: { last_seen_at: null, note: null, alive: false },
          alerts: [
            { level: "CRITICAL", code: "STAGE_STUCK", message: "63 moons: documents has been running for 95 min", stage: "documents" },
            { level: "WARNING", code: "STAGE_TIMEOUTS", message: "documents: 1 attempt(s) ran out of time", stage: "documents" },
          ],
        }),
      ),
    );
    render(<PipelineDashboard />);
    expect(await screen.findByText("Not working")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Alerts" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent(/critical.*95 min/i);
    expect(items[1]).toHaveTextContent(/warning.*ran out of time/i);
    expect(screen.getByText(/worker: not reporting/i)).toBeInTheDocument();
  });

  it("shows how long each stage takes beside its time limit, and flags timeouts", async () => {
    serve(withMonitoring());
    render(<PipelineDashboard />);
    const table = await screen.findByRole("table", { name: "Stage durations" });
    const documents = within(table).getByRole("row", { name: /Documents/ });
    expect(within(documents).getByText("1 min 15 s")).toBeInTheDocument(); // typical
    expect(within(documents).getByText("13 min")).toBeInTheDocument(); // slowest 5%
    expect(within(documents).getAllByText("15 min")).toHaveLength(2); // longest = limit here
    expect(within(documents).getAllByText("1").length).toBeGreaterThan(0); // timed out
    expect(screen.getByText(/#7 completed, 2\/2 companies, 5 min, 1 failed/)).toBeInTheDocument();
  });

  it("still shows the rest of the page when monitoring cannot be read", async () => {
    serve((url, init) => {
      const path = pathOf(url);
      if (path.startsWith("/health") || path.startsWith("/metrics")) return { status: 500, body: { detail: "boom" } };
      return base(url, init);
    });
    render(<PipelineDashboard />);
    expect(await screen.findByText("Where the data stands")).toBeInTheDocument();
    expect(screen.queryByText("Pipeline health")).not.toBeInTheDocument();
  });

  it("sends the selected companies with the schedule", async () => {
    const user = userEvent.setup();
    let saved: Record<string, unknown> = {};
    serve((url, init) => {
      if (init?.method === "PUT") {
        saved = JSON.parse(String(init.body));
        return { body: overview().schedule };
      }
      return withMonitoring()(url, init);
    });
    render(<PipelineDashboard />);
    await user.type(await screen.findByLabelText(/companies on the schedule/i), "ICICIBANK, TCS");
    await user.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => expect(saved).toMatchObject({ symbols: ["ICICIBANK", "TCS"] }));
  });

  it("formats durations for people", () => {
    expect(duration(null)).toBe("-");
    expect(duration(0.4)).toBe("0.4 s");
    expect(duration(42)).toBe("42 s");
    expect(duration(75)).toBe("1 min 15 s");
    expect(duration(900)).toBe("15 min");
    expect(duration(5400)).toBe("1 h 30 min");
  });
});
