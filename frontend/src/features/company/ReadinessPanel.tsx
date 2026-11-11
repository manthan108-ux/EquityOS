"use client";

import { Badge, Card, CardHeader, InfoTip, Notice, ProgressBar, Stat, type Tone } from "@/components/ui";
import type { Readiness } from "@/services/research";

export const ELIGIBILITY: Record<Readiness["eligibility"], { label: string; tone: Tone; help: string }> = {
  AUTOMATIC: { label: "Automatic research", tone: "ok", help: "Every critical requirement is met, so research and the complete report run without your input." },
  ASSISTED: { label: "Assisted research", tone: "warn", help: "Some evidence is missing. You can generate a report with what exists, or upload documents and resume." },
  BLOCKED: { label: "Blocked", tone: "bad", help: "No validated revenue or profit is held, so no report can be written yet." },
};

const TIPS: [RegExp, string][] = [
  [/sector/i, "How many of the metrics that matter for this sector (for a bank: NIM, NPA, capital adequacy) are available."],
  [/valuation/i, "Whether the inputs for P/E, P/B and market capitalisation are held."],
  [/document/i, "Whether figures come from filed documents rather than a single feed."],
  [/period|consisten/i, "Whether the figures used refer to the same reporting period."],
  [/reliab/i, "How authoritative the sources behind the figures are (exchange filing above third-party feed)."],
  [/validation/i, "How many figures passed every arithmetic and cross-source check."],
  [/complete/i, "Share of the requirements for this research profile that are available."],
];
const tip = (label: string) => TIPS.find(([r]) => r.test(label))?.[1];

/** Evidence readiness for one company. It measures the evidence, never the company's quality. */
export default function ReadinessPanel({ r }: { r: Readiness }) {
  const el = ELIGIBILITY[r.eligibility];
  return (
    <Card aria-label="Research readiness">
      <CardHeader
        title="Research readiness"
        subtitle="Measures the evidence EquityOS holds, not how good the company is."
        actions={<Badge tone={el.tone} dot>{el.label}</Badge>}
      />
      <div className="grid gap-8 p-5 lg:grid-cols-[220px_1fr]">
        <div>
          <p className="tnum text-5xl font-semibold text-ink">{r.score}</p>
          <p className="text-xs text-muted">of 100 · {r.profile_label}</p>
          <div className="mt-3"><ProgressBar value={r.score} label="Overall research readiness" /></div>
          <p className="mt-3 text-[13px] text-muted">{el.help}</p>
        </div>
        <div className="space-y-5">
          <dl className="grid grid-cols-2 gap-5 sm:grid-cols-4">
            <Stat label="Critical missing" value={r.counts.critical_missing} tone={r.counts.critical_missing ? "bad" : "ok"} />
            <Stat label="Important missing" value={r.counts.important_missing} tone={r.counts.important_missing ? "warn" : "ok"} />
            <Stat label="Requirements met" value={`${r.counts.available}/${r.counts.requirements}`} />
            <Stat label="Evidence confidence" value={r.confidence.charAt(0) + r.confidence.slice(1).toLowerCase()} tip="How reliable the underlying sources and checks are, taken together." />
          </dl>
          <ul className="space-y-3">
            {Object.entries(r.components).map(([k, c]) => (
              <li key={k}>
                <div className="flex items-center justify-between gap-3 text-[13px]">
                  <span className="flex items-center gap-1.5 font-medium text-ink">{c.label}{tip(c.label) && <InfoTip text={tip(c.label)!} />}</span>
                  <span className="tnum text-muted">{c.score === null ? "n/a" : c.score}</span>
                </div>
                <div className="mt-1"><ProgressBar value={c.score} tone={c.score !== null && c.score < r.thresholds.min_component ? "bad" : "ok"} label={c.label} /></div>
                <p className="mt-0.5 text-xs text-muted">{c.detail}</p>
              </li>
            ))}
          </ul>
        </div>
      </div>
      {r.reasons.length > 0 && (
        <ul className="list-disc space-y-1 border-t border-line px-9 py-4 text-[13px] text-ink">
          {r.reasons.map((x) => <li key={x}>{x}</li>)}
        </ul>
      )}
      <div className="space-y-3 border-t border-line p-5">
        <Notice tone="info" title="Four separate questions">
          Evidence readiness (this score), financial-data completeness (the requirements met), scoring eligibility (whether a score could be computed) and report completeness (complete or available-evidence) are different things. None of them says the company is attractive.
        </Notice>
        <p className="text-xs text-muted">
          Automatic research needs: no critical item missing, score of at least {r.thresholds.min_score}, every component of at least {r.thresholds.min_component}, and no open validation error. One rule alone is never enough.
        </p>
      </div>
    </Card>
  );
}
