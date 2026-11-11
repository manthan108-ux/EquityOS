"use client";

import { Badge, Button, Card, CardHeader, Notice, type Tone } from "@/components/ui";
import { describeFetch, type SessionStatus, type StepState } from "@/services/research";

const STATUS: Record<StepState["status"], { label: string; tone: Tone }> = {
  PENDING: { label: "Pending", tone: "neutral" },
  RUNNING: { label: "Running", tone: "info" },
  DONE: { label: "Completed", tone: "ok" },
  SKIPPED: { label: "Not required", tone: "neutral" },
  FAILED: { label: "Failed", tone: "bad" },
};

/** What a step does, in plain words (the backend label is used for the name). */
const PURPOSE: Record<string, string> = {
  IDENTITY: "Confirms exactly which listed company this is.",
  SOURCES: "Refreshes data from permitted sources, only when requested.",
  DOCUMENTS: "Lists the documents held and collects public ones from the company's own website.",
  EXTRACTION: "Reads figures out of the documents.",
  EVIDENCE: "Validates every figure and resolves conflicts between sources.",
  CALCULATION: "Calculates ratios, growth and valuation multiples from validated figures.",
  PEERS: "Selects comparable companies and compares them.",
  SCORING: "Scores the company against peers and benchmarks.",
  READINESS: "Checks whether the evidence is enough for a report.",
  REPORT: "Writes the report, or defers it until the evidence is enough.",
};

function Mark({ s, n }: { s: StepState["status"]; n: number }) {
  const base = "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border text-xs font-semibold";
  if (s === "DONE")
    return (
      <span aria-hidden className={`${base} border-ok bg-ok text-white`}>
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 5 5 9-10" /></svg>
      </span>
    );
  if (s === "FAILED") return <span aria-hidden className={`${base} border-bad bg-bad text-white`}>!</span>;
  if (s === "RUNNING") return <span aria-hidden className={`${base} animate-pulse border-brand bg-brand-soft text-brand`}>{n}</span>;
  if (s === "SKIPPED") return <span aria-hidden className={`${base} border-line-strong bg-surface-2 text-faint`}>–</span>;
  return <span aria-hidden className={`${base} border-line-strong bg-surface text-faint`}>{n}</span>;
}

/** Friendly failure text; the raw error stays available under technical details. */
function failure(step: StepState): string {
  return `“${step.label}” hit an unexpected problem and stopped. Stages that finished are kept and will not be repeated, so retrying is safe.`;
}

export default function Pipeline({
  session,
  busy,
  onRetry,
}: {
  session: SessionStatus;
  busy: boolean;
  onRetry: () => void;
}) {
  const failed = session.steps.find((s) => s.status === "FAILED");
  const done = session.steps.filter((s) => s.status === "DONE" || s.status === "SKIPPED").length;
  return (
    <Card aria-label="Research progress">
      <CardHeader
        title="Research pipeline"
        subtitle={`${done} of ${session.steps.length} stages finished`}
        actions={
          <Badge tone={session.status === "FAILED" ? "bad" : session.status === "COMPLETED" ? "ok" : session.status === "RUNNING" || session.status === "CREATED" ? "info" : "warn"} dot>
            {session.status.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase())}
          </Badge>
        }
      />
      <ol className="grid gap-px bg-line sm:grid-cols-2 xl:grid-cols-5" aria-label="Research stages">
        {session.steps.map((s, i) => {
          const st = STATUS[s.status];
          const fetchNote = s.step === "DOCUMENTS" ? describeFetch(s.detail?.evidence_fetch) : null;
          return (
            <li key={s.step} className="bg-surface p-4" aria-label={`${s.label}: ${st.label}`}>
              <div className="flex items-start gap-3">
                <Mark s={s.status} n={i + 1} />
                <div className="min-w-0">
                  <p className="text-[13px] leading-snug font-semibold text-ink">{s.label}</p>
                  <Badge tone={st.tone} className="mt-1">{st.label}</Badge>
                </div>
              </div>
              <p className="mt-2 text-xs text-muted">{PURPOSE[s.step] ?? ""}</p>
              {s.resumed_without_rerun && <p className="mt-1 text-xs text-ok">Already done earlier, not repeated.</p>}
              {fetchNote && <p className="mt-1 text-xs text-muted">{fetchNote}</p>}
            </li>
          );
        })}
      </ol>
      {session.status === "FAILED" && (
        <div className="space-y-3 border-t border-line p-5">
          <Notice tone="bad" title="Research stopped">
            {failed ? failure(failed) : "The research stopped before it finished."}
          </Notice>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" loading={busy} onClick={onRetry}>Retry from the failed stage</Button>
            <span className="text-xs text-muted">Nothing already validated is lost.</span>
          </div>
          {(failed?.error || session.last_error) && (
            <details className="text-xs text-muted">
              <summary className="cursor-pointer font-medium">Technical details</summary>
              <p className="mt-1 break-words font-mono">{failed?.error ?? session.last_error}</p>
              {failed && <p className="mt-1">Attempts at this stage: {failed.attempts}</p>}
            </details>
          )}
        </div>
      )}
    </Card>
  );
}
