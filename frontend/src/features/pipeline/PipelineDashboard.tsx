"use client";

import { useState } from "react";

import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  Notice,
  Skeleton,
  Stat,
  inputClass,
  type Tone,
} from "@/components/ui";
import { usePipeline } from "@/features/pipeline/usePipeline";
import { explain } from "@/features/research/useResearch";
import {
  STAGES,
  STAGE_LABEL,
  controlRun,
  isActive,
  saveSchedule,
  startRun,
  type CompanyStages,
  type Health,
  type Metrics,
  type ScheduleChange,
  type RunDetail,
  type RunRequest,
  type RunStatus,
  type StageName,
  type StageState,
  type StageStatus,
} from "@/services/pipeline";

const STAGE_TONE: Record<StageStatus, Tone> = {
  SUCCEEDED: "ok",
  FAILED: "bad",
  RUNNING: "info",
  SKIPPED: "neutral",
  PENDING: "neutral",
};
const RUN_TONE: Record<RunStatus, Tone> = {
  QUEUED: "info",
  RUNNING: "info",
  PAUSED: "warn",
  CANCELLING: "warn",
  CANCELLED: "neutral",
  COMPLETED: "ok",
  FAILED: "bad",
};
const STATUS_WORD: Record<StageStatus, string> = {
  SUCCEEDED: "Done",
  FAILED: "Failed",
  RUNNING: "Running",
  SKIPPED: "Skipped",
  PENDING: "Pending",
};

const HEALTH_TONE: Record<Health["status"], Tone> = { OK: "ok", DEGRADED: "warn", CRITICAL: "bad" };
const HEALTH_WORD: Record<Health["status"], string> = {
  OK: "Healthy",
  DEGRADED: "Needs attention",
  CRITICAL: "Not working",
};

/** 0.4 -> "0.4 s", 75 -> "1 min 15 s", 5400 -> "1 h 30 min". */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds - m * 60);
    return s ? `${m} min ${s} s` : `${m} min`;
  }
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds - h * 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

