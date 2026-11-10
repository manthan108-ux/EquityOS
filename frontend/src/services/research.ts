// Client for the research workflow API (Phase 11).
//
// Research endpoints need an API key (header X-API-Key). It is kept in sessionStorage only, so it
// disappears when the tab closes and is never written to the page, the URL or a cookie.

import { API_URL } from "@/services/api";

const KEY_STORE = "equityos.apiKey";

const listeners = new Set<() => void>();

export const getApiKey = (): string => {
  try {
    return window.sessionStorage.getItem(KEY_STORE) ?? "";
  } catch {
    return "";
  }
};
export const setApiKey = (key: string): void => {
  try {
    if (key) window.sessionStorage.setItem(KEY_STORE, key);
    else window.sessionStorage.removeItem(KEY_STORE);
  } catch {
    /* storage unavailable: the key then lives only until the page is reloaded */
  }
  listeners.forEach((l) => l());
};
// For useSyncExternalStore: components re-render when the key is set or cleared.
export const subscribeApiKey = (listener: () => void): (() => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

async function research<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("X-API-Key", getApiKey());
  if (typeof init.body === "string") headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}/api/research${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* no JSON body */
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : ((detail as { message?: string } | undefined)?.message ?? `HTTP ${res.status}`);
    throw new ApiError(res.status, message, detail);
  }
  return body as T;
}

export type Preferences = {
  basis: "CONSOLIDATED" | "STANDALONE";
  period_mode: "LATEST_QUARTER" | "LATEST_ANNUAL" | "QUARTER" | "FISCAL_YEAR";
  fiscal_year: number | null;
  quarter: "Q1" | "Q2" | "Q3" | "Q4" | null;
  include_optional: boolean;
  period_label?: string;
};
export const DEFAULT_PREFERENCES: Preferences = {
  basis: "CONSOLIDATED",
  period_mode: "LATEST_QUARTER",
  fiscal_year: null,
  quarter: null,
  include_optional: false,
};
export type PreferenceLogEntry = {
  at: string;
  by: string;
  from: Partial<Preferences> | null;
  to: Partial<Preferences>;
};

export type StepState = {
  step: string;
  label: string;
  status: "PENDING" | "RUNNING" | "DONE" | "SKIPPED" | "FAILED";
  attempts: number;
  error: string | null;
  resumed_without_rerun: boolean;
  finished_at: string | null;
};
export type SessionStatus = {
  id: string;
  company_id: string;
  query: string;
  status: "CREATED" | "RUNNING" | "AWAITING_DOCUMENTS" | "READY" | "COMPLETED" | "FAILED";
  workflow: string | null;
  eligibility: string | null;
  readiness_score: number | null;
  choice: string | null;
  preferences: Preferences;
  preference_log: PreferenceLogEntry[];
  last_error: string | null;
  updated_at: string | null;
  steps: StepState[];
};
export type SessionRow = {
  id: string;
  company_id: string;
  company: string | null;
  status: SessionStatus["status"];
  eligibility: string | null;
  readiness_score: number | null;
  latest_report_version: number | null;
  updated_at: string | null;
};
export type Found = {
  metric: string;
  value: string | null;
  unit: string | null;
  period_end: string | null;
  period_type: string | null;
  basis: string | null;
  document_id: string | null;
  page: number | null;
  quality: string | null;
  dimension?: string | null;
  period_note?: string | null;
};
export type HeldDocument = {
  title: string | null;
  doc_type: string;
  financial_period: string | null;
  extraction: string;
};
export type Requirement = {
  id: string;
  label: string;
  importance: "CRITICAL" | "IMPORTANT" | "OPTIONAL";
  status: "AVAILABLE" | "STALE" | "MISSING";
  why_it_matters: string;
  affects: string;
  sections: string[];
  document: string;
  document_types: string[];
  preferred_period: string;
  basis_required: string;
  official_source: string;
  alternative: string | null;
  upload_helps: boolean;
  action: string;
  reason?: string | null;
  missing_inputs?: string[];
  held_documents?: HeldDocument[];
  other_basis_available?: { basis: string | null; period_end: string | null } | null;
  found?: Found;
};
export type Component = {
  label: string;
  score: number | null;
  applicable: boolean;
  weight: number;
  detail: string;
};
export type Readiness = {
  profile_label: string;
  score: number;
  eligibility: "AUTOMATIC" | "ASSISTED" | "BLOCKED";
  confidence: string;
  reasons: string[];
  open_validation_errors: number;
  components: Record<string, Component>;
  thresholds: { min_score: number; min_component: number };
  counts: {
    requirements: number;
    available: number;
    critical_missing: number;
    important_missing: number;
    optional_missing: number;
  };
  sections: { key: string; title: string; status: string; missing: string[] }[];
};
export type MissingAssessment = {
  eligibility: string;
  reasons: string[];
  missing: Record<"CRITICAL" | "IMPORTANT" | "OPTIONAL", Requirement[]>;
  resolved: { id: string; label: string; importance: string; found: Found }[];
  documents_held: HeldDocument[];
  options: { continue_with_available: boolean };
};
export type Upload = {
  id: string;
  filename: string | null;
  status:
    | "RECEIVED"
    | "PROCESSING"
    | "ACCEPTED"
    | "HELD_PRIVATE"
    | "NEEDS_CLARIFICATION"
    | "REJECTED"
    | "DUPLICATE"
    | "FAILED";
  reject_reason: string | null;
  category: string | null;
  fiscal_year: string | null;
  quarter: string | null;
  basis: string | null;
  accounting_standard: string | null;
  issuer_status: string | null;
  extraction_status: string | null;
  validation_status: string | null;
  visibility: string;
  version: number;
  uploaded_at: string | null;
  ambiguities: { code: string; message: string; options: string[] }[];
  conflicts: { metric: string; period_end: string | null; selected: string; note: string }[];
  resolved_requirements: string[];
  steps: { stage: string; ok: boolean; detail: string }[];
  warnings: string[];
};
export type ReportRow = {
  id: string;
  version: number;
  mode: "AVAILABLE_EVIDENCE" | "COMPLETE";
  readiness_score: number | null;
  missing_critical: number;
  missing_important: number;
  created_at: string | null;
  complete: boolean;
};
export type ReportItem = {
  requirement: string;
  label: string;
  importance: string;
  value: string | null;
  unit: string | null;
  period_end: string | null;
  basis: string | null;
  dimension?: string | null;
  period_note?: string | null;
  source: { evidence_id: number | null; document_id: string | null; page: number | null };
};
export type ReportGap = {
  requirement: string;
  label: string;
  importance: string;
  reason: string | null;
  affects: string;
};
type Excerpt = { heading: string; excerpt: string | null; source: { page: number | null } } | null;
export type ReportSection = {
  key: string;
  title: string;
  status: string;
  items: ReportItem[];
  gaps: ReportGap[];
  series?: Record<string, { fiscal_year: string; value: string; unit: string | null }[]>;
  segments?: { metric: string; segment: string | null; value: string; unit: string | null }[];
  peers?: {
    available: boolean;
    peer_count?: number;
    thin?: boolean;
    reason?: string;
    peers?: { name: string; tier: string }[];
  };
  score?: {
    available: boolean;
    methodology?: string;
    overall?: string | null;
    confidence?: string | null;
    reason?: string | null;
  };
  management_discussion?: Excerpt;
  auditor_report?: Excerpt;
};
export type Report = ReportRow & {
  content: {
    meta: {
      banner: string;
      complete: boolean;
      company: string;
      as_of: string;
      preferences?: Preferences;
    };
    readiness: { score: number; eligibility: string; confidence: string; note: string };
    sections: ReportSection[];
    limitations: {
      missing_critical: ReportGap[];
      missing_important: ReportGap[];
      missing_optional: ReportGap[];
      unavailable_calculations: ReportGap[];
      incomplete_valuation: ReportGap[];
      unavailable_scores: string | null;
      limits_on_conclusions: string[];
      price_history_notes?: { symbol: string; date: string; factor: string; note: string }[];
    };
  };
};

