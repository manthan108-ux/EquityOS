"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { Badge, Button, Card, EmptyState, ErrorState, Notice, SkeletonCard, TabPanel, Tabs } from "@/components/ui";
import ScoreCard from "@/features/scoring/ScoreCard";
import { fmtValue, titleCase } from "@/lib/format";
import { fetchCompany, type CompanySummary } from "@/services/api";
import type { Requirement } from "@/services/research";
import { useResearch } from "@/features/research/useResearch";
import DocumentRequirements, { docAnchor } from "./DocumentRequirements";
import FinancialsTab from "./FinancialsTab";
import OverviewTab from "./OverviewTab";
import PeersTab from "./PeersTab";
import ReadinessPanel from "./ReadinessPanel";
import ReportsTab, { savePdf } from "./ReportsTab";
import Uploader, { type UploaderHandle } from "./Uploader";

export type TabId = "overview" | "financials" | "peers" | "scorecard" | "readiness" | "documents" | "reports";
const TAB_IDS: TabId[] = ["overview", "financials", "peers", "scorecard", "readiness", "documents", "reports"];

function Ident({ k, v }: { k: string; v: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium tracking-wide text-muted uppercase">{k}</dt>
      <dd className="tnum truncate text-sm font-medium text-ink" title={v}>{v}</dd>
    </div>
  );
}

