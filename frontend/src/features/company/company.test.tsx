import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { missing, nim, otherBasis, readiness, session, upload } from "@/features/research/fixtures";
import type { PeerRow, Report } from "@/services/research";
import DocumentRequirements, { docAnchor } from "./DocumentRequirements";
import PeersTab from "./PeersTab";
import Pipeline from "./Pipeline";
import ReadinessPanel from "./ReadinessPanel";
import { uploadStage } from "./Uploader";

describe("Pipeline", () => {
  it("shows ten stages with understandable statuses", () => {
    render(<Pipeline session={session()} busy={false} onRetry={() => {}} />);
    expect(screen.getAllByRole("listitem").filter((li) => li.getAttribute("aria-label")?.includes(":"))).toHaveLength(10);
    expect(screen.getByLabelText(/Step SOURCES: Not required/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Step IDENTITY: Completed/)).toBeInTheDocument();
  });

  it("explains a failure in plain words, hides the exception, and offers a retry", async () => {
    const s = session({ status: "FAILED", last_error: "Inventory documents: InvalidRequestError while running this stage" });
    s.steps[2] = { ...s.steps[2], status: "FAILED", error: "InvalidRequestError while running this stage", attempts: 2 };
    const retry = vi.fn();
    render(<Pipeline session={s} busy={false} onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/hit an unexpected problem/i);
    expect(screen.getByRole("alert")).not.toHaveTextContent("InvalidRequestError");
    expect(screen.getByText(/Technical details/)).toBeInTheDocument(); // the raw text lives in the expander
    await userEvent.click(screen.getByRole("button", { name: /retry from the failed stage/i }));
    expect(retry).toHaveBeenCalled();
  });
});

describe("Documents required", () => {
  const onUpload = vi.fn();

  it("leads with the critical documents, each with why, period, basis, formats and source", () => {
    render(<DocumentRequirements missing={missing()} uploads={[]} onUpload={onUpload} />);
    expect(screen.getByRole("heading", { name: "Documents Required for Research" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Critical documents \(1\)/ })).toHaveTextContent("2 figures"); // two figures, one document
    const card = document.getElementById(docAnchor(nim))!;
    expect(within(card).getByRole("heading", { level: 4 })).toHaveTextContent(new RegExp(nim.document, "i"));
    expect(card).toHaveTextContent(nim.why_it_matters);
    expect(within(card).getByRole("list", { name: "Figures this document settles" })).toHaveTextContent(otherBasis.label);
    expect(card).toHaveTextContent(nim.preferred_period);
    expect(card).toHaveTextContent("PDF, XLSX or CSV");
    expect(card).toHaveTextContent(nim.official_source);
    expect(within(card).getByText("Critical: missing")).toBeInTheDocument();
  });

  it("summarises blockers and links straight to each missing requirement", () => {
    render(<DocumentRequirements missing={missing()} uploads={[upload({ status: "ACCEPTED" })]} onUpload={onUpload} />);
    expect(screen.getByText("Critical missing").parentElement).toHaveTextContent("2");
    expect(screen.getByText("Validated").parentElement).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: nim.label })).toBeInTheDocument();
  });

  it("says when a figure exists only on the other basis and is not used", () => {
    render(<DocumentRequirements missing={missing()} uploads={[]} onUpload={onUpload} />);
    expect(document.getElementById(docAnchor(otherBasis))).toHaveTextContent(/standalone basis.*not used unless you change the basis/i);
  });

  it("starts an upload for the chosen requirement, and refuses when upload cannot help", async () => {
    const un = { ...nim, id: "cet1", label: "CET1", document: "Pillar 3 disclosure", upload_helps: false, action: "NOT_RESOLVABLE_BY_UPLOAD" };
    render(<DocumentRequirements missing={missing({ missing: { CRITICAL: [nim], IMPORTANT: [un], OPTIONAL: [] } })} uploads={[]} onUpload={onUpload} />);
    await userEvent.click(within(document.getElementById(docAnchor(nim))!).getByRole("button", { name: "Upload document" }));
    expect(onUpload).toHaveBeenCalledWith(expect.objectContaining({ id: nim.id }));
    expect(within(document.getElementById(docAnchor(un))!).getByRole("button", { name: "Upload document" })).toBeDisabled();
  });

  it("lists what is already available, with values, so nothing is asked twice", () => {
    render(<DocumentRequirements missing={missing()} uploads={[]} onUpload={onUpload} />);
    expect(screen.getByText(/Already available \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("3.11%")).toBeInTheDocument();
  });

  it("says when nothing is missing", () => {
    render(<DocumentRequirements missing={missing({ missing: { CRITICAL: [], IMPORTANT: [], OPTIONAL: [] } })} uploads={[]} onUpload={onUpload} />);
    expect(screen.getByText("Nothing is missing")).toBeInTheDocument();
  });
});

