"use client";

import { useEffect, useState } from "react";

import {
  fetchMetricScores,
  fetchScore,
  fetchScoreHistory,
  type MethodChoice,
  type MetricScore,
  type ScoreHistoryRow,
  type ScoreResponse,
} from "@/services/api";

const PILLARS: Record<string, string> = {
  GROWTH: "Growth",
  PROFITABILITY: "Profitability",
  FINANCIAL_HEALTH: "Financial health",
  VALUATION: "Valuation",
  EFFICIENCY: "Operational efficiency",
  // Phase 8 relative engine pillars
  RETURNS: "Returns",
  BALANCE_SHEET: "Balance sheet",
  MOMENTUM: "Momentum",
};

const BADGE: Record<string, string> = {
  RELATIVE: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  ABSOLUTE:
    "bg-violet-100 text-violet-900 dark:bg-violet-950 dark:text-violet-200",
  HYBRID: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  UNAVAILABLE: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};

type Loaded =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "none" }
  | { phase: "ready"; score: ScoreResponse };

function Radar({ values }: { values: Record<string, string> }) {
  // Pillars without a score are left out of the polygon (never drawn as zero) and listed below.
  const keys = Object.keys(PILLARS).filter((k) => k in values);
  const size = 220;
  const c = size / 2;
  const r = 80;
  if (keys.length < 3) {
    return (
      <p className="text-xs text-zinc-500">
        At least three scored pillars are needed for a chart.
      </p>
    );
  }
  const point = (i: number, v: number) => {
    const a = (Math.PI * 2 * i) / keys.length - Math.PI / 2;
    return [c + Math.cos(a) * r * (v / 100), c + Math.sin(a) * r * (v / 100)];
  };
  const poly = keys
    .map((k, i) => point(i, Number(values[k])).join(","))
    .join(" ");
  return (
    <svg
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label="Pillar scores"
      className="w-56"
    >
      {[25, 50, 75, 100].map((ring) => (
        <polygon
          key={ring}
          points={keys.map((_, i) => point(i, ring).join(",")).join(" ")}
          fill="none"
          className="stroke-zinc-300 dark:stroke-zinc-700"
        />
      ))}
      <polygon points={poly} className="fill-violet-500/30 stroke-violet-600" />
      {keys.map((k, i) => {
        const [x, y] = point(i, 118);
        return (
          <text
            key={k}
            x={x}
            y={y}
            textAnchor="middle"
            fontSize="8"
            className="fill-zinc-500"
          >
            {PILLARS[k]}
          </text>
        );
      })}
    </svg>
  );
}

function BenchmarkBar({ m }: { m: MetricScore }) {
  const b = m.benchmark;
  if (!b || b.type !== "EMPIRICAL" || m.actual_value === null) return null;
  const lo = Math.min(b.p25 ?? 0, Number(m.actual_value));
  const hi = Math.max(b.p90 ?? 0, Number(m.actual_value));
  const pos = (v: number) => (hi === lo ? 50 : ((v - lo) / (hi - lo)) * 100);
  return (
    <div
      className="relative mt-1 h-2 w-full rounded bg-zinc-200 dark:bg-zinc-800"
      title="p25–p90 range"
    >
      <div
        className="absolute h-2 rounded bg-zinc-400/60"
        style={{
          left: `${pos(b.p25 ?? lo)}%`,
          width: `${pos(b.p90 ?? hi) - pos(b.p25 ?? lo)}%`,
        }}
      />
      <div
        className="absolute -top-0.5 h-3 w-1 rounded bg-violet-600"
        style={{ left: `${pos(Number(m.actual_value))}%` }}
      />
    </div>
  );
}

