"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, Card, CardHeader, EmptyState, ErrorState, Notice, PageHeader, Skeleton, inputClass } from "@/components/ui";
import { ELIGIBILITY } from "@/features/company/ReadinessPanel";
import { useApiKey } from "@/features/research/AccessKey";
import { useOverview } from "@/features/research/hooks";
import CompanySearch from "@/features/search/CompanySearch";

export default function CompaniesPage() {
  const key = useApiKey();
  const ov = useOverview();
  const [filter, setFilter] = useState("");
  const [profile, setProfile] = useState("All");
  // Companies whose only stored evidence is a price are left out of the directory.
  const rows = useMemo(() => (ov.data?.companies ?? []).filter((r) => r.score >= 10), [ov.data]);
  const profiles = useMemo(() => ["All", ...new Set(rows.map((r) => r.profile))], [rows]);
  const shown = rows.filter(
    (r) => (profile === "All" || r.profile === profile) && (!filter || `${r.name} ${r.symbol}`.toLowerCase().includes(filter.toLowerCase())),
  );

  return (
    <div className="space-y-8">
      <PageHeader
        title="Companies"
        subtitle="Search any NSE or BSE listed company, or browse the companies EquityOS already holds evidence for. Selecting a company opens its own research workspace."
      />
      <Card className="p-5">
        <CompanySearch />
      </Card>

      <Card>
        <CardHeader
          title="Companies with research evidence"
          subtitle="Browse by research profile (sector group). Companies outside this list can still be researched through search."
        />
        {!key ? (
          <div className="p-5">
            <Notice tone="warn">Set the research access key in Settings to browse the directory.</Notice>
          </div>
        ) : ov.loading ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-8" />
            <Skeleton className="h-8" />
            <Skeleton className="h-8" />
          </div>
        ) : ov.error ? (
          <div className="p-5">
            <ErrorState message={ov.error.message} detail={ov.error.detail} onRetry={ov.reload} />
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
              <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by name or ticker" aria-label="Filter companies" className={`${inputClass} max-w-xs`} />
              <div role="group" aria-label="Research profile" className="flex flex-wrap gap-1">
                {profiles.map((p) => (
                  <button
                    key={p}
                    type="button"
                    aria-pressed={profile === p}
                    onClick={() => setProfile(p)}
                    className={`rounded-full border px-3 py-1 text-xs font-medium ${profile === p ? "border-brand bg-brand-soft text-brand" : "border-line-strong text-muted hover:bg-surface-2"}`}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
            {shown.length === 0 ? (
              <div className="p-5">
                <EmptyState title="No companies match">Try a different filter, or search above.</EmptyState>
              </div>
            ) : (
              <ul className="grid gap-px bg-line sm:grid-cols-2 xl:grid-cols-3">
                {shown.map((r) => (
                  <li key={r.company_id} className="bg-surface">
                    <Link href={`/companies/${r.company_id}`} className="block px-5 py-4 hover:bg-surface-2">
                      <span className="flex items-start justify-between gap-2">
                        <span className="min-w-0">
                          <span className="block truncate text-sm font-semibold text-ink">{r.name}</span>
                          <span className="tnum text-xs text-muted">
                            {r.symbol ?? "—"} · {r.profile}
                          </span>
                        </span>
                        <Badge tone={ELIGIBILITY[r.eligibility].tone}>{r.score}</Badge>
                      </span>
                      <span className="mt-2 block text-xs text-muted">
                        {ELIGIBILITY[r.eligibility].label}
                        {r.critical_missing.length ? ` · ${r.critical_missing.length} critical missing` : ""}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
