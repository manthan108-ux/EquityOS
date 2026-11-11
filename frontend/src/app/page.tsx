"use client";

import Link from "next/link";

import { Badge, ButtonLink, Card, CardHeader, EmptyState, ErrorState, Notice, Skeleton, Stat } from "@/components/ui";
import { useHealth } from "@/features/health/useHealth";
import { useApiKey } from "@/features/research/AccessKey";
import { useOverview, useSessions } from "@/features/research/hooks";
import CompanySearch from "@/features/search/CompanySearch";
import { dateShort } from "@/lib/format";
import { SESSION_LABEL } from "@/features/research/labels";

export default function Home() {
  const key = useApiKey();
  const health = useHealth();
  const sessions = useSessions();
  const overview = useOverview();
  const rows = sessions.data ?? [];
  const recent = [...new Map(rows.map((s) => [s.company_id, s])).values()].slice(0, 6);
  const count = (st: string) => rows.filter((s) => s.status === st).length;
  // Companies whose only stored evidence is a price are not counted as researchable.
  const ready = (overview.data?.companies ?? []).filter((r) => r.score >= 10);
  const readyCount = (e: string) => ready.filter((r) => r.eligibility === e).length;

  return (
    <div className="space-y-8">
      <section className="rounded-[16px] border border-line bg-surface px-6 py-10 text-center shadow-[var(--shadow-card)] sm:px-12">
        <p className="text-xs font-semibold tracking-widest text-brand uppercase">Evidence-driven equity research</p>
        <h1 className="mx-auto mt-2 max-w-2xl text-3xl font-semibold tracking-tight text-ink sm:text-4xl">
          Research any NSE or BSE listed company, backed by evidence you can trace
        </h1>
        <p className="mx-auto mt-3 max-w-xl text-[15px] text-muted">
          Every figure is validated and linked to its source. When evidence is missing, EquityOS tells you exactly which document to supply.
        </p>
        <div className="mx-auto mt-6 max-w-2xl text-left">
          <CompanySearch autoFocus />
        </div>
        <div className="mt-5 flex flex-wrap justify-center gap-3 text-sm">
          <ButtonLink href="/research" variant="secondary">Research workspace</ButtonLink>
          <ButtonLink href="/companies" variant="secondary">Company directory</ButtonLink>
          <ButtonLink href="/readiness" variant="secondary">Research readiness</ButtonLink>
        </div>
      </section>

      {!key && (
        <Notice tone="warn" title="Research access key needed">
          Company search works without it. To start research, view sessions or reports, set the key in <Link href="/settings" className="font-medium text-brand underline">Settings</Link>.
        </Notice>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Recently researched companies" actions={<ButtonLink href="/research" size="sm" variant="tertiary">All sessions</ButtonLink>} />
          {!key ? (
            <div className="p-5"><EmptyState title="Sign in with the access key">Your research sessions appear here.</EmptyState></div>
          ) : sessions.loading ? (
            <div className="space-y-3 p-5"><Skeleton className="h-6" /><Skeleton className="h-6" /><Skeleton className="h-6" /></div>
          ) : sessions.error ? (
            <div className="p-5"><ErrorState message={sessions.error.message} detail={sessions.error.detail} onRetry={sessions.reload} /></div>
          ) : recent.length === 0 ? (
            <div className="p-5"><EmptyState title="No research yet">Search for a company above and start your first research.</EmptyState></div>
          ) : (
            <ul className="divide-y divide-line">
              {recent.map((s) => (
                <li key={s.id}>
                  <Link href={`/companies/${s.company_id}`} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 hover:bg-surface-2">
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-semibold text-ink">{s.company ?? "Company"}</span>
                      <span className="text-xs text-muted">Updated {dateShort(s.updated_at)}{s.latest_report_version ? ` · report v${s.latest_report_version}` : ""}</span>
                    </span>
                    <Badge tone={SESSION_LABEL[s.status]?.tone ?? "neutral"} dot>{SESSION_LABEL[s.status]?.label ?? s.status}</Badge>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="space-y-6">
          <Card>
            <CardHeader title="Research activity" />
            <dl className="grid grid-cols-2 gap-5 p-5">
              <Stat label="Sessions" value={key ? rows.length : "–"} />
              <Stat label="Reports ready" value={key ? count("COMPLETED") : "–"} tone="ok" />
              <Stat label="Waiting for documents" value={key ? count("AWAITING_DOCUMENTS") : "–"} tone={count("AWAITING_DOCUMENTS") ? "warn" : undefined} />
              <Stat label="Needs retry" value={key ? count("FAILED") : "–"} tone={count("FAILED") ? "bad" : undefined} />
            </dl>
          </Card>
          <Card>
            <CardHeader title="Readiness summary" actions={<ButtonLink href="/readiness" size="sm" variant="tertiary">Open</ButtonLink>} />
            <dl className="grid grid-cols-3 gap-4 p-5">
              <Stat label="Automatic" value={overview.data ? readyCount("AUTOMATIC") : "–"} tone="ok" />
              <Stat label="Assisted" value={overview.data ? readyCount("ASSISTED") : "–"} tone="warn" />
              <Stat label="Blocked" value={overview.data ? readyCount("BLOCKED") : "–"} />
            </dl>
            <p className="border-t border-line px-5 py-3 text-xs text-muted">Companies with stored evidence. Readiness measures the evidence, not the company.</p>
          </Card>
          <Card>
            <CardHeader title="System status" />
            <ul className="divide-y divide-line text-[13px]">
              {[["Backend API", health.phase === "ok" || health.phase === "degraded" ? `${health.live.service} v${health.live.version}` : health.phase === "loading" ? "Checking…" : "Unreachable", health.phase === "ok" || health.phase === "degraded"], ["Database", health.phase === "loading" ? "Checking…" : health.phase === "ok" ? "Connected" : "Unavailable", health.phase === "ok"]].map(([k, v, ok]) => (
                <li key={k as string} className="flex items-center justify-between px-5 py-3"><span className="text-muted">{k}</span><span className="flex items-center gap-2 font-medium text-ink"><span aria-hidden className={`h-2 w-2 rounded-full ${health.phase === "loading" ? "bg-faint" : ok ? "bg-ok" : "bg-bad"}`} />{v}</span></li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </div>
  );
}