function Drilldown({
  id,
  method,
  pillar,
}: {
  id: string;
  method: MethodChoice;
  pillar: string;
}) {
  const [rows, setRows] = useState<MetricScore[] | null>(null);
  useEffect(() => {
    let live = true;
    fetchMetricScores(id, method)
      .then(
        (r) =>
          live &&
          setRows(Array.isArray(r) ? r.filter((m) => m.pillar === pillar) : []),
      )
      .catch(() => live && setRows([]));
    return () => {
      live = false;
    };
  }, [id, method, pillar]);
  if (rows === null)
    return <p className="text-xs text-zinc-500">Loading metrics…</p>;
  if (rows.length === 0)
    return (
      <p className="text-xs text-zinc-500">
        No metric detail stored for this pillar.
      </p>
    );
  return (
    <ul className="mt-2 divide-y divide-zinc-200 text-sm dark:divide-zinc-800">
      {rows.map((m) => (
        <li key={m.metric} className="py-2">
          <div className="flex items-baseline justify-between gap-3">
            <span>{m.name}</span>
            <span className="tabular-nums">
              {m.status === "SCORED" ? (
                <>
                  <span className="text-zinc-500">
                    {m.actual_value} {m.unit}
                  </span>{" "}
                  <strong>{m.score}</strong>
                </>
              ) : (
                <span className="text-zinc-500">
                  {m.status === "GAP" ? "not scored" : "excluded"}
                </span>
              )}
            </span>
          </div>
          {m.status === "SCORED" ? (
            <>
              <BenchmarkBar m={m} />
              <p className="mt-1 text-xs text-zinc-500">
                {m.benchmark?.type === "EMPIRICAL"
                  ? `Benchmark ${m.benchmark.scope}: ${m.benchmark.observations} observations, ${m.benchmark.companies} companies (${m.benchmark.confidence}); median ${m.benchmark.median}`
                  : `Convention curve, not a validated industry benchmark. ${m.benchmark?.why ?? ""}`}
                {m.period_end && ` · ${m.period_end} ${m.basis ?? ""}`}
                {m.evidence_id && ` · evidence #${m.evidence_id}`}
              </p>
            </>
          ) : (
            <p className="mt-1 text-xs text-zinc-500">{m.reason}</p>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function ScoreCard({
  companyId,
  ticker,
}: {
  companyId: string;
  ticker?: string | null;
}) {
  const [method, setMethod] = useState<MethodChoice>("AUTO");
  const [state, setState] = useState<Loaded>({ phase: "loading" });
  const [open, setOpen] = useState<string | null>(null);
  const [history, setHistory] = useState<ScoreHistoryRow[]>([]);

  useEffect(() => {
    let live = true;
    fetchScore(companyId, method)
      .then((s) => {
        if (!live) return;
        setState(
          "run_id" in s ? { phase: "ready", score: s } : { phase: "none" },
        );
      })
      .catch(
        (e: Error) => live && setState({ phase: "error", message: e.message }),
      );
    return () => {
      live = false;
    };
  }, [companyId, method]);

  useEffect(() => {
    fetchScoreHistory(companyId)
      .then((h) => setHistory(Array.isArray(h) ? h : []))
      .catch(() => setHistory([]));
  }, [companyId]);

  const body = () => {
    if (state.phase === "loading")
      return <p className="text-sm text-zinc-500">Loading score…</p>;
    if (state.phase === "error")
      return (
        <p className="text-sm text-red-600">
          Could not load the score: {state.message}
        </p>
      );
    if (state.phase === "none")
      return (
        <p className="text-sm text-zinc-500">
          No stored score for {ticker ?? "this company"} with this methodology
          yet.
        </p>
      );
    const s = state.score;
    const pillars = s.pillar_scores ?? {};
    const missing = Object.keys(PILLARS).filter(
      (k) => !(k in pillars) && s.methodology === "ABSOLUTE",
    );
    return (
      <>
        <div className="flex flex-wrap items-start gap-6">
          <div>
            <span
              className={`rounded-full px-2.5 py-1 text-xs font-semibold ${BADGE[s.methodology]}`}
            >
              {s.label}
            </span>
            <p className="mt-3 text-5xl font-semibold tabular-nums">
              {s.overall ?? (
                <span className="text-2xl text-zinc-500">No score</span>
              )}
            </p>
            {s.overall === null && s.unavailable_reason && (
              <p className="mt-2 max-w-sm text-xs text-zinc-500">
                Why: {s.unavailable_reason}
              </p>
            )}
          </div>
          {s.pillar_scores && <Radar values={pillars} />}
        </div>

        <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          {[
            ["Sector", `${s.sector ?? "—"} / ${s.industry ?? "—"}`],
            [
              "Peers",
              `${s.peer_count} in industry, ${s.eligible_peer_count} eligible`,
            ],
            [
              "Data coverage",
              s.weight_coverage ? `${s.weight_coverage}%` : "—",
            ],
            ["Confidence", s.confidence ?? "—"],
            ["Benchmark version", s.benchmark_version ?? "n/a"],
            ["Profile", s.profile ?? "—"],
            ["As of", s.as_of],
            ["Calculated", new Date(s.calculated_at).toLocaleString()],
          ].map(([k, v]) => (
            <div key={k}>
              <dt className="text-xs uppercase tracking-wide text-zinc-500">
                {k}
              </dt>
              <dd className="mt-0.5 font-medium">{v}</dd>
            </div>
          ))}
        </dl>

        {s.explanation?.restructuring && (
          <p className="mt-4 rounded-lg bg-amber-50 p-3 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">
            Corporate restructuring: {s.explanation.restructuring}
          </p>
        )}
        {s.explanation?.peer_context && (
          <p className="mt-3 text-xs text-zinc-500">
            {s.explanation.peer_context}
          </p>
        )}

        <h3 className="mt-6 text-sm font-semibold">Pillars</h3>
        <ul className="mt-2 space-y-1">
          {Object.entries(pillars).map(([p, v]) => (
            <li key={p}>
              <button
                type="button"
                onClick={() => setOpen(open === p ? null : p)}
                className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm hover:bg-zinc-100 dark:hover:bg-zinc-800"
              >
                <span>
                  {PILLARS[p] ?? p}
                  {s.explanation?.pillar_coverage?.[p] !== undefined && (
                    <span className="ml-2 text-xs text-zinc-500">
                      {s.explanation.pillar_coverage[p]}% of its metrics scored
                    </span>
                  )}
                </span>
                <strong className="tabular-nums">{Number(v).toFixed(1)}</strong>
              </button>
              {open === p && s.methodology === "ABSOLUTE" && (
                <div className="px-2 pb-2">
                  <Drilldown id={companyId} method={method} pillar={p} />
                </div>
              )}
              {open === p && s.methodology === "RELATIVE" && (
                <p className="px-2 pb-2 text-xs text-zinc-500">
                  Relative scores are percentiles among eligible same-industry
                  peers; the per-metric comparison is served by the peer
                  scorecard API.
                </p>
              )}
            </li>
          ))}
          {missing.map((p) => (
            <li key={p} className="px-2 py-1.5 text-sm text-zinc-500">
              {PILLARS[p]}{" "}
              <span className="text-xs">
                — not scored (no usable metrics; not counted as zero)
              </span>
            </li>
          ))}
        </ul>

        {s.explanation && (
          <div className="mt-5 grid gap-4 text-sm sm:grid-cols-2">
            {s.explanation.improved && s.explanation.improved.length > 0 && (
              <div>
                <h4 className="font-semibold">What helped</h4>
                <ul className="mt-1 list-disc pl-5 text-zinc-600 dark:text-zinc-400">
                  {s.explanation.improved.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            )}
            {s.explanation.reduced && s.explanation.reduced.length > 0 && (
              <div>
                <h4 className="font-semibold">What held it back</h4>
                <ul className="mt-1 list-disc pl-5 text-zinc-600 dark:text-zinc-400">
                  {s.explanation.reduced.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </div>
            )}
            {s.explanation.missing && s.explanation.missing.length > 0 && (
              <details className="sm:col-span-2">
                <summary className="cursor-pointer font-semibold">
                  Missing metrics ({s.explanation.missing.length})
                </summary>
                <ul className="mt-1 list-disc pl-5 text-xs text-zinc-500">
                  {s.explanation.missing.map((m) => (
                    <li key={m.metric + m.pillar}>
                      {m.metric} — {m.reason}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {s.explanation.assumptions &&
              s.explanation.assumptions.length > 0 && (
                <div className="sm:col-span-2">
                  <h4 className="font-semibold">Assumptions</h4>
                  <ul className="mt-1 list-disc pl-5 text-xs text-zinc-500">
                    {s.explanation.assumptions.map((t) => (
                      <li key={t}>{t}</li>
                    ))}
                  </ul>
                </div>
              )}
          </div>
        )}
      </>
    );
  };

  return (
    <section
      aria-label="Company scorecard"
      className="mt-6 rounded-xl border border-zinc-200 bg-white p-6 dark:border-zinc-800 dark:bg-zinc-900"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Scorecard
        </p>
        <div
          role="group"
          aria-label="Scoring methodology"
          className="flex gap-1 text-xs"
        >
          {(["AUTO", "RELATIVE", "ABSOLUTE", "HYBRID"] as MethodChoice[]).map(
            (m) => (
              <button
                key={m}
                type="button"
                aria-pressed={method === m}
                onClick={() => {
                  setState({ phase: "loading" });
                  setMethod(m);
                }}
                className={`rounded-md px-2.5 py-1 ${
                  method === m
                    ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                    : "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                }`}
              >
                {m === "AUTO"
                  ? "Automatic"
                  : m.charAt(0) + m.slice(1).toLowerCase()}
              </button>
            ),
          )}
        </div>
      </div>
      <div className="mt-4">{body()}</div>
      {method === "HYBRID" && (
        <p className="mt-3 text-xs text-zinc-500">
          Hybrid scoring stays off until a calibration between absolute and
          relative scores can be justified; with the current sample sizes it
          cannot.
        </p>
      )}
      <p className="mt-4 text-xs text-zinc-500">
        Absolute and relative scores are different scales and are never ranked
        together.
      </p>
      {history.length > 1 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs font-semibold">
            Score history ({history.length})
          </summary>
          <ul className="mt-1 text-xs text-zinc-500">
            {history.slice(0, 10).map((h) => (
              <li key={h.run_id}>
                {new Date(h.calculated_at).toLocaleDateString()} ·{" "}
                {h.methodology} · {h.overall ?? "no score"}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
