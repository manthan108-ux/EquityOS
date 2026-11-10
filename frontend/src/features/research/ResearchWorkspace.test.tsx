import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { missing, readiness, report, session, upload } from "@/features/research/fixtures";
import ResearchWorkspace from "@/features/research/ResearchWorkspace";
import * as api from "@/services/research";

vi.mock("@/services/research", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/services/research")>();
  return {
    ...real,
    startResearch: vi.fn(),
    fetchSession: vi.fn(),
    fetchResearchReadiness: vi.fn(),
    fetchMissing: vi.fn(),
    fetchUploads: vi.fn(),
    fetchReports: vi.fn(),
    fetchReport: vi.fn(),
    continueWithAvailable: vi.fn(),
    resumeResearch: vi.fn(),
    generateReport: vi.fn(),
    updatePreferences: vi.fn(),
    uploadDocuments: vi.fn(),
    clarifyUpload: vi.fn(),
    downloadReportPdf: vi.fn(),
  };
});

const m = vi.mocked(api);

function loaded(over: { ready?: Partial<api.Readiness>; miss?: Partial<api.MissingAssessment>; uploads?: api.Upload[]; reports?: api.ReportRow[] } = {}) {
  m.fetchResearchReadiness.mockResolvedValue(readiness(over.ready));
  m.fetchMissing.mockResolvedValue(missing(over.miss));
  m.fetchUploads.mockResolvedValue(over.uploads ?? []);
  m.fetchReports.mockResolvedValue(over.reports ?? []);
}

const renderWorkspace = () =>
  render(<ResearchWorkspace companyId="c1" companyName="State Bank of India" />);

beforeEach(() => {
  vi.resetAllMocks();
  window.sessionStorage.clear();
  api.setApiKey("test-key");
  loaded();
});

describe("starting research", () => {
  it("cannot start without an access key and says where it comes from", () => {
    api.setApiKey("");
    renderWorkspace();
    expect(screen.getByRole("button", { name: /start research with the defaults/i })).toBeDisabled();
    expect(screen.getByText(/RESEARCH_API_KEYS/)).toBeInTheDocument();
  });

  it("starts with the defaults and shows the progress of every stage", async () => {
    m.startResearch.mockResolvedValue(session());
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    expect(m.startResearch).toHaveBeenCalledWith("c1");
    expect(await screen.findByLabelText(/research progress/i)).toBeInTheDocument();
    expect(screen.getByText("Step EVIDENCE")).toBeInTheDocument();
    expect(screen.getByText(/already done, not repeated/i)).toBeInTheDocument();
    expect(screen.getByText(/\(not needed\)/i)).toBeInTheDocument();
  });

  it("starts with the preferences the user chose", async () => {
    m.startResearch.mockResolvedValue(session());
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByLabelText(/standalone/i));
    await user.click(screen.getByRole("button", { name: /start research with these preferences/i }));
    expect(m.startResearch).toHaveBeenCalledWith(
      "c1",
      expect.objectContaining({ basis: "STANDALONE", period_mode: "LATEST_QUARTER" }),
    );
  });

  it("explains a server that is not configured instead of failing silently", async () => {
    m.startResearch.mockRejectedValue(new api.ApiError(503, "not configured"));
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/research access is not configured/i);
  });

  it("explains a rejected key and a rate limit", async () => {
    m.startResearch.mockRejectedValueOnce(new api.ApiError(401, "bad"));
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/access key was not accepted/i);
    m.startResearch.mockRejectedValueOnce(new api.ApiError(429, "slow"));
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/too many requests/i));
  });
});