export default function CompanyWorkspace({ companyId }: { companyId: string }) {
  const router = useRouter();
  const path = usePathname();
  const params = useSearchParams();
  const tabParam = params.get("tab") as TabId | null;
  const tab: TabId = tabParam && TAB_IDS.includes(tabParam) ? tabParam : "overview";

  const [company, setCompany] = useState<CompanySummary | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reqFocus, setReqFocus] = useState<Requirement | null>(null);
  const [pdfNote, setPdfNote] = useState<string | null>(null);
  const uploader = useRef<UploaderHandle>(null);
  const rs = useResearch(companyId, params.get("session"));

  useEffect(() => {
    const c = new AbortController();
    fetchCompany(companyId, c.signal)
      .then(setCompany)
      .catch((e: unknown) => !c.signal.aborted && setLoadError(e instanceof Error ? e.message : "Unknown error"));
    return () => c.abort();
  }, [companyId]);

  const go = useCallback(
    (t: TabId) => {
      const q = new URLSearchParams(params.toString());
      q.set("tab", t);
      router.replace(`${path}?${q.toString()}`, { scroll: false });
    },
    [params, path, router],
  );

  const toRequirement = (m: Requirement) => {
    go("documents");
    setTimeout(() => document.getElementById(docAnchor(m))?.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
  };
  const toUploader = (m?: Requirement) => {
    setReqFocus(m ?? null);
    go("documents");
    setTimeout(() => uploader.current?.focus(), 120);
  };

  if (loadError) {
    return <ErrorState title="Company not found" message="This company could not be loaded. It may not exist in the master data." detail={loadError} />;
  }
  if (!company) return <div className="space-y-4"><SkeletonCard lines={2} /><SkeletonCard lines={5} /></div>;

  const nse = company.listings.find((l) => l.exchange === "NSE");
  const bse = company.listings.find((l) => l.exchange === "BSE");
  const isin = [...new Set(company.listings.map((l) => l.isin))].join(", ");
  const analysis = rs.report?.content.analysis;
  const valuation = analysis?.valuation as { snapshot?: { price?: { value: string; unit: string | null; period_end: string } | null } } | undefined;
  const price = valuation?.snapshot?.price;
  const mcap = rs.report?.content.sections.find((s) => s.key === "valuation")?.items.find((i) => i.requirement === "market_cap");
  const hasReports = !!rs.data?.reports.length;
  const hasPeers = !!analysis?.available && !!analysis.peers?.peers.length;
  const session = rs.session;
  const resumable = session && !rs.running && (session.status === "AWAITING_DOCUMENTS" || session.status === "FAILED" || session.status === "READY");
  const crit = rs.data?.readiness.counts.critical_missing ?? 0;

  const tabs = [
    { id: "overview" as const, label: "Overview" },
    { id: "financials" as const, label: "Financials" },
    { id: "peers" as const, label: "Peer comparison" },
    { id: "scorecard" as const, label: "Scorecard" },
    { id: "readiness" as const, label: "Research readiness", badge: crit ? <Badge tone="bad">{crit}</Badge> : undefined },
    { id: "documents" as const, label: "Documents", badge: crit ? <Badge tone="bad">{crit} needed</Badge> : undefined },
    { id: "reports" as const, label: "Reports", badge: hasReports ? <Badge tone="neutral">{rs.data!.reports.length}</Badge> : undefined },
  ];

  return (
    <div>
      <nav aria-label="Breadcrumb" className="mb-3 text-xs text-muted">
        <Link href="/companies" className="hover:text-brand">Companies</Link> <span aria-hidden>/</span> <span className="text-ink">{company.common_name}</span>
      </nav>

      <Card as="article" aria-label="Company" className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="text-2xl font-semibold tracking-tight text-ink">{company.common_name}</h1>
            <p className="mt-0.5 text-sm text-muted">{company.legal_name}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {!session && rs.key && <Button variant="primary" loading={rs.busy} onClick={() => rs.start()}>Start research</Button>}
            {resumable && <Button variant="primary" loading={rs.busy} onClick={() => rs.resume()}>Resume research</Button>}
            <Button disabledReason={hasReports ? undefined : "No report has been generated yet."} onClick={() => go("reports")}>View report</Button>
            <Button onClick={() => go("documents")}>View documents</Button>
            <Button disabledReason={hasPeers ? undefined : "Peer comparison appears once a report with peer analysis exists."} onClick={() => go("peers")}>Compare peers</Button>
            {rs.data?.reports[0] && <Button variant="tertiary" onClick={async () => setPdfNote(await savePdf(rs.data!.reports[0]))}>Download PDF</Button>}
          </div>
        </div>
        <dl className="mt-5 grid grid-cols-2 gap-x-6 gap-y-3 border-t border-line pt-4 sm:grid-cols-4 lg:grid-cols-7">
          <Ident k="NSE" v={nse?.symbol ?? "Not listed"} />
          <Ident k="BSE code" v={bse?.security_code ?? "Not listed"} />
          <Ident k="ISIN" v={isin || "—"} />
          <Ident k="Sector" v={titleCase(company.sector) || "Not classified"} />
          <Ident k="Industry" v={titleCase(company.industry) || "Not classified"} />
          <Ident k="Share price" v={price ? `${fmtValue(price.value, price.unit)} · ${price.period_end}` : "Not loaded"} />
          <Ident k="Market cap" v={mcap ? fmtValue(mcap.value, mcap.unit) : "Not loaded"} />
        </dl>
      </Card>
      {pdfNote && <div className="mt-3"><Notice tone="bad" title="PDF download failed">{pdfNote}</Notice></div>}

      {rs.error && (
        <div className="mt-4"><ErrorState message={rs.error.message} detail={rs.error.detail} onRetry={rs.clearError} /></div>
      )}

      <div className="mt-6">
        <Tabs tabs={tabs} value={tab} onChange={go} label="Company research sections" />
        <TabPanel id={tab}>
          {tab === "overview" && <OverviewTab rs={rs} go={go} />}
          {tab === "financials" && <FinancialsTab report={rs.report} resolved={rs.data?.missing.resolved} onGoToReports={() => go("reports")} />}
          {tab === "peers" && <PeersTab report={rs.report} company={company.common_name} />}
          {tab === "scorecard" && <ScoreCard companyId={companyId} ticker={nse?.symbol} />}
          {tab === "readiness" && (
            rs.data ? (
              <div className="space-y-6">
                <MissingSummary rs={rs} onJump={toRequirement} onDocs={() => go("documents")} />
                <ReadinessPanel r={rs.data.readiness} />
              </div>
            ) : <NoSession rs={rs} />
          )}
          {tab === "documents" && (
            rs.data && rs.session ? (
              <div className="space-y-6">
                <DocumentRequirements missing={rs.data.missing} uploads={rs.data.uploads} onUpload={toUploader} />
                <Uploader ref={uploader} uploads={rs.data.uploads} busy={rs.busy} error={rs.error?.message ?? null} onUpload={(f, l) => rs.upload(f, l)} onChange={rs.replaceUpload} forRequirement={reqFocus} onClearRequirement={() => setReqFocus(null)} />
                {session && resumable && (
                  <Card className="p-5">
                    <p className="text-sm font-semibold text-ink">After uploading</p>
                    <p className="mt-0.5 text-[13px] text-muted">Once your documents show as validated, resume research so EquityOS recalculates readiness and the report.</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button variant="primary" loading={rs.busy} onClick={() => rs.resume()}>Resume research</Button>
                      <Button loading={rs.busy} disabledReason={rs.data.missing.options.continue_with_available ? undefined : "Not available for this company."} onClick={() => rs.continueAvailable()}>Continue with what is available</Button>
                    </div>
                  </Card>
                )}
              </div>
            ) : <NoSession rs={rs} />
          )}
          {tab === "reports" && <ReportsTab rs={rs} />}
        </TabPanel>
      </div>
    </div>
  );
}