export const startResearch = (companyId: string, preferences?: Preferences) =>
  research<SessionStatus>("/sessions", {
    method: "POST",
    body: JSON.stringify({ company_id: companyId, ...(preferences ? { preferences } : {}) }),
  });
export const updatePreferences = (id: string, preferences: Preferences) =>
  research<SessionStatus>(`/sessions/${id}/preferences`, {
    method: "PUT",
    body: JSON.stringify(preferences),
  });
export const downloadReportPdf = async (id: string): Promise<Blob> => {
  const res = await fetch(`${API_URL}/api/research/reports/${id}/pdf`, {
    headers: { "X-API-Key": getApiKey() },
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, `HTTP ${res.status}`);
  return res.blob();
};
export const fetchSession = (id: string) => research<SessionStatus>(`/sessions/${id}`);
export const listSessions = (onlyOpen = false) =>
  research<SessionRow[]>(`/sessions${onlyOpen ? "?only_open=true" : ""}`);
export const fetchResearchReadiness = (id: string) =>
  research<Readiness>(`/sessions/${id}/readiness`);
export const fetchMissing = (id: string) => research<MissingAssessment>(`/sessions/${id}/missing`);
export const resumeResearch = (id: string) =>
  research<SessionStatus>(`/sessions/${id}/resume`, { method: "POST" });
export const continueWithAvailable = (id: string) =>
  research<SessionStatus>(`/sessions/${id}/continue`, { method: "POST" });
export const fetchUploads = (id: string) => research<Upload[]>(`/sessions/${id}/uploads`);
export const uploadDocuments = (id: string, files: File[], licence: string) => {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("licence", licence);
  return research<{ uploads: Upload[] }>(`/sessions/${id}/uploads`, {
    method: "POST",
    body: form,
  });
};
export const fetchUpload = (id: string) => research<Upload>(`/uploads/${id}`);
export const clarifyUpload = (id: string, answers: Record<string, string | boolean>) =>
  research<Upload>(`/uploads/${id}/clarify`, { method: "POST", body: JSON.stringify(answers) });
export const generateReport = (id: string, mode: Report["mode"]) =>
  research<Report>(`/sessions/${id}/reports`, { method: "POST", body: JSON.stringify({ mode }) });
export const fetchReports = (id: string) => research<ReportRow[]>(`/sessions/${id}/reports`);
export const fetchReport = (id: string) => research<Report>(`/reports/${id}`);

export type OverviewRow = {
  company_id: string;
  name: string;
  symbol: string | null;
  profile: string;
  score: number;
  eligibility: "AUTOMATIC" | "ASSISTED" | "BLOCKED";
  confidence: string;
  critical_missing: { id: string; label: string; action: string; reason: string | null }[];
  important_missing: number;
  documents_needed: string[];
  cannot_be_settled_by_upload: string[];
};
export type Overview = {
  companies: OverviewRow[];
  automatic: number;
  assisted: number;
  blocked: number;
};
export const fetchOverview = () => research<Overview>("/overview");
