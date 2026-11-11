"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badge, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Skeleton } from "@/components/ui";
import { uploadStage } from "@/features/company/Uploader";
import { useApiKey } from "@/features/research/AccessKey";
import { useSessions } from "@/features/research/hooks";
import { explain, type UiError } from "@/features/research/useResearch";
import { dateShort, titleCase } from "@/lib/format";
import { fetchUploads, type SessionRow, type Upload } from "@/services/research";

type Group = { session: SessionRow; uploads: Upload[] };
const MAX_COMPANIES = 15;

export default function DocumentsPage() {
  const key = useApiKey();
  const sessions = useSessions();
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [error, setError] = useState<UiError | null>(null);

  useEffect(() => {
    if (!sessions.data) return;
    // The newest session of each company; its uploads are that company's document library.
    const latest = [...new Map(sessions.data.map((s) => [s.company_id, s])).values()].slice(0, MAX_COMPANIES);
    let live = true;
    Promise.all(latest.map((s) => fetchUploads(s.id).then((uploads) => ({ session: s, uploads }))))
      .then((g) => live && setGroups(g))
      .catch((e) => live && setError(explain(e)));
    return () => {
      live = false;
    };
  }, [sessions.data]);

  const total = groups?.reduce((n, g) => n + g.uploads.length, 0) ?? 0;
  const loading = sessions.loading || (!groups && !error && !!sessions.data);

  return (
    <div className="space-y-6">
      <PageHeader title="Documents" subtitle="Documents you have uploaded, per company, with their processing and validation status. What a company still needs is on its Documents tab." />
      {!key ? (
        <Notice tone="warn" title="Access key needed">Set the research access key in <Link href="/settings" className="font-medium text-brand underline">Settings</Link>.</Notice>
      ) : sessions.error || error ? (
        <ErrorState message={(sessions.error ?? error)!.message} detail={(sessions.error ?? error)!.detail} onRetry={sessions.reload} />
      ) : loading ? (
        <div className="space-y-3"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
      ) : !groups || groups.length === 0 ? (
        <EmptyState title="No documents yet" action={<ButtonLink href="/" variant="primary">Find a company</ButtonLink>}>Upload documents from a company&apos;s Documents tab when research asks for them.</EmptyState>
      ) : (
        <>
          <p className="text-sm text-muted">{total} uploaded document{total === 1 ? "" : "s"} across {groups.length} compan{groups.length === 1 ? "y" : "ies"}.</p>
          {groups.map(({ session: s, uploads }) => (
            <Card key={s.id}>
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
                <Link href={`/companies/${s.company_id}?tab=documents&session=${s.id}`} className="text-[15px] font-semibold text-ink hover:text-brand">{s.company ?? "Company"}</Link>
                <ButtonLink size="sm" href={`/companies/${s.company_id}?tab=documents&session=${s.id}`}>Open documents</ButtonLink>
              </div>
              {uploads.length === 0 ? (
                <p className="px-5 py-4 text-[13px] text-muted">Nothing uploaded in this company&apos;s latest session. Documents EquityOS fetched itself are listed under “Documents EquityOS already holds” on its Documents tab.</p>
              ) : (
                <ul className="divide-y divide-line">
                  {uploads.map((u) => {
                    const st = uploadStage(u);
                    return (
                      <li key={u.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-[13px]">
                        <span className="min-w-0"><span className="block truncate font-medium text-ink">{u.filename ?? "file"}</span><span className="text-xs text-muted">{[u.category && titleCase(u.category), u.fiscal_year, u.basis?.toLowerCase(), dateShort(u.uploaded_at)].filter(Boolean).join(" · ")}</span></span>
                        <Badge tone={st.tone} dot>{st.label}</Badge>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Card>
          ))}
          {sessions.data && new Set(sessions.data.map((x) => x.company_id)).size > MAX_COMPANIES && <p className="text-xs text-muted">Showing the {MAX_COMPANIES} most recently researched companies.</p>}
        </>
      )}
    </div>
  );
}
