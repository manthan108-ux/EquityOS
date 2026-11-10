import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { report } from "@/features/research/fixtures";
import ReportView from "@/features/research/ReportView";

describe("ReportView", () => {
  it("says plainly that a partial report is not complete", () => {
    render(<ReportView report={report()} />);
    expect(screen.getByText(/available-evidence report v1/i)).toBeInTheDocument();
    expect(screen.getByText(/2 critical and 0 important item\(s\) are not available/i)).toBeInTheDocument();
    expect(screen.queryByText(/^complete research report/i)).not.toBeInTheDocument();
  });

  it("prints each gap in the section it affects, marked as not available", () => {
    render(<ReportView report={report()} />);
    const section = screen.getByRole("heading", { name: /sector-specific analysis/i }).closest("section")!;
    expect(within(section).getByText(/interest earned/i)).toBeInTheDocument();
    expect(within(section).getByText(/critical · not available/i)).toBeInTheDocument();
    expect(within(section).getByText(/no validated value is stored/i)).toBeInTheDocument();
  });

  it("shows the value with its period, dimension and evidence citation", () => {
    render(<ReportView report={report()} />);
    expect(screen.getByText("3.11%")).toBeInTheDocument();
    expect(screen.getByText(/2026-09-30/)).toBeInTheDocument();
    expect(screen.getByText(/\(domestic\)/)).toBeInTheDocument();
    expect(screen.getByText(/p\.2 · evidence #42/)).toBeInTheDocument();
  });

  it("states the basis and period the report was made under", () => {
    render(<ReportView report={report()} />);
    expect(screen.getByText(/basis: consolidated; period: latest available quarter/i)).toBeInTheDocument();
  });

  it("lists the limitations and the limits on conclusions", () => {
    render(<ReportView report={report()} />);
    const lim = screen.getByRole("heading", { name: "Limitations" }).closest("section")!;
    expect(within(lim).getByText(/critical items not available/i)).toBeInTheDocument();
    expect(within(lim).getByText(/no conclusion is drawn on interest earned/i)).toBeInTheDocument();
  });

  it("never prints a missing value as zero or as a placeholder", () => {
    const r = report();
    r.content.sections[0].items[0] = { ...r.content.sections[0].items[0], value: null };
    render(<ReportView report={r} />);
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(screen.queryByText(/undefined|null|NaN/)).not.toBeInTheDocument();
  });

  it("marks a complete report as complete", () => {
    const r = report({ mode: "COMPLETE", complete: true });
    r.content.meta = { ...r.content.meta, complete: true, banner: "Complete research report: every critical requirement is met by validated evidence." };
    render(<ReportView report={r} />);
    expect(screen.getByText(/complete research report v1/i)).toBeInTheDocument();
  });
});
