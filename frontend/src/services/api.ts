// Thin client for the EquityOS backend. All network access goes through here.

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type LiveHealth = { status: string; service: string; version: string };
export type ReadyHealth = { status: string; database: string; detail: string | null };

export type Listing = {
  exchange: string;
  symbol: string;
  security_code: string | null;
  isin: string;
  status: string;
};
export type CompanySummary = {
  id: string;
  legal_name: string;
  common_name: string;
  sector: string | null;
  industry: string | null;
  listings: Listing[];
};
export type MatchType = "ISIN" | "BSE_CODE" | "TICKER" | "NAME" | "ALIAS" | "FUZZY";
export type ResolutionStatus = "RESOLVED" | "AMBIGUOUS" | "SUGGESTIONS" | "NOT_FOUND";
export type CompanyMatch = {
  company: CompanySummary;
  match_type: MatchType;
  score: number;
  matched_on: string;
};
export type SearchResponse = {
  query: string;
  status: ResolutionStatus;
  matches: CompanyMatch[];
  notes: string[];
};

async function getJson<T>(
  path: string,
  acceptStatuses: number[] = [],
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store", signal });
  if (!res.ok && !acceptStatuses.includes(res.status)) {
    throw new Error(`${path} returned HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

export const fetchLiveness = () => getJson<LiveHealth>("/health");
// /health/ready answers 503 with a JSON body when the database is down; that body is useful.
export const fetchReadiness = () => getJson<ReadyHealth>("/health/ready", [503]);

export const searchCompanies = (q: string, signal?: AbortSignal) =>
  getJson<SearchResponse>(`/api/companies/search?q=${encodeURIComponent(q)}`, [], signal);

// ---- scoring (Phase 10) ----------------------------------------------------------------------
export type Methodology = "RELATIVE" | "ABSOLUTE" | "HYBRID" | "UNAVAILABLE";
export type MethodChoice = "AUTO" | "RELATIVE" | "ABSOLUTE" | "HYBRID";

export type ScoreResponse = {
  run_id: number;
  company: string;
  ticker: string | null;
  sector: string | null;
  industry: string | null;
  methodology: Methodology;
  requested: string;
  label: string;
  overall: string | null;
  unavailable_reason: string | null;
  profile: string | null;
  pillar_scores: Record<string, string> | null;
  weight_coverage: string | null;
  confidence: string | null;
  peer_count: number;
  eligible_peer_count: number;
  benchmark_version: string | null;
  config_version: string | null;
  as_of: string;
  calculated_at: string;
  explanation: {
    improved?: string[];
    reduced?: string[];
    missing?: { metric: string; pillar: string; reason: string | null }[];
    assumptions?: string[];
    pillar_coverage?: Record<string, number>;
    eligible_peers?: string[];
    peer_context?: string;
    restructuring?: string;
    computable_coverage_pct?: number;
  } | null;
};

export type MetricScore = {
  metric: string;
  name: string;
  pillar: string;
  status: "SCORED" | "GAP" | "EXCLUDED";
  reason: string | null;
  actual_value: string | null;
  unit: string | null;
  score: string | null;
  scoring_kind: string | null;
  benchmark: {
    type?: string;
    scope?: string;
    confidence?: string;
    observations?: number;
    companies?: number;
    p25?: number;
    median?: number;
    p75?: number;
    p90?: number;
    percentile?: number;
    why?: string;
  } | null;
  period_end: string | null;
  basis: string | null;
  evidence_id: number | null;
};

export type ScoreHistoryRow = {
  run_id: number;
  methodology: Methodology;
  requested: string;
  overall: string | null;
  calculated_at: string;
};

export const fetchScore = (id: string, method: MethodChoice = "AUTO") =>
  getJson<ScoreResponse>(`/api/companies/${id}/score?method=${method}`, [404]);
export const fetchMetricScores = (id: string, method: MethodChoice = "AUTO") =>
  getJson<MetricScore[]>(`/api/companies/${id}/score/metrics?method=${method}`, [404]);
export const fetchScoreHistory = (id: string) =>
  getJson<ScoreHistoryRow[]>(`/api/companies/${id}/score/history`);
