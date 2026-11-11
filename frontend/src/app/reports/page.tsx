"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, Button, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Skeleton } from "@/components/ui";
import { savePdf } from "@/features/company/ReportsTab";
import { useApiKey } from "@/features/research/AccessKey";
import { useAllReports, useSessions } from "@/features/research/hooks";
import { dateShort } from "@/lib/format";

export default function ReportsPage() {
  const key = useApiKey();
  const reports = useAllReports();
  const sessions = useSessions();
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const names = useMemo(() => new Map((sessions.data ?? []).map((s) => [s.company_id, s.company ?? "Company"])), [sessions.data]);
  const reportRows = reports.data;
  const byCompany = useMemo(() => {
    const m = new Map<string, NonNullable<typeof reportRows>>();
    for (const r of reportRows ?? []) m.set(r.company_id, [...(m.get(r.company_id) ?? []), r]);
    return [...m.entries()];
  }, [reportRows]);

  return (
    <div className="space-y-6">
      <PageHeader title="Reports" subtitle="Every report version generated in your research sessions. Earlier versions are kept; open any of them, or download its PDF." />
      {!key ? (
        <Notice tone="warn" title="Access key needed">Set the research access key in <Link href="/settings" className="font-medium text-brand underline">Settings</Link>.</Notice>
      ) : reports.error ? (
        <ErrorState message={reports.error.message} detail={reports.error.detail} onRetry={reports.reload} />
      ) : reports.loading ? (
        <div className="space-y-3"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
      ) : byCompany.length === 0 ? (
        <EmptyState title="No reports yet" action={<ButtonLink href="/research" variant="primary">Research workspace</ButtonLink>}>Reports appear after research has produced one.</EmptyState>
      ) : (
        <>
          {pdfError && <Notice tone="bad" title="PDF download failed">{pdfError}</Notice>}
          {byCompany.map(([companyId, list]) => (
            <Card key={companyId}>
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
                <Link href={`/companies/${companyId}?tab=reports`} className="text-[15px] font-semibold text-ink hover:text-brand">{names.get(companyId) ?? "Company"}</Link>
                <span className="text-xs text-muted">{list.length} version{list.length === 1 ? "" : "s"}</span>
              </div>
              <ul className="divide-y divide-line">
                {list.map((r, i) => (
                  <li key={r.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-[13px]">
                    <span className="min-w-0">
                      <span className="font-semibold text-ink">Version {r.version}</span>
                      {i === 0 && <Badge tone="brand" className="ml-2">Latest</Badge>}
                      <Badge tone={r.complete ? "ok" : "warn"} className="ml-2">{r.complete ? "Complete" : "Available evidence"}</Badge>
                      <span className="block text-xs text-muted">Readiness {r.readiness_score ?? "n/a"} · {r.missing_critical} critical gap{r.missing_critical === 1 ? "" : "s"} · {dateShort(r.created_at)}</span>
                    </span>
                    <span className="flex gap-2">
                      <ButtonLink size="sm" href={`/companies/${companyId}?tab=reports&session=${r.session_id}`}>Open</ButtonLink>
                      <Button size="sm" loading={busy === r.id} onClick={async () => { setBusy(r.id); setPdfError(await savePdf(r)); setBusy(null); }}>Download PDF</Button>
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          ))}
          <p className="text-xs text-muted">Report comparison across versions is not available yet.</p>
        </>
      )}
    </div>
  );
}
