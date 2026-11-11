"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { Badge, Card, EmptyState, ErrorState, InfoTip, Notice, PageHeader, ProgressBar, Skeleton, Stat, inputClass } from "@/components/ui";
import { ELIGIBILITY } from "@/features/company/ReadinessPanel";
import { useApiKey } from "@/features/research/AccessKey";
import { useOverview } from "@/features/research/hooks";
import type { OverviewRow } from "@/services/research";

type SortKey = "name" | "score" | "confidence" | "critical";
const CONF: Record<string, number> = { HIGH: 3, MEDIUM: 2, LOW: 1, NONE: 0 };

export default function ReadinessPage() {
  const key = useApiKey();
  const { data, loading, error, reload } = useOverview();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<"ALL" | OverviewRow["eligibility"]>("ALL");
  const [profile, setProfile] = useState("All");
  const [withPriceOnly, setWithPriceOnly] = useState(false);
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "score", dir: -1 });

  const all = useMemo(() => data?.companies ?? [], [data]);
  const profiles = useMemo(() => ["All", ...new Set(all.map((r) => r.profile))], [all]);
  const rows = useMemo(() => {
    const f = all.filter(
      (r) =>
        (withPriceOnly || r.score >= 10) &&
        (status === "ALL" || r.eligibility === status) &&
        (profile === "All" || r.profile === profile) &&
        (!q || `${r.name} ${r.symbol}`.toLowerCase().includes(q.toLowerCase())),
    );
    const val = (r: OverviewRow) => (sort.key === "name" ? r.name.toLowerCase() : sort.key === "score" ? r.score : sort.key === "confidence" ? (CONF[r.confidence] ?? 0) : r.critical_missing.length);
    return [...f].sort((a, b) => (val(a) < val(b) ? -sort.dir : val(a) > val(b) ? sort.dir : 0));
  }, [all, q, status, profile, withPriceOnly, sort]);

  const th = (k: SortKey, label: string, right = false) => (
    <th scope="col" aria-sort={sort.key === k ? (sort.dir === 1 ? "ascending" : "descending") : "none"} className={`px-3 py-2 font-medium ${right ? "text-right" : "text-left"}`}>
      <button type="button" onClick={() => setSort((s) => ({ key: k, dir: s.key === k ? (s.dir === 1 ? -1 : 1) : k === "name" ? 1 : -1 }))} className="inline-flex items-center gap-1 hover:text-ink">
        {label}
        <span aria-hidden className="text-[10px]">{sort.key === k ? (sort.dir === 1 ? "▲" : "▼") : ""}</span>
      </button>
    </th>
  );

  const count = (e: OverviewRow["eligibility"]) => rows.filter((r) => r.eligibility === e).length;

  return (
    <div className="space-y-6">
      <PageHeader title="Research readiness" subtitle="How much validated evidence EquityOS holds for each company, and what to supply next. Readiness measures the evidence, not the company's quality or attractiveness." />
      {!key ? (
        <Notice tone="warn" title="Access key needed">Set the research access key in <Link href="/settings" className="font-medium text-brand underline">Settings</Link> to see readiness.</Notice>
      ) : error ? (
        <ErrorState message={error.message} detail={error.detail} onRetry={reload} />
      ) : (
        <>
          <Card>
            <dl className="grid grid-cols-2 gap-5 p-5 sm:grid-cols-4">
              <Stat label="Companies shown" value={rows.length} />
              <Stat label="Automatic" value={count("AUTOMATIC")} tone="ok" tip={ELIGIBILITY.AUTOMATIC.help} />
              <Stat label="Assisted" value={count("ASSISTED")} tone="warn" tip={ELIGIBILITY.ASSISTED.help} />
              <Stat label="Blocked" value={count("BLOCKED")} tone={count("BLOCKED") ? "bad" : undefined} tip={ELIGIBILITY.BLOCKED.help} />
            </dl>
          </Card>

          <Card>
            <div className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-3">
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name or ticker" aria-label="Search companies" className={`${inputClass} max-w-xs`} />
              <select value={status} onChange={(e) => setStatus(e.target.value as typeof status)} aria-label="Research status" className={`${inputClass} w-44`}>
                <option value="ALL">All research statuses</option>
                <option value="AUTOMATIC">Automatic</option>
                <option value="ASSISTED">Assisted</option>
                <option value="BLOCKED">Blocked</option>
              </select>
              <select value={profile} onChange={(e) => setProfile(e.target.value)} aria-label="Sector profile" className={`${inputClass} w-52`}>
                {profiles.map((p) => <option key={p} value={p}>{p === "All" ? "All sectors" : p}</option>)}
              </select>
              <label className="flex items-center gap-2 text-xs text-muted">
                <input type="checkbox" className="accent-[var(--color-brand)]" checked={withPriceOnly} onChange={(e) => setWithPriceOnly(e.target.checked)} />
                Include companies with only price data
              </label>
            </div>
            {loading && <div className="space-y-2 p-5"><Skeleton className="h-9" /><Skeleton className="h-9" /><Skeleton className="h-9" /></div>}
            {!loading && rows.length === 0 && <div className="p-5"><EmptyState title="No companies match these filters" /></div>}
            {rows.length > 0 && (
              <div className="max-h-[70vh] overflow-auto">
                <table className="w-full min-w-[900px] text-[13px]">
                  <thead className="sticky top-0 z-10 bg-surface-2 text-xs text-muted">
                    <tr>
                      {th("name", "Company")}
                      <th className="px-3 py-2 text-left font-medium">Sector</th>
                      {th("score", "Readiness", true)}
                      <th className="px-3 py-2 text-left font-medium">Status</th>
                      {th("confidence", "Confidence")}
                      {th("critical", "Critical missing", true)}
                      <th className="px-3 py-2 text-left font-medium"><span className="inline-flex items-center gap-1">Documents needed <InfoTip text="Documents that would settle the critical or important gaps." /></span></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {rows.map((r) => (
                      <tr key={r.company_id} className="align-top hover:bg-surface-2/60">
                        <td className="px-3 py-2.5"><Link href={`/companies/${r.company_id}?tab=readiness`} className="font-semibold text-ink hover:text-brand">{r.name}</Link><span className="tnum block text-xs text-muted">{r.symbol ?? "—"}</span></td>
                        <td className="px-3 py-2.5 text-muted">{r.profile}</td>
                        <td className="w-40 px-3 py-2.5"><div className="tnum text-right font-semibold text-ink">{r.score}</div><ProgressBar value={r.score} label={`${r.name} readiness`} /></td>
                        <td className="px-3 py-2.5"><Badge tone={ELIGIBILITY[r.eligibility].tone} dot>{ELIGIBILITY[r.eligibility].label}</Badge></td>
                        <td className="px-3 py-2.5 text-muted">{r.confidence.charAt(0) + r.confidence.slice(1).toLowerCase()}</td>
                        <td className="px-3 py-2.5 text-right">
                          {r.critical_missing.length ? <span title={r.critical_missing.map((m) => m.label).join(", ")} className="tnum font-semibold text-bad">{r.critical_missing.length}</span> : <span className="text-ok">0</span>}
                          {r.critical_missing.length > 0 && <span className="block max-w-[220px] truncate text-xs text-muted">{r.critical_missing.map((m) => m.label).join(", ")}</span>}
                        </td>
                        <td className="max-w-[260px] px-3 py-2.5 text-xs text-muted">{r.documents_needed.length ? r.documents_needed.join("; ") : r.cannot_be_settled_by_upload.length ? "Not settled by upload" : "None"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="border-t border-line px-5 py-3 text-xs text-muted">A last-updated date is not provided by the readiness overview, so none is shown.</p>
          </Card>
        </>
      )}
    </div>
  );
}