describe("Upload processing status", () => {
  it("uses the document-workflow vocabulary", () => {
    expect(uploadStage(upload({ status: "RECEIVED" })).label).toBe("Uploaded");
    expect(uploadStage(upload({ status: "PROCESSING" })).label).toBe("Processing");
    expect(uploadStage(upload({ status: "REJECTED" })).label).toBe("Rejected");
    expect(uploadStage(upload({ status: "FAILED" })).label).toBe("Processing failed");
    expect(uploadStage(upload({ status: "NEEDS_CLARIFICATION" })).label).toBe("Needs your answer");
    expect(uploadStage(upload({ status: "ACCEPTED", resolved_requirements: ["nim"], conflicts: [] })).label).toBe("Validated");
    expect(uploadStage(upload({ status: "ACCEPTED", resolved_requirements: [] })).label).toBe("Extracted, nothing mapped");
    expect(uploadStage(upload({ status: "ACCEPTED", conflicts: [{ metric: "m", period_end: null, selected: "a", note: "n" }] })).label).toBe("Conflict detected");
    expect(uploadStage(upload({ status: "ACCEPTED", validation_status: "FAILED" })).label).toBe("Validation failed");
  });
});

describe("Readiness panel", () => {
  it("separates readiness from company quality and names the four questions", () => {
    render(<ReadinessPanel r={readiness()} />);
    expect(screen.getByText(/measures the evidence EquityOS holds, not how good the company is/i)).toBeInTheDocument();
    expect(screen.getByText("Four separate questions")).toBeInTheDocument();
    expect(screen.getByText("Assisted research")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Overall research readiness" })).toHaveAttribute("aria-valuenow", "66");
  });
});

const row = (over: Partial<PeerRow> = {}): PeerRow => ({
  key: "roe", label: "Return on equity", group: "Profitability", kind: "pct", better: "higher", basis: "CONSOLIDATED",
  company: { value: "12", unit: "percent", period_end: "2026-03-31", evidence_id: 1 },
  peers: [
    { name: "Alpha", comparable: true, value: "10" }, { name: "Beta", comparable: true, value: "14" },
    { name: "Gamma", comparable: true, value: "16" }, { name: "Delta", comparable: false, value: "9", reason: "different period" },
  ],
  n_comparable: 3, median: "14", rank: 3, of: 4, position: "worse", ...over,
});
const report = (rows: PeerRow[]): Report => ({
  id: "r", version: 1, mode: "COMPLETE", readiness_score: 80, missing_critical: 0, missing_important: 0, created_at: null, complete: true,
  content: {
    analysis: { available: true, peers: { peers: ["Alpha", "Beta", "Gamma", "Delta"].map((n, i) => ({ name: n, rank: i + 1, reason: "same industry" })), rows, unavailable_metrics: [], summary: { ranked_metrics: 1, better: [], worse: ["Return on equity"], in_line: [] }, rules: "a peer is compared only on the same period" } },
    meta: { banner: "", complete: true, company: "Own", as_of: "2026-11-11" }, readiness: { score: 80, eligibility: "AUTOMATIC", confidence: "MEDIUM", note: "" },
    sections: [], limitations: { missing_critical: [], missing_important: [], missing_optional: [], unavailable_calculations: [], incomplete_valuation: [], unavailable_scores: null, limits_on_conclusions: [] },
  },
});

describe("Peer comparison", () => {
  it("ranks only comparable peers, and shows others greyed with a reason instead of comparing them", () => {
    render(<PeersTab report={report([row()])} company="Own Co" />);
    expect(screen.getByText(/Worse than median · 3\/4/)).toBeInTheDocument();
    const grey = screen.getByTitle("Not compared: different period");
    expect(grey).toHaveTextContent("9.00%*");
    expect(screen.getAllByText("14.00%")).toHaveLength(2); // Beta and the peer median
  });

  it("highlights the best comparable value where a direction exists", () => {
    render(<PeersTab report={report([row()])} company="Own Co" />);
    expect(screen.getAllByText("16.00%")[0].className).toContain("text-ok");
  });

  it("does not rank with fewer than three comparable peers", () => {
    const r = row({ n_comparable: 2, median: undefined, rank: undefined, of: undefined, position: undefined, note: "2 comparable peer(s)" });
    render(<PeersTab report={report([r])} company="Own Co" />);
    expect(screen.getByText("not ranked")).toBeInTheDocument();
  });

  it("explains an unavailable comparison rather than inventing one", () => {
    render(<PeersTab report={null} company="Own Co" />);
    expect(screen.getByText("Peer comparison needs a report")).toBeInTheDocument();
  });
});
