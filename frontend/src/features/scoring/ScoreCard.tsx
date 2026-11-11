"use client";

import { useEffect, useState } from "react";

import { LineChart, Radar } from "@/components/charts";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  InfoTip,
  ProgressBar,
  SkeletonCard,
  type Tone,
} from "@/components/ui";
import { dateShort, score1, titleCase } from "@/lib/format";
import {
  fetchMetricScores,
  fetchScore,
  fetchScoreHistory,
  type MethodChoice,
  type MetricScore,
  type ScoreHistoryRow,
  type ScoreResponse,
} from "@/services/api";

export const PILLARS: Record<string, string> = {
  GROWTH: "Growth",
  PROFITABILITY: "Profitability",
  FINANCIAL_HEALTH: "Financial health",
  VALUATION: "Valuation",
  EFFICIENCY: "Operational efficiency",
  RETURNS: "Returns",
  BALANCE_SHEET: "Balance sheet",
  MOMENTUM: "Momentum",
};

const METHOD_TONE: Record<string, Tone> = { RELATIVE: "info", ABSOLUTE: "brand", HYBRID: "warn", UNAVAILABLE: "neutral" };
const METHOD_HELP: Record<string, string> = {
  RELATIVE: "Relative: each metric is a percentile among eligible same-industry peers. It says how the company compares, not how good it is in absolute terms.",
  ABSOLUTE: "Absolute: each metric is scored against a benchmark or a convention curve, independent of the peer set.",
  HYBRID: "Hybrid: a calibrated blend of absolute and relative scores. It stays off until the calibration can be justified.",
  UNAVAILABLE: "No score could be produced; the reason is shown.",
};

type Loaded =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "none" }
  | { phase: "ready"; score: ScoreResponse };

const barTone = (v: number): Tone => (v >= 60 ? "ok" : v >= 40 ? "warn" : "bad");

function BenchmarkBar({ m }: { m: MetricScore }) {
  const b = m.benchmark;
  if (!b || b.type !== "EMPIRICAL" || m.actual_value === null) return null;
  const v = Number(m.actual_value);
  const lo = Math.min(b.p25 ?? 0, v);
  const hi = Math.max(b.p90 ?? 0, v);
  const pos = (x: number) => (hi === lo ? 50 : ((x - lo) / (hi - lo)) * 100);
  return (
    <div className="relative mt-1.5 h-2 w-full rounded bg-line" title="p25 to p90 benchmark range">
      <div className="absolute h-2 rounded bg-brand-line" style={{ left: `${pos(b.p25 ?? lo)}%`, width: `${pos(b.p90 ?? hi) - pos(b.p25 ?? lo)}%` }} />
      <div className="absolute -top-0.5 h-3 w-1 rounded bg-brand" style={{ left: `${pos(v)}%` }} />
    </div>
  );
}

function Drilldown({ id, method, pillar }: { id: string; method: MethodChoice; pillar: string }) {
  const [rows, setRows] = useState<MetricScore[] | null>(null);
  useEffect(() => {
    let live = true;
    fetchMetricScores(id, method)
      .then((r) => live && setRows(Array.isArray(r) ? r.filter((m) => m.pillar === pillar) : []))
      .catch(() => live && setRows([]));
    return () => {
      live = false;
    };
  }, [id, method, pillar]);
  if (rows === null) return <p className="px-1 py-2 text-xs text-muted">Loading metrics…</p>;
  if (rows.length === 0) return <p className="px-1 py-2 text-xs text-muted">No metric detail is stored for this pillar.</p>;
  return (
    <ul className="divide-y divide-line">
      {rows.map((m) => (
        <li key={m.metric} className="py-2.5">
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="text-ink">{m.name}</span>
            <span className="tnum text-right">
              {m.status === "SCORED" ? (
                <>
                  <span className="text-muted">{m.actual_value} {m.unit}</span>{" "}
                  <strong className="text-ink">{m.score}</strong>
                </>
              ) : (
                <span className="text-muted">{m.status === "GAP" ? "not scored" : "excluded"}</span>
              )}
            </span>
          </div>
          {m.status === "SCORED" ? (
            <>
              <BenchmarkBar m={m} />
              <p className="mt-1 text-xs text-muted">
                {m.benchmark?.type === "EMPIRICAL"
                  ? `Benchmark ${m.benchmark.scope}: ${m.benchmark.observations} observations, ${m.benchmark.companies} companies (${m.benchmark.confidence}); median ${m.benchmark.median}`
                  : `Convention curve, not a validated industry benchmark. ${m.benchmark?.why ?? ""}`}
                {m.period_end && ` · ${m.period_end} ${m.basis ?? ""}`}
                {m.evidence_id && ` · evidence #${m.evidence_id}`}
              </p>
            </>
          ) : (
            <p className="mt-1 text-xs text-muted">{m.reason}</p>
          )}
        </li>
      ))}
    </ul>
  );
}

