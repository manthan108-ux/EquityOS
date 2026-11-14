// Client for the pipeline monitoring and control API. Reads need an API key; controls need an admin key.

import { API_URL } from "@/services/api";
import { ApiError, getApiKey } from "@/services/research";

export const STAGES = [
  "classify",
  "website",
  "upstox",
  "documents",
  "extract",
  "evidence",
  "calculate",
  "enrich",
  "readiness",
  "report",
] as const;
export type StageName = (typeof STAGES)[number];

export const STAGE_LABEL: Record<StageName, string> = {
  classify: "Industry label",
  website: "Website",
  upstox: "Upstox data",
  documents: "Documents",
  extract: "Extraction",
  evidence: "Evidence",
  calculate: "Calculation",
  enrich: "Enrichment",
  readiness: "Readiness",
  report: "Report",
};

export type StageStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "SKIPPED";
export type RunStatus = "QUEUED" | "RUNNING" | "PAUSED" | "CANCELLING" | "CANCELLED" | "COMPLETED" | "FAILED";

export type StageState = {
  status: StageStatus;
  message: string | null;
  error: string | null;
  attempts: number;
  consecutive_failures: number;
  started_at: string | null;
  finished_at: string | null;
  next_retry_at: string | null;
  detail: Record<string, unknown> | null;
  last_run_id: number | null;
};
export type CompanyStages = {
  company_id: string;
  name: string;
  symbol: string | null;
  stages: Partial<Record<StageName, StageState>>;
};
export type PipelineEvent = {
  id: number;
  at: string;
  company_id: string | null;
  stage: StageName | null;
  status: string;
  message: string | null;
};
export type Run = {
  id: number;
  trigger: string;
  status: RunStatus;
  control: string;
  stages: StageName[];
  symbols: string[] | null;
  batch_size: number;
  options: Record<string, unknown> | null;
  companies: number;
  summary: { companies_processed?: number; stages?: Record<string, Record<string, number>> } | null;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
};
export type RunDetail = Run & { company_states: CompanyStages[]; events: PipelineEvent[] };
export type Schedule = {
  enabled: boolean;
  interval_minutes: number;
  batch_size: number;
  concurrency: number;
  stages: StageName[] | null;
  symbols: string[] | null;
  last_triggered_at: string | null;
  worker_seen_at: string | null;
  worker_note: string | null;
};
export type StageMetric = {
  stage: StageName;
  attempts: number;
  succeeded: number;
  skipped: number;
  failed: number;
  timed_out: number;
  failure_rate: number | null;
  avg_seconds: number | null;
  p50_seconds: number | null;
  p95_seconds: number | null;
  max_seconds: number | null;
  budget_seconds: number;
  p95_share_of_budget: number | null;
  last_attempt_at: string | null;
};
export type RunMetric = {
  run_id: number;
  trigger: string;
  status: RunStatus;
  companies: number;
  processed: number | null;
  failed_stages: number;
  duration_seconds: number | null;
  started_at: string | null;
};
export type Metrics = { hours: number; stages: StageMetric[]; runs: RunMetric[] };
export type Alert = {
  level: "CRITICAL" | "WARNING";
  code: string;
  message: string;
  stage?: StageName;
  company?: string;
  run_id?: number;
};
export type Health = {
  status: "OK" | "DEGRADED" | "CRITICAL";
  checked_at: string;
  alerts: Alert[];
  worker: { last_seen_at: string | null; note: string | null; alive: boolean };
};
export type Backlog = {
  companies_with_provider_facts: number;
  companies_with_statement_evidence: number;
  companies_missing_evidence: number;
  financial_pdfs_stored: number;
  financial_pdfs_read: number;
  companies_with_unread_documents: number;
};
export type Overview = {
  stages: StageName[];
  stage_status_counts: Record<string, Partial<Record<StageStatus, number>>>;
  backlog: Backlog;
  active_run: Run | null;
  latest_run: Run | null;
  schedule: Schedule;
};
export type Failure = StageState & { company_id: string; name: string; stage: StageName };
export type RunRequest = {
  stages?: StageName[];
  symbols?: string[];
  batch_size?: number;
  workers?: number;
  force?: boolean;
  select?: "failed" | "missing-evidence";
};

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-API-Key", getApiKey());
  if (typeof init.body === "string") headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}/api/pipeline${path}`, { ...init, headers, cache: "no-store" });
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* no JSON body */
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : `HTTP ${res.status}`, detail);
  }
  return body as T;
}

export const fetchOverview = () => call<Overview>("/overview");
export const fetchRuns = () => call<Run[]>("/runs?limit=15");
export const fetchRun = (id: number) => call<RunDetail>(`/runs/${id}`);
export const fetchFailures = () => call<Failure[]>("/failures");
export const fetchMetrics = () => call<Metrics>("/metrics");
export const fetchHealth = () => call<Health>("/health");
export const startRun = (body: RunRequest) =>
  call<Run>("/runs", { method: "POST", body: JSON.stringify(body) });
export const controlRun = (id: number, action: "pause" | "resume" | "cancel") =>
  call<Run>(`/runs/${id}/${action}`, { method: "POST" });
export type ScheduleChange = Partial<
  Pick<Schedule, "enabled" | "interval_minutes" | "batch_size" | "concurrency" | "stages" | "symbols">
> & { clear_symbols?: boolean };
export const saveSchedule = (body: ScheduleChange) =>
  call<Schedule>("/schedule", { method: "PUT", body: JSON.stringify(body) });

export const isActive = (s: RunStatus) => ["QUEUED", "RUNNING", "CANCELLING"].includes(s);
