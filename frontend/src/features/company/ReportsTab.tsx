"use client";

import { useState } from "react";

import ReportViewer from "@/features/report/ReportViewer";
import { Badge, Button, Card, CardHeader, EmptyState, Notice } from "@/components/ui";
import { dateShort } from "@/lib/format";
import { downloadReportPdf, type Report, type ReportRow } from "@/services/research";
import { explain, type ResearchState } from "@/features/research/useResearch";

/** Saves a stored report as its PDF. Errors are returned as text for the caller to show. */
export async function savePdf(report: { id: string; version: number }): Promise<string | null> {
  try {
    const blob = await downloadReportPdf(report.id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `equityos-report-v${report.version}.pdf`;
    a.click();
    URL.revokeObjectURL(url);
    return null;
  } catch (e) {
    return explain(e).message;
  }
}

export function ReportVersions({
  rows,
  current,
  onOpen,
}: {
  rows: ReportRow[];
  current: string | undefined;
  onOpen: (id: string) => void;
}) {
  return (
    <ul className="divide-y divide-line" aria-label="Report versions">
      {rows.map((r, i) => (
        <li key={r.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-[13px]">
          <span className="min-w-0">
            <span className="font-semibold text-ink">Version {r.version}</span>
            {i === 0 && <Badge tone="brand" className="ml-2">Latest</Badge>}
            <span className="block text-xs text-muted">
              {r.mode === "COMPLETE" ? "Complete report" : "Available-evidence report"} · readiness {r.readiness_score ?? "n/a"} · {r.missing_critical} critical gap{r.missing_critical === 1 ? "" : "s"} · {dateShort(r.created_at)}
            </span>
          </span>
          <Button size="sm" variant={current === r.id ? "primary" : "secondary"} aria-pressed={current === r.id} onClick={() => onOpen(r.id)}>
            {current === r.id ? "Viewing" : "View"}
          </Button>
        </li>
      ))}
    </ul>
  );
}

export default function ReportsTab({ rs }: { rs: ResearchState }) {
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [pdfBusy, setPdfBusy] = useState(false);
  const { session, data, report } = rs;
  const r = data?.readiness;

  if (!session) {
    return <EmptyState title="No research yet">Start research from the Overview tab. Reports appear here once research has run.</EmptyState>;
  }
  const download = async (rep: Report) => {
    setPdfBusy(true);
    setPdfError(await savePdf(rep));
    setPdfBusy(false);
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="Generate a report"
          subtitle="A complete report needs every critical requirement met. An available-evidence report discloses every gap and estimates nothing."
          actions={
            <>
              <Button variant="primary" loading={rs.busy} disabledReason={!r ? "Readiness has not loaded yet." : r.eligibility !== "AUTOMATIC" ? "A complete report needs every critical requirement met." : undefined} onClick={() => rs.generate("COMPLETE")}>
                Generate complete report
              </Button>
              <Button loading={rs.busy} disabledReason={!r ? "Readiness has not loaded yet." : r.eligibility === "BLOCKED" ? "No validated revenue or profit is held, so no report can be written." : undefined} onClick={() => rs.generate("AVAILABLE_EVIDENCE")}>
                Generate available-evidence report
              </Button>
            </>
          }
        />
        {data && data.reports.length > 0 ? (
          <ReportVersions rows={data.reports} current={report?.id} onOpen={rs.openReport} />
        ) : (
          <p className="px-5 py-4 text-[13px] text-muted">{data ? "No report has been generated for this session yet." : "Loading reports…"}</p>
        )}
      </Card>

      {pdfError && <Notice tone="bad" title="PDF download failed">{pdfError}</Notice>}

      {report ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-lg font-semibold text-ink">Report version {report.version}</h2>
            <Button variant="primary" loading={pdfBusy} onClick={() => download(report)}>Download PDF</Button>
          </div>
          <ReportViewer report={report} />
        </div>
      ) : (
        data && data.reports.length === 0 && <EmptyState title="No report to show yet">Generate one above. If the evidence is not enough, the Documents tab shows what to upload.</EmptyState>
      )}
    </div>
  );
}
