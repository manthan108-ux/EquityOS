"use client";

import AccessKey from "@/features/research/AccessKey";
import PreferencesPanel from "@/features/research/PreferencesPanel";
import { Badge, Button, Card, CardHeader, EmptyState, Notice, Stat, type Tone } from "@/components/ui";
import { DEFAULT_PREFERENCES } from "@/services/research";
import type { ResearchState } from "@/features/research/useResearch";
import Pipeline from "./Pipeline";
import { ELIGIBILITY } from "./ReadinessPanel";
import type { TabId } from "./CompanyWorkspace";

const VT: Record<string, Tone> = { BUY: "ok", SELL: "bad", WATCH: "warn" };

export default function OverviewTab({ rs, go }: { rs: ResearchState; go: (t: TabId) => void }) {
  const { session, data, report } = rs;

  if (!rs.key) {
    return (
      <Card>
        <CardHeader title="Research access key needed" subtitle="Research is protected. Enter the key set by whoever runs the server." />
        <div className="p-5"><AccessKey /></div>
      </Card>
    );
  }

  if (!session) {
    return (
      <div className="space-y-6">
        <Card>
          <CardHeader title="Start research" subtitle="EquityOS first checks whether it holds enough validated evidence. If it does, research and the report run automatically. If not, it tells you exactly what is missing." />
          <div className="flex flex-wrap items-center gap-3 p-5">
            <Button variant="primary" size="md" loading={rs.busy} onClick={() => rs.start()}>Start research</Button>
            <span className="text-xs text-muted">Uses the default preferences: consolidated basis, latest available quarter. Change them below to start differently.</span>
          </div>
        </Card>
        <PreferencesPanel value={DEFAULT_PREFERENCES} busy={rs.busy} applyLabel="Start research with these preferences" onApply={(p) => rs.start(p)} />
        {rs.loading && <p className="text-xs text-muted">Looking for earlier research on this company…</p>}
      </div>
    );
  }

  const r = data?.readiness;
  const crit = r?.counts.critical_missing ?? 0;
  const verdict = report?.content.analysis?.verdict;
  const failed = session.status === "FAILED";

  // The single most useful next step, from the state of the research.
  const next = failed
    ? { text: "Research stopped. Retry from the failed stage.", action: <Button variant="primary" loading={rs.busy} onClick={() => rs.resume()}>Retry from the failed stage</Button> }
    : rs.running
      ? { text: "Research is running. This page updates by itself.", action: null }
      : r && r.eligibility !== "AUTOMATIC"
        ? { text: crit ? `${crit} critical document${crit === 1 ? " is" : "s are"} missing. Upload them, then resume research.` : "Some evidence is missing. Upload documents and resume, or generate an available-evidence report.", action: <Button variant="primary" onClick={() => go("documents")}>See documents required</Button> }
        : data && data.reports.length === 0
          ? { text: "Evidence is sufficient. Generate the report.", action: <Button variant="primary" onClick={() => go("reports")}>Go to Reports</Button> }
          : { text: "Research is complete. The report is ready.", action: <Button variant="primary" onClick={() => go("reports")}>View report</Button> };

  return (
    <div className="space-y-6">
      {rs.sessions && rs.sessions.length > 1 && (
        <Notice tone="info">
          This company has {rs.sessions.length} research sessions. You are viewing the one updated {session.updated_at?.slice(0, 10)}. Switch sessions in the Reports tab history or the Research Workspace page.
        </Notice>
      )}
      <Card>
        <CardHeader title="Next step" />
        <div className="flex flex-wrap items-center justify-between gap-4 p-5">
          <p className="max-w-2xl text-sm text-ink">{next.text}</p>
          {next.action}
        </div>
        {r && (
          <dl className="grid grid-cols-2 gap-5 border-t border-line p-5 sm:grid-cols-4">
            <Stat label="Research readiness" value={`${r.score} / 100`} tone={ELIGIBILITY[r.eligibility].tone === "ok" ? "ok" : ELIGIBILITY[r.eligibility].tone === "bad" ? "bad" : "warn"} hint={ELIGIBILITY[r.eligibility].label} tip="Measures the evidence held, not the company." />
            <Stat label="Critical missing" value={crit} tone={crit ? "bad" : "ok"} hint={`${r.counts.important_missing} important`} />
            <Stat label="Report" value={data?.reports.length ? `v${data.reports[0].version}` : "None yet"} hint={data?.reports[0]?.mode === "COMPLETE" ? "complete" : data?.reports.length ? "available evidence" : undefined} />
            <div>
              <dt className="text-xs font-medium tracking-wide text-muted uppercase">EquityOS verdict</dt>
              <dd className="mt-1">{verdict ? <Badge tone={VT[verdict.verdict]} className="px-3 py-1 text-sm">{verdict.verdict}</Badge> : <span className="text-sm text-muted">Not in the latest report</span>}</dd>
              {verdict && <p className="mt-0.5 text-xs text-muted">Evidence confidence {verdict.confidence.band.toLowerCase()}</p>}
            </div>
          </dl>
        )}
      </Card>

      <Pipeline session={session} busy={rs.busy} onRetry={() => rs.resume()} />

      {data && !r && <EmptyState title="Loading readiness…" />}

      <PreferencesPanel key={JSON.stringify(session.preferences)} value={session.preferences} log={session.preference_log} busy={rs.busy || rs.running} onApply={(p) => rs.savePreferences(p)} />
    </div>
  );
}
