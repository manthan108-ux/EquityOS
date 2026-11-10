import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReadinessOverview from "@/features/research/ReadinessOverview";
import * as api from "@/services/research";

vi.mock("@/services/research", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/services/research")>();
  return { ...real, fetchOverview: vi.fn() };
});

const overview: api.Overview = {
  automatic: 1,
  assisted: 1,
  blocked: 0,
  companies: [
    {
      company_id: "c1", name: "TCS", symbol: "TCS", profile: "IT services", score: 88,
      eligibility: "AUTOMATIC", confidence: "MEDIUM", critical_missing: [], important_missing: 2,
      documents_needed: [], cannot_be_settled_by_upload: [],
    },
    {
      company_id: "c2", name: "State Bank of India", symbol: "SBIN", profile: "Bank", score: 65.5,
      eligibility: "ASSISTED", confidence: "LOW",
      critical_missing: [
        { id: "interest_earned", label: "Interest earned", action: "UPLOAD", reason: null },
        { id: "pb", label: "Price-to-book (P/B)", action: "UPLOAD", reason: null },
      ],
      important_missing: 3,
      documents_needed: ["bank-format profit and loss account"],
      cannot_be_settled_by_upload: [],
    },
  ],
};

beforeEach(() => {
  vi.resetAllMocks();
  window.sessionStorage.clear();
});

describe("ReadinessOverview", () => {
  it("shows nothing without a key and does not call the server", () => {
    const { container } = render(<ReadinessOverview />);
    expect(container).toBeEmptyDOMElement();
    expect(api.fetchOverview).not.toHaveBeenCalled();
  });

  it("lists every company with its eligibility, what is missing and what would settle it", async () => {
    api.setApiKey("k");
    vi.mocked(api.fetchOverview).mockResolvedValue(overview);
    render(<ReadinessOverview />);
    expect(await screen.findByText(/1 ready for automatic research · 1 need documents/i)).toBeInTheDocument();
    const sbi = screen.getByText("State Bank of India").closest("tr")!;
    expect(within(sbi).getByText("assisted")).toBeInTheDocument();
    expect(within(sbi).getByText(/interest earned, price-to-book/i)).toBeInTheDocument();
    expect(within(sbi).getByText(/bank-format profit and loss account/)).toBeInTheDocument();
    const tcs = screen.getByText("TCS", { selector: "div.font-medium" }).closest("tr")!;
    expect(within(tcs).getByText("automatic")).toBeInTheDocument();
    expect(within(tcs).getByText("none")).toBeInTheDocument();
  });

  it("says the overview measures evidence, not the company", async () => {
    api.setApiKey("k");
    vi.mocked(api.fetchOverview).mockResolvedValue(overview);
    render(<ReadinessOverview />);
    expect(await screen.findByText(/measures evidence, not how good a company is/i)).toBeInTheDocument();
  });

  it("reports a failure instead of hiding it", async () => {
    api.setApiKey("k");
    vi.mocked(api.fetchOverview).mockRejectedValue(new api.ApiError(500, "x"));
    render(<ReadinessOverview />);
    await waitFor(() => expect(screen.getByText(/could not be loaded/i)).toBeInTheDocument());
  });
});