describe("assisted research", () => {
  async function started() {
    m.startResearch.mockResolvedValue(session());
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    await screen.findByText(/missing data assessment/i);
    return user;
  }

  it("shows readiness, separate from the company's quality", async () => {
    await started();
    expect(await screen.findByText("65.5")).toBeInTheDocument();
    expect(screen.getByText(/assisted research/i)).toBeInTheDocument();
    expect(screen.getByText(/measures the evidence EquityOS holds, not how good the company is/i)).toBeInTheDocument();
    expect(screen.getByText(/one rule alone is never enough/i)).toBeInTheDocument();
  });

  it("lists exactly what is missing with why, where, which period and basis", async () => {
    await started();
    const card = screen.getByText("Interest earned").closest("li")!;
    expect(within(card).getByText("critical")).toBeInTheDocument();
    expect(within(card).getByText(/interest earned minus interest expended/i)).toBeInTheDocument();
    expect(within(card).getByText(/net interest income, net interest margin/)).toBeInTheDocument();
    expect(within(card).getByText(/bank-format profit and loss account/)).toBeInTheDocument();
    expect(within(card).getByText(/latest available quarter/)).toBeInTheDocument();
    expect(within(card).getByText(/consolidated \(your choice/)).toBeInTheDocument();
    expect(within(card).getByText(/company investor-relations page/)).toBeInTheDocument();
    expect(within(card).getByText(/upload the document below/i)).toBeInTheDocument();
  });

  it("tells the user when a figure exists only on the other basis", async () => {
    await started();
    const card = screen.getByText("Net profit").closest("li")!;
    expect(within(card).getByText(/available on the standalone basis for/i)).toBeInTheDocument();
    expect(within(card).getAllByText(/change the basis preference/i).length).toBeGreaterThan(0);
  });

  it("says which figures and documents are already held, so nothing is asked for twice", async () => {
    const user = await started();
    await user.click(screen.getByText(/already available and validated \(1\)/i));
    expect(screen.getByText(/net interest margin \(nim\)/i)).toBeInTheDocument();
    await user.click(screen.getByText(/documents equityos already holds \(1\)/i));
    expect(screen.getByText(/press release · FY2027-Q2/i)).toBeInTheDocument();
  });

  it("offers the two choices: generate with what exists, or upload", async () => {
    m.continueWithAvailable.mockResolvedValue(session({ status: "COMPLETED" }));
    const user = await started();
    expect(screen.getByRole("button", { name: /generate complete report/i })).toBeDisabled();
    expect(screen.getByRole("heading", { name: /upload supporting documents/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /generate with what is available/i }));
    expect(m.continueWithAvailable).toHaveBeenCalledWith("s1");
  });

  it("blocks the partial report when no revenue or profit is held at all", async () => {
    loaded({ ready: { eligibility: "BLOCKED" }, miss: { eligibility: "BLOCKED", options: { continue_with_available: false } } });
    await started();
    expect(screen.getByText(/no validated revenue or profit/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate with what is available/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /generate available-evidence report/i })).toBeDisabled();
  });
});

describe("preferences in a running session", () => {
  it("applies a changed basis to the session and keeps the choice visible", async () => {
    m.startResearch.mockResolvedValue(session());
    m.updatePreferences.mockResolvedValue(session({ preferences: { ...session().preferences, basis: "STANDALONE" } }));
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    const panel = (await screen.findAllByLabelText(/research preferences/i))[0];
    await user.click(within(panel).getByLabelText(/standalone/i));
    await user.click(within(panel).getByRole("button", { name: /apply preferences/i }));
    expect(m.updatePreferences).toHaveBeenCalledWith("s1", expect.objectContaining({ basis: "STANDALONE" }));
    await waitFor(() => expect(within(screen.getByLabelText(/research preferences/i)).getByLabelText(/standalone/i)).toBeChecked());
  });
});

describe("automatic research and reports", () => {
  const auto = () => {
    loaded({
      ready: { eligibility: "AUTOMATIC", score: 88, reasons: [], counts: { requirements: 18, available: 18, critical_missing: 0, important_missing: 0, optional_missing: 0 } },
      miss: { eligibility: "AUTOMATIC", missing: { CRITICAL: [], IMPORTANT: [], OPTIONAL: [] } },
      reports: [{ id: "r1", version: 1, mode: "COMPLETE", readiness_score: 88, missing_critical: 0, missing_important: 0, created_at: "2026-11-10T10:00:00Z", complete: true }],
    });
    m.fetchReport.mockResolvedValue(report({ mode: "COMPLETE", complete: true }));
    m.startResearch.mockResolvedValue(session({ status: "COMPLETED", eligibility: "AUTOMATIC", readiness_score: 88 }));
  };

  it("shows no missing-data assessment and a complete report straight away", async () => {
    auto();
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    expect(await screen.findByText(/complete research report v1/i)).toBeInTheDocument();
    expect(screen.queryByText(/missing data assessment/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generate complete report/i })).toBeEnabled();
    expect(screen.getByText(/v1 · complete · 0 critical gap/i)).toBeInTheDocument();
  });

  it("downloads the report as a PDF", async () => {
    auto();
    m.downloadReportPdf.mockResolvedValue(new Blob(["%PDF-"]));
    const url = vi.fn(() => "blob:x");
    const revoke = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: url, revokeObjectURL: revoke }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    await user.click(await screen.findByRole("button", { name: /download this report as pdf/i }));
    expect(m.downloadReportPdf).toHaveBeenCalledWith("r1");
    await waitFor(() => expect(revoke).toHaveBeenCalled());
    expect(click).toHaveBeenCalled();
    click.mockRestore();
    vi.unstubAllGlobals();
  });

  it("explains why a complete report was refused", async () => {
    loaded();
    m.startResearch.mockResolvedValue(session());
    m.generateReport.mockRejectedValue(new api.ApiError(409, "report not allowed"));
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    await screen.findByText(/missing data assessment/i);
    await user.click(screen.getByRole("button", { name: /generate available-evidence report/i }));
    expect(m.generateReport).toHaveBeenCalledWith("s1", "AVAILABLE_EVIDENCE");
    expect(await screen.findByRole("alert")).toHaveTextContent(/report not allowed/i);
  });
});