function Meta({ label, value, tip }: { label: string; value: string; tip?: string }) {
  return (
    <div className="min-w-0">
      <dt className="flex items-center gap-1 text-xs font-medium tracking-wide text-muted uppercase">
        {label}
        {tip && <InfoTip text={tip} />}
      </dt>
      <dd className="tnum mt-0.5 truncate text-sm font-medium text-ink" title={value}>{value}</dd>
    </div>
  );
}

export default function ScoreCard({ companyId, ticker }: { companyId: string; ticker?: string | null }) {
  const [method, setMethod] = useState<MethodChoice>("AUTO");
  const [state, setState] = useState<Loaded>({ phase: "loading" });
  const [open, setOpen] = useState<string | null>(null);
  const [history, setHistory] = useState<ScoreHistoryRow[]>([]);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let live = true;
    fetchScore(companyId, method)
      .then((s) => live && setState("run_id" in s ? { phase: "ready", score: s } : { phase: "none" }))
      .catch((e: Error) => live && setState({ phase: "error", message: e.message }));
    return () => {
      live = false;
    };
  }, [companyId, method, tick]);

  useEffect(() => {
    fetchScoreHistory(companyId)
      .then((h) => setHistory(Array.isArray(h) ? h : []))
      .catch(() => setHistory([]));
  }, [companyId]);

  const shown = state.phase === "ready" ? state.score : null;
  const sameKind = shown ? history.filter((h) => h.methodology === shown.methodology && h.overall !== null).slice(0, 12).reverse() : [];

  return (
    <div className="space-y-6" aria-label="Company scorecard">
      <Card>
        <CardHeader
          title="Scorecard"
          subtitle="The financial score ranks the company's metrics. It is separate from research readiness."
          actions={
            <div role="group" aria-label="Scoring methodology" className="flex gap-1">
              {(["AUTO", "RELATIVE", "ABSOLUTE", "HYBRID"] as MethodChoice[]).map((m) => (
                <Button
                  key={m}
                  size="sm"
                  variant={method === m ? "primary" : "secondary"}
                  aria-pressed={method === m}
                  onClick={() => {
                    setState({ phase: "loading" });
                    setMethod(m);
                  }}
                >
                  {m === "AUTO" ? "Automatic" : titleCase(m)}
                </Button>
              ))}
            </div>
          }
        />
        <div className="p-5">
          {state.phase === "loading" && <SkeletonCard lines={4} />}
          {state.phase === "error" && (
            <ErrorState title="Score unavailable" message="The score could not be loaded." detail={state.message} onRetry={() => { setState({ phase: "loading" }); setTick((t) => t + 1); }} />
          )}
          {state.phase === "none" && (
            <EmptyState title="No stored score">
              {ticker ?? "This company"} has no stored score with this methodology yet. A score appears once research has produced enough validated evidence.
            </EmptyState>
          )}
          {shown && <Body s={shown} method={method} companyId={companyId} open={open} setOpen={setOpen} />}
          {method === "HYBRID" && (
            <p className="mt-4 text-xs text-muted">
              Hybrid scoring stays off until a calibration between absolute and relative scores can be justified; with the current sample sizes it cannot.
            </p>
          )}
          <p className="mt-4 text-xs text-muted">Absolute and relative scores are different scales and are never ranked together.</p>
        </div>
      </Card>

      {shown && history.length > 0 && (
        <Card>
          <CardHeader title="Score history" subtitle={`Past ${titleCase(shown.methodology)} runs only; other score types are not mixed onto this scale.`} />
          <div className="grid gap-6 p-5 lg:grid-cols-[1fr_1fr]">
            {sameKind.length >= 2 ? (
              <LineChart
                title={`${titleCase(shown.methodology)} score over time`}
                unit="score, 0 to 100"
                categories={sameKind.map((h) => dateShort(h.calculated_at))}
                series={[{ name: "Overall", values: sameKind.map((h) => Number(h.overall)) }]}
                source="Each point is a stored score run."
              />
            ) : (
              <p className="text-sm text-muted">Fewer than two runs of this score type are stored, so no trend is drawn.</p>
            )}
            <ul className="divide-y divide-line text-sm">
              {history.slice(0, 8).map((h) => (
                <li key={h.run_id} className="flex items-center justify-between gap-3 py-2">
                  <span className="text-muted">{dateShort(h.calculated_at)}</span>
                  <Badge tone={METHOD_TONE[h.methodology] ?? "neutral"}>{titleCase(h.methodology)}</Badge>
                  <span className="tnum font-medium text-ink">{score1(h.overall)}</span>
                </li>
              ))}
            </ul>
          </div>
        </Card>
      )}
    </div>
  );
}