function NoSession({ rs }: { rs: ReturnType<typeof useResearch> }) {
  if (!rs.key) return <Notice tone="warn" title="Access key needed">Set the research access key in Settings to see this section.</Notice>;
  if (rs.loading || rs.running) return <div className="space-y-4"><SkeletonCard /><SkeletonCard lines={5} /></div>;
  return (
    <EmptyState title="Start research to see this section" action={<Button variant="primary" loading={rs.busy} onClick={() => rs.start()}>Start research</Button>}>
      Readiness, required documents and uploads belong to a research session for this company.
    </EmptyState>
  );
}

function MissingSummary({ rs, onJump, onDocs }: { rs: ReturnType<typeof useResearch>; onJump: (m: Requirement) => void; onDocs: () => void }) {
  const m = rs.data!.missing;
  const uploads = rs.data!.uploads;
  const crit = m.missing.CRITICAL, imp = m.missing.IMPORTANT;
  return (
    <Card aria-label="Missing document summary">
      <div className="flex flex-wrap items-start justify-between gap-4 p-5">
        <div className="grid flex-1 grid-cols-2 gap-5 sm:grid-cols-5">
          {[["Critical documents missing", crit.length, crit.length ? "text-bad" : "text-ok"], ["Important documents missing", imp.length, imp.length ? "text-warn" : "text-ok"], ["Uploaded", uploads.length, "text-ink"], ["Validated", uploads.filter((u) => u.status === "ACCEPTED").length, "text-ok"], ["Research blockers", m.eligibility === "BLOCKED" ? "Blocked" : crit.length ? crit.length : "None", m.eligibility === "BLOCKED" || crit.length ? "text-bad" : "text-ok"]].map(([k, v, c]) => (
            <div key={k as string}><p className="text-xs font-medium tracking-wide text-muted uppercase">{k}</p><p className={`tnum mt-1 text-xl font-semibold ${c}`}>{v}</p></div>
          ))}
        </div>
        <Button variant="primary" onClick={onDocs}>Open documents required</Button>
      </div>
      {(crit.length > 0 || imp.length > 0) && (
        <div className="border-t border-line px-5 py-3">
          <p className="mb-2 text-xs font-medium tracking-wide text-muted uppercase">Go straight to a requirement</p>
          <ul className="flex flex-wrap gap-2">
            {[...crit, ...imp].map((x) => (
              <li key={x.id}><button type="button" onClick={() => onJump(x)} className={`rounded-full border px-3 py-1 text-xs font-medium hover:bg-surface-2 ${x.importance === "CRITICAL" ? "border-bad-line text-bad" : "border-warn-line text-warn"}`}>{x.label}</button></li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