describe("uploads", () => {
  async function ready(uploads: api.Upload[] = []) {
    loaded({ uploads });
    m.startResearch.mockResolvedValue(session());
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(screen.getByRole("button", { name: /start research with the defaults/i }));
    await screen.findByRole("heading", { name: /upload supporting documents/i });
    return user;
  }

  it("uploads several files together as published filings by default", async () => {
    m.uploadDocuments.mockResolvedValue({ uploads: [upload(), upload({ id: "u2", filename: "deck.pdf" })] });
    const user = await ready();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, [new File(["a"], "results.pdf"), new File(["b"], "deck.pdf")]);
    await user.click(screen.getByRole("button", { name: /upload 2 files/i }));
    expect(m.uploadDocuments).toHaveBeenCalledWith("s1", expect.any(Array), "PUBLIC_FILING");
    expect(m.uploadDocuments.mock.calls[0][1]).toHaveLength(2);
    expect(await screen.findByText("results.pdf")).toBeInTheDocument();
    expect(screen.getByText("deck.pdf")).toBeInTheDocument();
  });

  it("warns that private documents cannot settle a requirement", async () => {
    const user = await ready();
    await user.selectOptions(screen.getByLabelText(/these documents are/i), "USER_LICENSED");
    expect(screen.getByText(/not read into the shared evidence/i)).toBeInTheDocument();
  });

  it("shows each upload's validation outcome, what it settled and any disagreement", async () => {
    await ready([
      upload({ warnings: ["2 value(s) were read but not accepted"], conflicts: [{ metric: "INCOME.revenue", period_end: "2026-03-31", selected: "EXISTING", note: "differs" }] }),
      upload({ id: "u3", filename: "scan.pdf", status: "REJECTED", reject_reason: "the pages are scans without a text layer", resolved_requirements: [] }),
    ]);
    expect(screen.getByText(/settled: nim/i)).toBeInTheDocument();
    expect(screen.getByText(/1 value\(s\) disagree with what EquityOS already held/i)).toBeInTheDocument();
    expect(screen.getByText(/2 value\(s\) were read but not accepted/i)).toBeInTheDocument();
    expect(screen.getByText(/pages are scans without a text layer/i)).toBeInTheDocument();
    expect(screen.getAllByText(/issuer confirmed/i).length).toBeGreaterThan(0);
  });

  it("asks the user to settle an ambiguity instead of accepting silently", async () => {
    m.clarifyUpload.mockResolvedValue(upload({ id: "u4", filename: "release.pdf" }));
    const user = await ready([
      upload({
        id: "u4",
        filename: "release.pdf",
        status: "NEEDS_CLARIFICATION",
        issuer_status: "UNVERIFIED",
        ambiguities: [{ code: "ISSUER_UNCONFIRMED", message: "the issuer could not be read from this document", options: ["confirm_issuer"] }],
      }),
    ]);
    expect(screen.getByText(/issuer could not be read/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /this company's own document/i }));
    expect(m.clarifyUpload).toHaveBeenCalledWith("u4", { confirm_issuer: true });
    await waitFor(() => expect(screen.queryByText(/issuer could not be read/i)).not.toBeInTheDocument());
  });
});
