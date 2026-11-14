import type { CompanyStages, Failure, Health, Metrics, Overview, Run, RunDetail, StageState } from "@/services/pipeline";

export const stage = (over: Partial<StageState> = {}): StageState => ({
  status: "SUCCEEDED",
  message: "validated",
  error: null,
  attempts: 1,
  consecutive_failures: 0,
  started_at: "2026-11-14T09:00:00Z",
  finished_at: "2026-11-14T09:00:05Z",
  next_retry_at: null,
  detail: null,
  last_run_id: 7,
  ...over,
});

export const run = (over: Partial<Run> = {}): Run => ({
  id: 7,
  trigger: "MANUAL",
  status: "COMPLETED",
  control: "NONE",
  stages: ["upstox", "extract", "evidence", "readiness"],
  symbols: null,
  batch_size: 2,
  options: { workers: 1 },
  companies: 2,
  summary: { companies_processed: 2 },
  created_by: "root",
  created_at: "2026-11-14T08:59:00Z",
  started_at: "2026-11-14T09:00:00Z",
  finished_at: "2026-11-14T09:05:00Z",
  heartbeat_at: "2026-11-14T09:05:00Z",
  ...over,
});

export const companies = (): CompanyStages[] => [
  {
    company_id: "c1",
    name: "63 moons technologies",
    symbol: "63MOONS",
    stages: {
      upstox: stage({ message: "REFRESHED" }),
      extract: stage({
        message: "3 document(s): 3 completed",
        detail: { documents: 3, by_status: { COMPLETED: 3 } },
      }),
      evidence: stage(),
      readiness: stage({ message: "ASSISTED 55.3/100 (6 of 18 available)" }),
    },
  },
  {
    company_id: "c2",
    name: "A B Infrabuild",
    symbol: "ABINFRA",
    stages: {
      upstox: stage(),
      extract: stage({
        status: "FAILED",
        message: "2 document(s): 1 failed",
        error: "1 extraction(s) failed",
        attempts: 2,
        next_retry_at: "2026-11-14T09:15:00Z",
      }),
      evidence: stage({
        status: "SKIPPED",
        message: "unchanged since 13/11/2026: nothing new to validate",
      }),
    },
  },
];

export const detail = (over: Partial<RunDetail> = {}): RunDetail => ({
  ...run(),
  company_states: companies(),
  events: [
    {
      id: 2,
      at: "2026-11-14T09:00:05Z",
      company_id: "c2",
      stage: "extract",
      status: "FAILED",
      message: "1 extraction(s) failed",
    },
    {
      id: 1,
      at: "2026-11-14T09:00:01Z",
      company_id: "c1",
      stage: "extract",
      status: "SUCCEEDED",
      message: "3 document(s): 3 completed",
    },
  ],
  ...over,
});

export const failure = (): Failure => ({
  ...stage({
    status: "FAILED",
    error: "1 extraction(s) failed",
    attempts: 2,
    next_retry_at: "2026-11-14T09:15:00Z",
  }),
  company_id: "c2",
  name: "A B Infrabuild",
  stage: "extract",
});

export const overview = (over: Partial<Overview> = {}): Overview => ({
  stages: [
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
  ],
  stage_status_counts: { upstox: { SUCCEEDED: 134 }, extract: { SUCCEEDED: 1, FAILED: 1 } },
  backlog: {
    companies_with_provider_facts: 158,
    companies_with_statement_evidence: 158,
    companies_missing_evidence: 0,
    financial_pdfs_stored: 83,
    financial_pdfs_read: 70,
    companies_with_unread_documents: 3,
  },
  active_run: null,
  latest_run: run(),
  schedule: {
    enabled: false,
    interval_minutes: 360,
    batch_size: 10,
    concurrency: 2,
    stages: null,
    symbols: null,
    last_triggered_at: null,
    worker_seen_at: "2026-11-14T09:05:40Z",
    worker_note: "host:4242",
  },
  ...over,
});

export const health = (over: Partial<Health> = {}): Health => ({
  status: "OK",
  checked_at: "2026-11-14T09:06:00Z",
  alerts: [],
  worker: { last_seen_at: "2026-11-14T09:05:40Z", note: "host:4242", alive: true },
  ...over,
});

export const metrics = (over: Partial<Metrics> = {}): Metrics => ({
  hours: 168,
  stages: [
    {
      stage: "documents",
      attempts: 12,
      succeeded: 9,
      skipped: 1,
      failed: 2,
      timed_out: 1,
      failure_rate: 0.1818,
      avg_seconds: 120,
      p50_seconds: 75,
      p95_seconds: 780,
      max_seconds: 900,
      budget_seconds: 900,
      p95_share_of_budget: 0.867,
      last_attempt_at: "2026-11-14T09:00:00Z",
    },
    {
      stage: "evidence",
      attempts: 30,
      succeeded: 30,
      skipped: 0,
      failed: 0,
      timed_out: 0,
      failure_rate: 0,
      avg_seconds: 2.4,
      p50_seconds: 1.8,
      p95_seconds: 6.2,
      max_seconds: 8,
      budget_seconds: 600,
      p95_share_of_budget: 0.01,
      last_attempt_at: "2026-11-14T09:01:00Z",
    },
  ],
  runs: [
    {
      run_id: 7,
      trigger: "SCHEDULED",
      status: "COMPLETED",
      companies: 2,
      processed: 2,
      failed_stages: 1,
      duration_seconds: 300,
      started_at: "2026-11-14T09:00:00Z",
    },
  ],
  ...over,
});
