"use client";

import { useEffect, useState } from "react";

import {
  searchCompanies,
  type CompanyMatch,
  type CompanySummary,
  type SearchResponse,
} from "@/services/api";
import CompanyConfirmation from "./CompanyConfirmation";

const DEBOUNCE_MS = 250;

type View =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "result"; response: SearchResponse };

const MATCH_LABEL: Record<CompanyMatch["match_type"], string> = {
  ISIN: "ISIN match",
  BSE_CODE: "BSE code match",
  TICKER: "Ticker match",
  NAME: "Name match",
  ALIAS: "Alias match",
  FUZZY: "Similar",
};

function listingText(company: CompanySummary): string {
  return company.listings.map((l) => `${l.exchange}: ${l.symbol}`).join("  ·  ");
}

export default function CompanySearch() {
  const [query, setQuery] = useState("");
  const [view, setView] = useState<View>({ kind: "idle" });
  const [selected, setSelected] = useState<CompanySummary | null>(null);

  const trimmed = query.trim();

  useEffect(() => {
    const q = trimmed;
    if (!q) return; // nothing to search; the empty state is derived during render below
    // Debounce typing, and abort the previous request so a slow old response can't overwrite a newer one.
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setView({ kind: "loading" });
      searchCompanies(q, controller.signal)
        .then((response) => {
          setView({ kind: "result", response });
          // A single exact match is shown for confirmation straight away.
          setSelected(response.status === "RESOLVED" ? response.matches[0].company : null);
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          setView({
            kind: "error",
            message: err instanceof Error ? err.message : "Search failed",
          });
        });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [trimmed]);

  // With an empty box, ignore whatever the last search left behind.
  const current: View = trimmed ? view : { kind: "idle" };
  const response = current.kind === "result" ? current.response : null;
  const showList =
    response && (response.status === "AMBIGUOUS" || response.status === "SUGGESTIONS");

  return (
    <div className="w-full max-w-xl">
      <label htmlFor="company-search" className="sr-only">
        Search company by name, ticker, BSE code or ISIN
      </label>
      <input
        id="company-search"
        type="search"
        autoComplete="off"
        spellCheck={false}
        maxLength={100}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setSelected(null);
        }}
        placeholder="Company name, ticker, BSE code or ISIN — e.g. RELIANCE, TCS, INFY"
        className="w-full rounded-xl border border-zinc-300 bg-white px-4 py-3 text-base shadow-sm outline-none transition focus:border-zinc-900 focus:ring-2 focus:ring-zinc-900/10 dark:border-zinc-700 dark:bg-zinc-900 dark:focus:border-zinc-300"
      />

      <div aria-live="polite" className="mt-3">
        {current.kind === "loading" && <p className="text-sm text-zinc-500">Searching…</p>}
        {current.kind === "error" && (
          <p role="alert" className="text-sm text-red-600">
            {current.message}. Is the backend running?
          </p>
        )}
        {response?.status === "NOT_FOUND" && (
          <p className="text-sm text-zinc-500">
            No NSE/BSE company matches “{response.query}”.
            {response.notes.map((n) => (
              <span key={n} className="block text-amber-600">
                {n}
              </span>
            ))}
          </p>
        )}
        {showList && (
          <div>
            <p className="mb-2 text-sm text-zinc-500">
              {response.status === "AMBIGUOUS"
                ? "Several companies match. Please choose the correct one:"
                : "No exact match. Did you mean:"}
            </p>
            <ul className="divide-y divide-zinc-100 overflow-hidden rounded-xl border border-zinc-200 bg-white dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900">
              {response.matches.map((m) => (
                <li key={m.company.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(m.company)}
                    className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left hover:bg-zinc-50 dark:hover:bg-zinc-800"
                  >
                    <span>
                      <span className="block font-medium">{m.company.common_name}</span>
                      <span className="block text-xs text-zinc-500">{listingText(m.company)}</span>
                    </span>
                    <span className="shrink-0 text-xs text-zinc-500">{MATCH_LABEL[m.match_type]}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {selected && <CompanyConfirmation company={selected} />}
    </div>
  );
}