function Body({ s, method, companyId, open, setOpen }: { s: ScoreResponse; method: MethodChoice; companyId: string; open: string | null; setOpen: (p: string | null) => void }) {
  const pillars = s.pillar_scores ?? {};
  const axes = Object.keys(PILLARS).filter((k) => k in pillars).map((k) => ({ name: PILLARS[k], value: Number(pillars[k]) }));
  const missing = Object.keys(PILLARS).filter((k) => !(k in pillars) && s.methodology === "ABSOLUTE");
  const ex = s.explanation;
  return (
    <>
      <div className="grid gap-8 lg:grid-cols-[320px_1fr]">
        <div className="flex flex-col items-center rounded-[12px] border border-line bg-surface-2 p-5">
          <div className="flex items-center gap-1.5">
            <Badge tone={METHOD_TONE[s.methodology] ?? "neutral"}>{s.label}</Badge>
            <InfoTip text={METHOD_HELP[s.methodology] ?? ""} />
          </div>
          <p className="tnum mt-3 text-5xl font-semibold text-ink">{s.overall !== null ? score1(s.overall) : <span className="text-2xl text-muted">No score</span>}</p>
          {s.overall !== null && <p className="text-xs text-muted">out of 100</p>}
          {s.overall === null && s.unavailable_reason && <p className="mt-2 text-center text-xs text-muted">Why: {s.unavailable_reason}</p>}
          {axes.length >= 3 && <div className="mt-4 w-full"><Radar axes={axes} label="Pillar scores" /></div>}
        </div>
        <dl className="grid grid-cols-2 content-start gap-x-6 gap-y-4 sm:grid-cols-3" aria-label="Score details">
          <Meta label="Sector" value={`${titleCase(s.sector) || "—"} / ${titleCase(s.industry) || "—"}`} />
          <Meta label="Peer group" value={`${s.peer_count} in industry`} tip="Companies in the same industry used as the peer group." />
          <Meta label="Eligible peers" value={String(s.eligible_peer_count)} tip="Peers with enough validated data to be compared on the scored metrics." />
          <Meta label="Data coverage" value={s.weight_coverage ? `${s.weight_coverage}%` : "—"} tip="Share of the scoring weight that had usable data." />
          <Meta label="Confidence" value={s.confidence ? titleCase(s.confidence) : "—"} />
          <Meta label="Scoring profile" value={s.profile ?? "—"} />
          <Meta label="Benchmark version" value={s.benchmark_version ?? "n/a"} />
          <Meta label="Config version" value={s.config_version ?? "n/a"} />
          <Meta label="As of" value={dateShort(s.as_of)} />
          <Meta label="Calculated" value={new Date(s.calculated_at).toLocaleString("en-GB")} />
        </dl>
      </div>

      {ex?.restructuring && <p className="mt-5 rounded-[10px] border border-warn-line bg-warn-soft p-3 text-[13px] text-ink">Corporate restructuring: {ex.restructuring}</p>}
      {ex?.peer_context && <p className="mt-3 text-xs text-muted">{ex.peer_context}</p>}

      <h3 className="mt-7 text-sm font-semibold text-ink">Pillars</h3>
      <ul className="mt-2 divide-y divide-line rounded-[10px] border border-line">
        {Object.entries(pillars).map(([p, v]) => {
          const n = Number(v);
          return (
            <li key={p}>
              <button type="button" aria-expanded={open === p} onClick={() => setOpen(open === p ? null : p)} className="grid w-full grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 px-4 py-3 text-left hover:bg-surface-2 sm:grid-cols-[200px_1fr_56px]">
                <span className="text-sm font-medium text-ink">
                  {PILLARS[p] ?? titleCase(p)}
                  {ex?.pillar_coverage?.[p] !== undefined && <span className="mt-0.5 block text-xs font-normal text-muted">{ex.pillar_coverage[p]}% of its metrics scored</span>}
                </span>
                <span className="order-3 col-span-2 sm:order-none sm:col-span-1"><ProgressBar value={n} tone={barTone(n)} label={`${PILLARS[p] ?? p} score`} /></span>
                <strong className="tnum text-right text-sm text-ink">{n.toFixed(1)}</strong>
              </button>
              {open === p && s.methodology === "ABSOLUTE" && <div className="border-t border-line bg-surface-2 px-4 pb-2"><Drilldown id={companyId} method={method} pillar={p} /></div>}
              {open === p && s.methodology === "RELATIVE" && <p className="border-t border-line bg-surface-2 px-4 py-3 text-xs text-muted">Relative scores are percentiles among eligible same-industry peers. The per-metric peer comparison is in the Peer comparison tab.</p>}
            </li>
          );
        })}
        {missing.map((p) => (
          <li key={p} className="px-4 py-3 text-sm text-muted">{PILLARS[p]} <span className="text-xs">· not scored (no usable metrics; never counted as zero)</span></li>
        ))}
      </ul>

      {ex && (
        <div className="mt-6 grid gap-5 text-sm sm:grid-cols-2">
          {ex.improved && ex.improved.length > 0 && (
            <div><h4 className="font-semibold text-ok">What helped</h4><ul className="mt-1 list-disc space-y-1 pl-5 text-muted">{ex.improved.map((t) => <li key={t}>{t}</li>)}</ul></div>
          )}
          {ex.reduced && ex.reduced.length > 0 && (
            <div><h4 className="font-semibold text-bad">What held it back</h4><ul className="mt-1 list-disc space-y-1 pl-5 text-muted">{ex.reduced.map((t) => <li key={t}>{t}</li>)}</ul></div>
          )}
          {ex.missing && ex.missing.length > 0 && (
            <details className="sm:col-span-2"><summary className="cursor-pointer font-semibold text-ink">Missing metrics ({ex.missing.length})</summary><ul className="mt-1 list-disc pl-5 text-xs text-muted">{ex.missing.map((m) => <li key={m.metric + m.pillar}>{m.metric}: {m.reason}</li>)}</ul></details>
          )}
          {ex.assumptions && ex.assumptions.length > 0 && (
            <div className="sm:col-span-2"><h4 className="font-semibold text-ink">Scoring assumptions</h4><ul className="mt-1 list-disc pl-5 text-xs text-muted">{ex.assumptions.map((t) => <li key={t}>{t}</li>)}</ul></div>
          )}
        </div>
      )}
    </>
  );
}
