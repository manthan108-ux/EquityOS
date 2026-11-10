import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SessionHistory from "@/features/research/SessionHistory";
import * as api from "@/services/research";

vi.mock("@/services/research", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/services/research")>();
  return { ...real, listSessions: vi.fn() };
});
vi.mock("@/features/research/ResearchWorkspace", () => ({
  default: ({ companyName, sessionId }: { companyName: string; sessionId?: string }) => (
    <div>workspace for {companyName} / {sessionId}</div>
  ),
}));

const rows: api.SessionRow[] = [
  { id: "s1", company_id: "c1", company: "State Bank of India", status: "AWAITING_DOCUMENTS", eligibility: "ASSISTED", readiness_score: 65.5, latest_report_version: null, updated_at: "2026-11-10T10:00:00Z" },
  { id: "s2", company_id: "c2", company: "TCS", status: "COMPLETED", eligibility: "AUTOMATIC", readiness_score: 88, latest_report_version: 2, updated_at: "2026-11-09T10:00:00Z" },
];

beforeEach(() => {
  vi.resetAllMocks();
  window.sessionStorage.clear();
});

describe("SessionHistory", () => {
  it("shows nothing without a key", () => {
    vi.mocked(api.listSessions).mockResolvedValue(rows);
    const { container } = render(<SessionHistory />);
    expect(container).toBeEmptyDOMElement();
    expect(api.listSessions).not.toHaveBeenCalled();
  });

  it("lists saved sessions with their state and offers to resume the incomplete ones", async () => {
    api.setApiKey("k");
    vi.mocked(api.listSessions).mockResolvedValue(rows);
    const user = userEvent.setup();
    render(<SessionHistory />);
    expect(await screen.findByText("State Bank of India")).toBeInTheDocument();
    expect(screen.getByText(/saved: waiting for documents · readiness 65.5/i)).toBeInTheDocument();
    expect(screen.getByText(/report ready · readiness 88 · report v2/i)).toBeInTheDocument();
    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("Resume");
    expect(buttons[1]).toHaveTextContent("Open");
    await user.click(buttons[0]);
    expect(screen.getByText(/workspace for State Bank of India \/ s1/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByText(/workspace for/)).not.toBeInTheDocument());
  });

  it("stays hidden when there is no history or the server refuses", async () => {
    api.setApiKey("k");
    vi.mocked(api.listSessions).mockRejectedValue(new api.ApiError(401, "no"));
    const { container } = render(<SessionHistory />);
    await waitFor(() => expect(api.listSessions).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
