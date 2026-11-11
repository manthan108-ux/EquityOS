"use client";

import { useEffect, useId, useState, type KeyboardEvent } from "react";

import { Badge, ButtonLink, Card, Spinner, inputClass } from "@/components/ui";
import {
  searchCompanies,
  type CompanyMatch,
  type CompanySummary,
  type SearchResponse,
} from "@/services/api";
import { titleCase } from "@/lib/format";

const DEBOUNCE_MS = 250;

type View =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "result"; response: SearchResponse };

const EXACT: CompanyMatch["match_type"][] = ["ISIN", "BSE_CODE", "TICKER", "NAME", "ALIAS"];
const MATCH_LABEL: Record<CompanyMatch["match_type"], string> = {
  ISIN: "ISIN match",
  BSE_CODE: "BSE code match",
  TICKER: "Ticker match",
  NAME: "Name match",
  ALIAS: "Alias match",
  FUZZY: "Similar match",
};

const nse = (c: CompanySummary) => c.listings.find((l) => l.exchange === "NSE");
const bse = (c: CompanySummary) => c.listings.find((l) => l.exchange === "BSE");

export function CompanyIdentity({ company }: { company: CompanySummary }) {
  const isins = [...new Set(company.listings.map((l) => l.isin))].join(", ");
  const rows: [string, string][] = [
    ["NSE symbol", nse(company)?.symbol ?? "Not listed"],
    ["BSE code", bse(company)?.security_code ?? "Not listed"],
    ["ISIN", isins || "Unavailable"],
    ["Sector", titleCase(company.sector) || "Not classified"],
    ["Industry", titleCase(company.industry) || "Not classified"],
  ];
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt className="text-xs font-medium tracking-wide text-muted uppercase">{k}</dt>
          <dd className="tnum mt-0.5 text-sm font-medium text-ink">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Search by name, NSE ticker, BSE code or ISIN. Nothing is opened until the user confirms the company. */
export default function CompanySearch({ autoFocus = false }: { autoFocus?: boolean }) {
  const listId = useId();
  const [query, setQuery] = useState("");
  const [view, setView] = useState<View>({ kind: "idle" });
  const [selected, setSelected] = useState<CompanySummary | null>(null);
  const [active, setActive] = useState(0);

  const trimmed = query.trim();

  useEffect(() => {
    const q = trimmed;
    if (!q) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setView({ kind: "loading" });
      searchCompanies(q, controller.signal)
        .then((response) => {
          setView({ kind: "result", response });
          setActive(0);
          // One exact match is shown for confirmation straight away; it is still not opened.
          setSelected(response.status === "RESOLVED" ? response.matches[0].company : null);
        })
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          setView({ kind: "error", message: err instanceof Error ? err.message : "Search failed" });
        });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [trimmed]);

  const current: View = trimmed ? view : { kind: "idle" };
  const response = current.kind === "result" ? current.response : null;
  const matches = response?.matches ?? [];
  const showList = !!response && (response.status === "AMBIGUOUS" || response.status === "SUGGESTIONS");

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!showList || !matches.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (i + 1) % matches.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (i - 1 + matches.length) % matches.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      setSelected(matches[active].company); // choosing is explicit: the highlighted row, on Enter
    }
  };

  return (
    <div className="w-full">
      <label htmlFor="company-search" className="sr-only">
        Search company by name, ticker, BSE code or ISIN
      </label>
      <div className="relative">
        <svg aria-hidden viewBox="0 0 24 24" className="pointer-events-none absolute top-1/2 left-3.5 h-5 w-5 -translate-y-1/2 text-faint" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" />
        </svg>
        <input
          id="company-search"
          type="search"
          role="combobox"
          aria-expanded={showList}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={showList ? `${listId}-${active}` : undefined}
          autoComplete="off"
          spellCheck={false}
          autoFocus={autoFocus}
          maxLength={100}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setSelected(null);
          }}
          onKeyDown={onKey}
          placeholder="Company name, NSE ticker, BSE code or ISIN, for example TCS, INFY, 500325"
          className={`${inputClass} h-12 pl-11 text-[15px] shadow-[var(--shadow-card)]`}
        />
        {current.kind === "loading" && <Spinner className="absolute top-1/2 right-3.5 -translate-y-1/2 text-brand" />}
      </div>

      <div aria-live="polite" className="mt-3">
        {current.kind === "error" && (
          <p role="alert" className="text-sm text-bad">
            {current.message}. Is the backend running?
          </p>
        )}
        {response?.status === "NOT_FOUND" && (
          <p className="text-sm text-muted">
            No NSE or BSE company matches “{response.query}”.
            {response.notes.map((n) => (
              <span key={n} className="block text-warn">{n}</span>
            ))}
          </p>
        )}
        {showList && (
          <div>
            <p className="mb-2 text-[13px] text-muted">
              {response.status === "AMBIGUOUS"
                ? "Several companies match. Choose the correct one to continue."
                : "No exact match. Did you mean one of these? Similar matches are suggestions only."}
            </p>
            <ul id={listId} role="listbox" aria-label="Matching companies" className="divide-y divide-line overflow-hidden rounded-[12px] border border-line bg-surface shadow-[var(--shadow-card)]">
              {matches.map((m, i) => {
                const exact = EXACT.includes(m.match_type);
                return (
                  <li key={m.company.id} role="option" id={`${listId}-${i}`} aria-selected={active === i}>
                    <button
                      type="button"
                      onClick={() => {
                        setActive(i);
                        setSelected(m.company);
                      }}
                      className={`flex w-full items-center justify-between gap-4 px-4 py-3 text-left hover:bg-surface-2 ${active === i ? "bg-brand-soft/60" : ""}`}
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold text-ink">{m.company.common_name}</span>
                        <span className="tnum block truncate text-xs text-muted">
                          {[nse(m.company)?.symbol && `NSE ${nse(m.company)?.symbol}`, bse(m.company)?.security_code && `BSE ${bse(m.company)?.security_code}`, titleCase(m.company.sector), titleCase(m.company.industry)].filter(Boolean).join(" · ")}
                        </span>
                      </span>
                      <Badge tone={exact ? "ok" : "neutral"}>{exact ? MATCH_LABEL[m.match_type] : "Similar"}</Badge>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

      {selected && (
        <Card className="mt-4 p-5" aria-label="Confirm company">
          <p className="text-xs font-semibold tracking-wider text-brand uppercase">Confirm company</p>
          <h2 className="mt-1 text-lg font-semibold text-ink">{selected.legal_name}</h2>
          <div className="mt-4">
            <CompanyIdentity company={selected} />
          </div>
          <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <ButtonLink href={`/companies/${selected.id}`} variant="primary">
              Open research workspace
            </ButtonLink>
            <span className="text-xs text-muted">Check that the symbol and ISIN are the company you mean.</span>
          </div>
        </Card>
      )}
    </div>
  );
}
