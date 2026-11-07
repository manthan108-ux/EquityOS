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