function HealthCard({ health }: { health: Health | null }) {
  if (!health || !Array.isArray(health.alerts)) return null;
  const w = health.worker;
  return (
    <Card>
      <CardHeader
        title="Pipeline health"
        subtitle={`Worker: ${w?.alive ? "running" : "not reporting"}${w?.last_seen_at ? `, last seen ${when(w.last_seen_at)}` : ""}${w?.note ? ` (${w.note})` : ""}`}
        actions={<Badge tone={HEALTH_TONE[health.status]}>{HEALTH_WORD[health.status]}</Badge>}
      />
      {health.alerts.length === 0 ? (
        <p className="px-5 py-4 text-sm text-muted">No stuck stages, stalled runs or repeated failures.</p>
      ) : (
        <ul className="divide-y divide-line" aria-label="Alerts">
          {health.alerts.map((a, i) => (
            <li key={`${a.code}-${i}`} className="flex items-start gap-3 px-5 py-3 text-sm">
              <Badge tone={a.level === "CRITICAL" ? "bad" : "warn"}>{a.level === "CRITICAL" ? "Critical" : "Warning"}</Badge>
              <span>{a.message}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function MetricsCard({ metrics }: { metrics: Metrics | null }) {
  if (!metrics || !Array.isArray(metrics.stages)) return null;
  const days = Math.round(metrics.hours / 24);
  return (
    <Card>
      <CardHeader
        title="How long stages take"
        subtitle={`Every attempt in the last ${days} day${days === 1 ? "" : "s"}. A stage that runs out of time is stopped and retried later.`}
      />
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label="Stage durations">
          <thead className="text-left text-xs text-muted">
            <tr>
              <th scope="col" className="px-5 py-2 font-medium">Stage</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Attempts</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Failed</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Timed out</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Typical</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Slowest 5%</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Longest</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Time limit</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {metrics.stages.map((m) => (
              <tr key={m.stage}>
                <th scope="row" className="px-5 py-2 text-left font-medium">{STAGE_LABEL[m.stage] ?? m.stage}</th>
                <td className="tnum px-3 py-2 text-right">{m.attempts}</td>
                <td className={`tnum px-3 py-2 text-right ${m.failed ? "font-semibold text-bad" : "text-faint"}`}>{m.failed}</td>
                <td className={`tnum px-3 py-2 text-right ${m.timed_out ? "font-semibold text-warn" : "text-faint"}`}>{m.timed_out}</td>
                <td className="tnum px-3 py-2 text-right">{duration(m.p50_seconds)}</td>
                <td className={`tnum px-3 py-2 text-right ${(m.p95_share_of_budget ?? 0) > 0.8 ? "font-semibold text-warn" : ""}`}>{duration(m.p95_seconds)}</td>
                <td className="tnum px-3 py-2 text-right">{duration(m.max_seconds)}</td>
                <td className="tnum px-3 py-2 text-right text-muted">{duration(m.budget_seconds)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {metrics.runs.length > 0 && (
        <div className="border-t border-line px-5 py-3 text-xs text-muted">
          Latest runs:{" "}
          {metrics.runs.slice(0, 5).map((r) => (
            <span key={r.run_id} className="mr-3 inline-block">
              #{r.run_id} {r.status.toLowerCase()}, {r.processed ?? 0}/{r.companies} companies, {duration(r.duration_seconds)}
              {r.failed_stages ? `, ${r.failed_stages} failed` : ""}
            </span>
          ))}
        </div>
      )}
    </Card>
  );
}

const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "medium" }) : "-";

function StageCell({ state, onOpen, open }: { state?: StageState; onOpen: () => void; open: boolean }) {
  if (!state) return <span className="text-faint" aria-label="not run">-</span>;
  const why = state.error ?? state.message ?? "";
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-expanded={open}
      title={why}
      className="rounded-full focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
    >
      <Badge tone={STAGE_TONE[state.status]} dot>
        {STATUS_WORD[state.status]}
        {state.attempts > 1 ? ` x${state.attempts}` : ""}
      </Badge>
    </button>
  );
}

function StageGrid({ detail }: { detail: RunDetail }) {
  const [open, setOpen] = useState<{ company: string; stage: StageName } | null>(null);
  const stages = STAGES.filter((s) => detail.stages.includes(s));
  const opened = open ? detail.company_states.find((c) => c.company_id === open.company) : undefined;
  const state = opened && open ? opened.stages[open.stage] : undefined;
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <caption className="sr-only">Stage status of every company in run {detail.id}</caption>
          <thead className="text-left text-xs text-muted">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">Company</th>
              {stages.map((s) => (
                <th key={s} scope="col" className="px-2 py-2 font-medium">{STAGE_LABEL[s]}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {detail.company_states.map((c: CompanyStages) => (
              <tr key={c.company_id}>
                <th scope="row" className="px-3 py-2 text-left font-medium text-ink">
                  {c.name}
                  {c.symbol && <span className="ml-2 text-xs font-normal text-muted">{c.symbol}</span>}
                </th>
                {stages.map((s) => (
                  <td key={s} className="px-2 py-2">
                    <StageCell
                      state={c.stages[s]}
                      open={open?.company === c.company_id && open.stage === s}
                      onOpen={() =>
                        setOpen(open?.company === c.company_id && open.stage === s ? null : { company: c.company_id, stage: s })
                      }
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {state && opened && open && (
        <div role="region" aria-label="Stage detail" className="m-3 rounded-[10px] border border-line bg-surface-2 p-4 text-[13px]">
          <p className="font-semibold text-ink">
            {opened.name} - {STAGE_LABEL[open.stage]}: {STATUS_WORD[state.status]}
          </p>
          {state.message && <p className="mt-1 text-ink">{state.message}</p>}
          {state.error && <p className="mt-1 text-bad">Error: {state.error}</p>}
          <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-muted sm:grid-cols-4">
            <div><dt className="text-xs uppercase">Attempts</dt><dd className="text-ink">{state.attempts}</dd></div>
            <div><dt className="text-xs uppercase">Started</dt><dd className="text-ink">{when(state.started_at)}</dd></div>
            <div><dt className="text-xs uppercase">Finished</dt><dd className="text-ink">{when(state.finished_at)}</dd></div>
            <div><dt className="text-xs uppercase">Next retry</dt><dd className="text-ink">{when(state.next_retry_at)}</dd></div>
          </dl>
          {state.detail && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs font-medium text-muted">Stage result</summary>
              <pre className="mt-1 max-h-60 overflow-auto rounded bg-surface p-2 text-xs">{JSON.stringify(state.detail, null, 2)}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

export default function PipelineDashboard() {
  const p = usePipeline();
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [symbols, setSymbols] = useState("");
  const [batch, setBatch] = useState(10);
  const [workers, setWorkers] = useState(1);
  const [chosen, setChosen] = useState<StageName[]>([...STAGES]);

  const act = async (name: string, fn: () => Promise<unknown>) => {
    setBusy(name);
    setActionError(null);
    try {
      await fn();
      p.refresh();
    } catch (e) {
      setActionError(explain(e).message);
    } finally {
      setBusy(null);
    }
  };
  const launch = (extra: Partial<RunRequest> = {}) =>
    act("start", () =>
      startRun({
        stages: chosen.length === STAGES.length ? undefined : chosen,
        symbols: symbols.split(",").map((s) => s.trim()).filter(Boolean),
        batch_size: batch,
        workers,
        ...extra,
      }),
    );

  if (p.error) return <ErrorState message={p.error.message} detail={p.error.detail} onRetry={p.refresh} />;
  if (p.loading || !p.overview)
    return (
      <div className="space-y-4" aria-busy>
        <Skeleton className="h-24" />
        <Skeleton className="h-48" />
      </div>
    );
  const { overview, detail } = p;
  const b = overview.backlog;
  const runActive = detail ? isActive(detail.status) || detail.status === "PAUSED" : false;

  return (
    <div className="space-y-6">
      <Notice title="How runs execute">
        Runs queued here are executed by the pipeline worker (<code>python -m app.pipeline.cli serve</code>). This page shows what is
        stored for every stage; starting, pausing or cancelling needs an admin key.
      </Notice>
      {actionError && <Notice tone="bad" title="That did not work">{actionError}</Notice>}

      <HealthCard health={p.health} />

      <Card>
        <CardHeader title="Where the data stands" subtitle="Counts are read from the database, not from a log." />
        <dl className="grid grid-cols-2 gap-5 p-5 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Provider facts" value={b.companies_with_provider_facts} hint="companies" />
          <Stat label="With evidence" value={b.companies_with_statement_evidence} hint="validated statements" />
          <Stat label="Missing evidence" value={b.companies_missing_evidence} tone={b.companies_missing_evidence ? "warn" : "ok"} />
          <Stat label="PDFs stored" value={b.financial_pdfs_stored} />
          <Stat label="PDFs read" value={b.financial_pdfs_read} />
          <Stat label="Unread documents" value={b.companies_with_unread_documents} hint="companies" tone={b.companies_with_unread_documents ? "warn" : "ok"} />
        </dl>
      </Card>

      <Card>
        <CardHeader title="Stage status across all companies" subtitle="The latest persisted state of each stage." />
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th scope="col" className="px-5 py-2 font-medium">Stage</th>
                {(["SUCCEEDED", "SKIPPED", "FAILED", "RUNNING"] as StageStatus[]).map((s) => (
                  <th key={s} scope="col" className="px-3 py-2 text-right font-medium">{STATUS_WORD[s]}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {STAGES.map((s) => (
                <tr key={s}>
                  <th scope="row" className="px-5 py-2 text-left font-medium">{STAGE_LABEL[s]}</th>
                  {(["SUCCEEDED", "SKIPPED", "FAILED", "RUNNING"] as StageStatus[]).map((st) => {
                    const n = overview.stage_status_counts[s]?.[st] ?? 0;
                    return (
                      <td key={st} className={`tnum px-3 py-2 text-right ${st === "FAILED" && n ? "font-semibold text-bad" : n ? "" : "text-faint"}`}>{n}</td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHeader title="Start work" subtitle="Leave companies empty to take the next batch that most needs work." />
        <div className="grid gap-4 p-5 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Companies (symbols)" hint="Comma separated; one symbol runs a single company">
            <input className={inputClass} value={symbols} onChange={(e) => setSymbols(e.target.value)} placeholder="e.g. TCS, 63MOONS" />
          </Field>
          <Field label="Batch size">
            <input className={inputClass} type="number" min={1} max={100} value={batch} onChange={(e) => setBatch(Number(e.target.value))} />
          </Field>
          <Field label="Parallel workers" hint="Provider limits still apply">
            <input className={inputClass} type="number" min={1} max={8} value={workers} onChange={(e) => setWorkers(Number(e.target.value))} />
          </Field>
        </div>
        <fieldset className="px-5 pb-4">
          <legend className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Stages</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {STAGES.map((s) => (
              <label key={s} className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={chosen.includes(s)}
                  onChange={(e) => setChosen((c) => (e.target.checked ? [...c, s] : c.filter((x) => x !== s)))}
                />
                {STAGE_LABEL[s]}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="flex flex-wrap gap-2 border-t border-line px-5 py-4">
          <Button variant="primary" loading={busy === "start"} disabledReason={chosen.length ? undefined : "Choose at least one stage"} onClick={() => launch()}>
            {chosen.length === STAGES.length ? "Run full pipeline" : "Run selected stages"}
          </Button>
          <Button loading={busy === "retry"} onClick={() => act("retry", () => startRun({ select: "failed", batch_size: batch, workers }))}>
            Retry failed companies
          </Button>
          <Button loading={busy === "backlog"} onClick={() => act("backlog", () => startRun({ select: "missing-evidence", stages: ["evidence", "readiness"], batch_size: 100 }))}>
            Sync evidence backlog
          </Button>
        </div>
      </Card>

      <Card>
        <CardHeader title="Runs" subtitle="Execution history. Select a run to see every company's stages." />
        {p.runs.length === 0 ? (
          <EmptyState title="No runs yet">Start the pipeline above, or enable the schedule below.</EmptyState>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted">
                <tr>
                  {["Run", "Trigger", "Status", "Companies", "Started", "Finished", ""].map((h) => (
                    <th key={h} scope="col" className="px-3 py-2 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {p.runs.map((r) => (
                  <tr key={r.id} className={p.detail?.id === r.id ? "bg-brand-soft" : ""}>
                    <th scope="row" className="px-3 py-2 text-left font-medium">#{r.id}</th>
                    <td className="px-3 py-2">{r.trigger.toLowerCase()}</td>
                    <td className="px-3 py-2"><Badge tone={RUN_TONE[r.status]} dot>{r.status.toLowerCase()}</Badge></td>
                    <td className="tnum px-3 py-2">{r.summary?.companies_processed ?? 0} / {r.companies || r.batch_size}</td>
                    <td className="px-3 py-2">{when(r.started_at ?? r.created_at)}</td>
                    <td className="px-3 py-2">{when(r.finished_at)}</td>
                    <td className="px-3 py-2 text-right">
                      <Button size="sm" onClick={() => p.setSelected(r.id)} aria-label={`Show run ${r.id}`}>Show</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {detail && (
        <Card aria-label={`Run ${detail.id}`}>
          <CardHeader
            title={`Run #${detail.id}`}
            subtitle={`${detail.trigger.toLowerCase()} run by ${detail.created_by}; stages: ${detail.stages.map((s) => STAGE_LABEL[s]).join(", ")}`}
            actions={
              <>
                <Badge tone={RUN_TONE[detail.status]} dot>{detail.status.toLowerCase()}</Badge>
                {runActive && detail.status !== "PAUSED" && (
                  <Button size="sm" loading={busy === "pause"} onClick={() => act("pause", () => controlRun(detail.id, "pause"))}>Pause</Button>
                )}
                {detail.status === "PAUSED" && (
                  <Button size="sm" variant="primary" loading={busy === "resume"} onClick={() => act("resume", () => controlRun(detail.id, "resume"))}>Resume</Button>
                )}
                {runActive && (
                  <Button size="sm" variant="danger" loading={busy === "cancel"} onClick={() => act("cancel", () => controlRun(detail.id, "cancel"))}>Cancel</Button>
                )}
              </>
            }
          />
          {detail.company_states.length === 0 ? (
            <EmptyState title="No companies yet">The worker has not picked this run up.</EmptyState>
          ) : (
            <StageGrid detail={detail} />
          )}
          <div className="border-t border-line p-5">
            <h3 className="text-sm font-semibold">Stage log</h3>
            <ol className="mt-2 max-h-64 space-y-1 overflow-auto font-mono text-xs" aria-label="Stage log">
              {detail.events.map((e) => (
                <li key={e.id} className={e.status === "FAILED" ? "text-bad" : "text-muted"}>
                  {when(e.at)} {e.stage ?? "run"} {e.status.toLowerCase()} {e.message ?? ""}
                </li>
              ))}
            </ol>
          </div>
        </Card>
      )}

      <Card>
        <CardHeader title="Failed stages" subtitle="What failed, why, and when it will be retried automatically." />
        {p.failures.length === 0 ? (
          <p className="p-5 text-sm text-muted">No stage is currently failing.</p>
        ) : (
          <ul className="divide-y divide-line">
            {p.failures.map((f) => (
              <li key={`${f.company_id}-${f.stage}`} className="px-5 py-3 text-sm">
                <p className="font-medium">{f.name} - {STAGE_LABEL[f.stage]} <span className="font-normal text-muted">({f.attempts} attempts)</span></p>
                <p className="text-[13px] text-bad">{f.error}</p>
                <p className="text-xs text-muted">Next automatic retry: {when(f.next_retry_at)}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <MetricsCard metrics={p.metrics} />

      <ScheduleCard overview={overview} onSave={(body) => act("schedule", () => saveSchedule(body))} saving={busy === "schedule"} />
    </div>
  );
}

function ScheduleCard({
  overview,
  onSave,
  saving,
}: {
  overview: NonNullable<ReturnType<typeof usePipeline>["overview"]>;
  onSave: (body: ScheduleChange) => void;
  saving: boolean;
}) {
  const s = overview.schedule;
  const [enabled, setEnabled] = useState(s.enabled);
  const [interval, setInterval_] = useState(s.interval_minutes);
  const [batch, setBatch] = useState(s.batch_size);
  const [conc, setConc] = useState(s.concurrency);
  const [chosen, setChosen] = useState((s.symbols ?? []).join(", "));
  return (
    <Card>
      <CardHeader title="Schedule" subtitle={`Incremental runs on a timer, for the selected companies only. Last started: ${when(s.last_triggered_at)}`} />
      <div className="grid gap-4 p-5 sm:grid-cols-2 lg:grid-cols-4">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          Scheduled runs enabled
        </label>
        <Field label="Every (minutes)"><input className={inputClass} type="number" min={5} value={interval} onChange={(e) => setInterval_(Number(e.target.value))} /></Field>
        <Field label="Companies per run"><input className={inputClass} type="number" min={1} max={100} value={batch} onChange={(e) => setBatch(Number(e.target.value))} /></Field>
        <Field label="Parallel workers"><input className={inputClass} type="number" min={1} max={8} value={conc} onChange={(e) => setConc(Number(e.target.value))} /></Field>
        <Field label="Companies on the schedule (tickers)" hint="Required to enable the schedule; the whole universe is never scheduled by default">
          <input className={inputClass} value={chosen} onChange={(e) => setChosen(e.target.value)} placeholder="e.g. ICICIBANK, TCS" />
        </Field>
      </div>
      <div className="border-t border-line px-5 py-4">
        <Button variant="primary" loading={saving} onClick={() => {
            const list = chosen.split(",").map((x) => x.trim()).filter(Boolean);
            onSave({
              enabled,
              interval_minutes: interval,
              batch_size: batch,
              concurrency: conc,
              ...(list.length ? { symbols: list } : { clear_symbols: !enabled }),
            });
          }}>
          Save schedule
        </Button>
      </div>
    </Card>
  );
}
