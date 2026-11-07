// Thin client for the EquityOS backend. All network access goes through here.

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type LiveHealth = { status: string; service: string; version: string };
export type ReadyHealth = { status: string; database: string; detail: string | null };

async function getJson<T>(path: string, acceptStatuses: number[] = []): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  if (!res.ok && !acceptStatuses.includes(res.status)) {
    throw new Error(`${path} returned HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

export const fetchLiveness = () => getJson<LiveHealth>("/health");
// /health/ready answers 503 with a JSON body when the database is down; that body is useful.
export const fetchReadiness = () => getJson<ReadyHealth>("/health/ready", [503]);
