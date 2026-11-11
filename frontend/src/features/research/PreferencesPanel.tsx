"use client";

import { useState } from "react";

import { Button, Card, CardHeader, Field, inputClass } from "@/components/ui";
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
    <Card aria-label="Research preferences">
      <CardHeader
        title="Research preferences"
        subtitle="The basis is never switched for you: if a figure is not available on the basis you choose, it is reported as missing, with the reason, and you decide."
      />
      <div className="grid gap-5 p-5 text-sm sm:grid-cols-2">
        <fieldset>
          <legend className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Accounting basis</legend>
          <div className="flex gap-2">
            {(["CONSOLIDATED", "STANDALONE"] as const).map((b) => (
              <label
                key={b}
                className={`flex cursor-pointer items-center gap-2 rounded-[8px] border px-3 py-2 ${draft.basis === b ? "border-brand bg-brand-soft text-brand" : "border-line-strong text-ink hover:bg-surface-2"}`}
              >
                <input type="radio" name="basis" className="accent-[var(--color-brand)]" checked={draft.basis === b} onChange={() => set({ basis: b })} />
                {b === "CONSOLIDATED" ? "Consolidated (default)" : "Standalone"}
              </label>
            ))}
          </div>
        </fieldset>
        <Field label="Reporting period">
          <select value={draft.period_mode} onChange={(e) => setMode(e.target.value as Preferences["period_mode"])} className={inputClass}>
            {MODES.map((m) => (
              <option key={m.value} value={m.value}>{m.label}</option>
            ))}
          </select>
        </Field>
        {chosen && (
          <Field label="Financial year">
            <input type="number" min={2000} max={2100} value={draft.fiscal_year ?? ""} onChange={(e) => set({ fiscal_year: e.target.value ? Number(e.target.value) : null })} className={`${inputClass} max-w-40`} />
          </Field>
        )}
        {draft.period_mode === "QUARTER" && (
          <Field label="Quarter">
            <select value={draft.quarter ?? ""} onChange={(e) => set({ quarter: (e.target.value || null) as Preferences["quarter"] })} className={`${inputClass} max-w-40`}>
              <option value="">choose…</option>
              {["Q1", "Q2", "Q3", "Q4"].map((q) => (
                <option key={q} value={q}>{q}</option>
              ))}
            </select>
          </Field>
        )}
        <label className="inline-flex items-center gap-2 sm:col-span-2">
          <input type="checkbox" className="accent-[var(--color-brand)]" checked={draft.include_optional} onChange={(e) => set({ include_optional: e.target.checked })} />
          Also request optional items (off by default; they never block research)
        </label>
      </div>
      {problem && <p role="alert" className="px-5 pb-2 text-sm text-bad">{problem}</p>}
      <div className="flex flex-wrap items-center gap-3 border-t border-line px-5 py-4">
        <Button variant="primary" loading={busy} disabled={!!problem || same(draft, value)} onClick={() => onApply(draft)}>
          {applyLabel}
        </Button>
        {same(draft, value) && !problem && <span className="text-xs text-muted">No changes to apply.</span>}
      </div>
      {log.length > 1 && (
        <details className="border-t border-line px-5 py-3 text-xs text-muted">
          <summary className="cursor-pointer font-medium">Earlier choices in this session ({log.length - 1})</summary>
          <ul className="mt-1 space-y-0.5">
            {log.map((e, i) => (
              <li key={i}>
                {e.at.slice(0, 16).replace("T", " ")} · {e.to.basis?.toLowerCase()} · {e.to.period_mode?.toLowerCase().replace("_", " ")}
                {e.to.fiscal_year ? ` FY${e.to.fiscal_year}` : ""}
                {e.to.quarter ? ` ${e.to.quarter}` : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Card>
  );
}
