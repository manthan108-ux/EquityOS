"use client";

import { useState } from "react";

import type { PreferenceLogEntry, Preferences } from "@/services/research";

const MODES: { value: Preferences["period_mode"]; label: string }[] = [
  { value: "LATEST_QUARTER", label: "Latest available quarter (default)" },
  { value: "LATEST_ANNUAL", label: "Latest available financial year" },
  { value: "QUARTER", label: "A specific quarter" },
  { value: "FISCAL_YEAR", label: "A specific financial year" },
];

/** Why a draft cannot be applied, or null when it can. Mirrors the server's own rules. */
export function preferenceProblem(p: Preferences): string | null {
  if (p.period_mode === "FISCAL_YEAR" || p.period_mode === "QUARTER") {
    if (!p.fiscal_year || p.fiscal_year < 2000 || p.fiscal_year > 2100) {
      return "Enter the financial year, for example 2027 for the year ended 31 March 2027.";
    }
  }
  if (p.period_mode === "QUARTER" && !p.quarter) {
    return "Choose the quarter of that financial year (Q1 = Apr-Jun ... Q4 = Jan-Mar).";
  }
  return null;
}

const same = (a: Preferences, b: Preferences) =>
  a.basis === b.basis &&
  a.period_mode === b.period_mode &&
  (a.fiscal_year ?? null) === (b.fiscal_year ?? null) &&
  (a.quarter ?? null) === (b.quarter ?? null) &&
  a.include_optional === b.include_optional;

export default function PreferencesPanel({
  value,
  onApply,
  busy = false,
  applyLabel = "Apply preferences",
  log = [],
}: {
  value: Preferences;
  onApply: (p: Preferences) => void;
  busy?: boolean;
  applyLabel?: string;
  log?: PreferenceLogEntry[];
}) {
  const [draft, setDraft] = useState<Preferences>(value);
  const problem = preferenceProblem(draft);
  const chosen = draft.period_mode === "FISCAL_YEAR" || draft.period_mode === "QUARTER";

  const set = (patch: Partial<Preferences>) => setDraft((d) => ({ ...d, ...patch }));
  const setMode = (mode: Preferences["period_mode"]) =>
    set({
      period_mode: mode,
      fiscal_year: mode === "FISCAL_YEAR" || mode === "QUARTER" ? (draft.fiscal_year ?? null) : null,
      quarter: mode === "QUARTER" ? (draft.quarter ?? null) : null,
    });

  return (
    <section
      aria-label="Research preferences"
      className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
    >
      <h3 className="font-semibold">Research preferences</h3>
      <p className="mt-1 text-xs text-zinc-500">
        The basis is never switched for you: if a figure is not available on the basis you choose, it
        is reported as missing, with the reason, and you decide.
      </p>
      <div className="mt-3 grid gap-4 text-sm sm:grid-cols-2">
        <fieldset>
          <legend className="text-xs uppercase tracking-wide text-zinc-500">Accounting basis</legend>
          {(["CONSOLIDATED", "STANDALONE"] as const).map((b) => (
            <label key={b} className="mr-4 inline-flex items-center gap-1.5">
              <input
                type="radio"
                name="basis"
                checked={draft.basis === b}
                onChange={() => set({ basis: b })}
              />
              {b === "CONSOLIDATED" ? "Consolidated (default)" : "Standalone"}
            </label>
          ))}
        </fieldset>
        <label>
          <span className="block text-xs uppercase tracking-wide text-zinc-500">Reporting period</span>
          <select
            value={draft.period_mode}
            onChange={(e) => setMode(e.target.value as Preferences["period_mode"])}
            className="mt-1 w-full rounded border px-2 py-1 dark:bg-zinc-900"
          >
            {MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        {chosen && (
          <label>
            <span className="block text-xs uppercase tracking-wide text-zinc-500">Financial year</span>
            <input
              type="number"
              min={2000}
              max={2100}
              value={draft.fiscal_year ?? ""}
              onChange={(e) => set({ fiscal_year: e.target.value ? Number(e.target.value) : null })}
              className="mt-1 w-32 rounded border px-2 py-1 dark:bg-zinc-900"
            />
          </label>
        )}
        {draft.period_mode === "QUARTER" && (
          <label>
            <span className="block text-xs uppercase tracking-wide text-zinc-500">Quarter</span>
            <select
              value={draft.quarter ?? ""}
              onChange={(e) => set({ quarter: (e.target.value || null) as Preferences["quarter"] })}
              className="mt-1 w-32 rounded border px-2 py-1 dark:bg-zinc-900"
            >
              <option value="">choose…</option>
              {["Q1", "Q2", "Q3", "Q4"].map((q) => (
                <option key={q} value={q}>
                  {q}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="inline-flex items-center gap-1.5 sm:col-span-2">
          <input
            type="checkbox"
            checked={draft.include_optional}
            onChange={(e) => set({ include_optional: e.target.checked })}
          />
          Also request optional items (off by default; they never block research)
        </label>
      </div>
      {problem && (
        <p role="alert" className="mt-2 text-sm text-red-600">
          {problem}
        </p>
      )}
      <button
        type="button"
        disabled={busy || !!problem || same(draft, value)}
        onClick={() => onApply(draft)}
        className="mt-3 rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
      >
        {applyLabel}
      </button>
      {log.length > 1 && (
        <details className="mt-3 text-xs text-zinc-600 dark:text-zinc-400">
          <summary className="cursor-pointer">Earlier choices in this session ({log.length - 1})</summary>
          <ul className="mt-1 space-y-0.5">
            {log.map((e, i) => (
              <li key={i}>
                {e.at.slice(0, 16).replace("T", " ")} · {e.to.basis?.toLowerCase()} ·{" "}
                {e.to.period_mode?.toLowerCase().replace("_", " ")}
                {e.to.fiscal_year ? ` FY${e.to.fiscal_year}` : ""}
                {e.to.quarter ? ` ${e.to.quarter}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
