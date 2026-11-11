"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, ButtonLink, Card, EmptyState, ErrorState, Notice, PageHeader, Skeleton, Tabs } from "@/components/ui";
import { useApiKey } from "@/features/research/AccessKey";
import { useSessions } from "@/features/research/hooks";
import { SESSION_LABEL } from "@/features/research/labels";
import { dateShort } from "@/lib/format";
import type { SessionRow } from "@/services/research";

type Filter = "all" | "active" | "waiting" | "completed" | "failed";
const MATCH: Record<Filter, (s: SessionRow) => boolean> = {
  all: () => true,
  active: (s) => s.status === "RUNNING" || s.status === "CREATED" || s.status === "READY",
  waiting: (s) => s.status === "AWAITING_DOCUMENTS",
  completed: (s) => s.status === "COMPLETED",
  failed: (s) => s.status === "FAILED",
};

function Row({ s, latest }: { s: SessionRow; latest: boolean }) {
  const st = SESSION_LABEL[s.status];
  const base = `/companies/${s.company_id}?session=${s.id}`;
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-[13px]">
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-2">
          <Badge tone={st?.tone ?? "neutral"} dot>
            {st?.label ?? s.status}
          </Badge>
          {latest && <Badge tone="brand">Latest</Badge>}
        </span>
        <span className="mt-1 block text-xs text-muted">
          Updated {dateShort(s.updated_at)} · readiness {s.readiness_score ?? "n/a"} · {s.eligibility ? s.eligibility.toLowerCase() : "not evaluated"}
          {s.latest_report_version ? ` · report v${s.latest_report_version}` : " · no report"}
        </span>
      </span>
      <span className="flex flex-wrap gap-2">
        <ButtonLink size="sm" href={`${base}&tab=overview`}>
          Open
        </ButtonLink>
        {(s.status === "AWAITING_DOCUMENTS" || s.status === "FAILED") && (
          <ButtonLink size="sm" variant="primary" href={`${base}&tab=${s.status === "FAILED" ? "overview" : "documents"}`}>
            Resume
          </ButtonLink>
        )}
        {s.latest_report_version && (
          <ButtonLink size="sm" href={`${base}&tab=reports`}>
            View report
          </ButtonLink>
        )}
      </span>
    </li>
  );
}

export default function ResearchPage() {
  const key = useApiKey();
  const { data, loading, error, reload } = useSessions();
  const [filter, setFilter] = useState<Filter>("all");

  const groups = useMemo(() => {
    const m = new Map<string, SessionRow[]>();
    for (const s of data ?? []) if (MATCH[filter](s)) m.set(s.company_id, [...(m.get(s.company_id) ?? []), s]);
    return [...m.entries()];
  }, [data, filter]);
  const n = (f: Filter) => (data ?? []).filter(MATCH[f]).length;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Research workspace"
        subtitle="Your research sessions, grouped by company. Open one to continue, resume a saved session, or read its report. Earlier sessions stay available under History."
      />
      {!key ? (
        <Notice tone="warn" title="Access key needed">
          Set the research access key in{" "}
          <Link href="/settings" className="font-medium text-brand underline">
            Settings
          </Link>{" "}
          to see your sessions.
        </Notice>
      ) : (
        <>
          <Tabs<Filter>
            label="Session status"
            value={filter}
            onChange={setFilter}
            tabs={[
              { id: "all", label: `All (${n("all")})` },
              { id: "active", label: `Active (${n("active")})` },
              { id: "waiting", label: `Waiting for documents (${n("waiting")})` },
              { id: "completed", label: `Completed (${n("completed")})` },
              { id: "failed", label: `Failed (${n("failed")})` },
            ]}
          />
          {loading && (
            <div className="space-y-3 pt-4">
              <Skeleton className="h-20" />
              <Skeleton className="h-20" />
            </div>
          )}
          {error && <ErrorState message={error.message} detail={error.detail} onRetry={reload} />}
          {!loading && !error && groups.length === 0 && (
            <EmptyState title="No sessions here" action={<ButtonLink href="/" variant="primary">Search for a company</ButtonLink>}>
              Start research from a company page and it will appear in this list.
            </EmptyState>
          )}
          <div className="space-y-4 pt-2">
            {groups.map(([companyId, list]) => (
              <Card key={companyId}>
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-3">
                  <Link href={`/companies/${companyId}`} className="text-[15px] font-semibold text-ink hover:text-brand">
                    {list[0].company ?? "Company"}
                  </Link>
                  <span className="text-xs text-muted">
                    {list.length} session{list.length === 1 ? "" : "s"}
                  </span>
                </div>
                <ul className="divide-y divide-line">
                  <Row s={list[0]} latest />
                </ul>
                {list.length > 1 && (
                  <details className="border-t border-line">
                    <summary className="cursor-pointer px-5 py-2.5 text-xs font-medium text-brand">History ({list.length - 1} earlier)</summary>
                    <ul className="divide-y divide-line border-t border-line">
                      {list.slice(1).map((s) => (
                        <Row key={s.id} s={s} latest={false} />
                      ))}
                    </ul>
                  </details>
                )}
              </Card>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
