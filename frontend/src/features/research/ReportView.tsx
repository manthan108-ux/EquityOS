"use client";

import type { Report, ReportGap, ReportItem, ReportSection } from "@/services/research";

const STATUS: Record<string, string> = {
  COMPLETE: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  PARTIAL: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  UNAVAILABLE: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  DERIVED: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  NOT_REPORTED: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};

function show(item: ReportItem): string {
  if (item.value === null) return "—";
  const n = Number(item.value);
  const text = Number.isFinite(n)
    ? Math.abs(n) >= 1e7
      ? `${(n / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 2 })} cr`
      : n.toLocaleString("en-IN", { maximumFractionDigits: 4 })
    : item.value;
  return item.unit === "percent" ? `${text}%` : text;
}

function Items({ items }: { items: ReportItem[] }) {
  if (!items.length) return null;
  return (
    <ul className="mt-2 divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
      {items.map((i) => (
        <li key={i.requirement} className="flex flex-wrap items-baseline justify-between gap-2 py-1.5">
          <span>{i.label}</span>
          <span className="text-right">
            <span className="font-medium">{show(i)}</span>
            <span className="ml-2 text-xs text-zinc-500">
              {i.period_end ?? ""} {i.basis?.toLowerCase() ?? ""}
              {i.source.page ? ` · p.${i.source.page}` : ""}
              {i.source.evidence_id ? ` · evidence #${i.source.evidence_id}` : ""}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function Gaps({ gaps }: { gaps: ReportGap[] }) {
  if (!gaps.length) return null;
  return (
    <ul className="mt-2 space-y-1 text-sm">
      {gaps.map((g) => (
        <li
          key={g.requirement}
          className="rounded-md border border-dashed border-amber-400 bg-amber-50 px-3 py-1.5 dark:border-amber-700 dark:bg-amber-950/40"
        >
          <span className="font-medium">{g.label}</span>{" "}
          <span className="text-xs uppercase text-amber-800 dark:text-amber-300">
            {g.importance} · not available
          </span>
          {g.reason && <div className="text-xs text-zinc-600 dark:text-zinc-400">{g.reason}</div>}
        </li>
      ))}
    </ul>
  );
}

function Section({ s }: { s: ReportSection }) {
  return (
    <section className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <div className="flex items-center justify-between">
        <h4 className="font-semibold">{s.title}</h4>
        <span className={`rounded-full px-2 py-0.5 text-xs ${STATUS[s.status] ?? STATUS.DERIVED}`}>
          {s.status.toLowerCase().replace("_", " ")}
        </span>
      </div>
      <Items items={s.items} />
      {s.series && (
        <div className="mt-3 space-y-2 text-sm">
          {Object.entries(s.series)
            .filter(([, rows]) => rows.length)
            .map(([name, rows]) => (
              <div key={name}>
                <span className="text-xs uppercase text-zinc-500">{name.replace("_", " ")}</span>
                <div className="flex flex-wrap gap-x-4">
                  {rows.map((r) => (
                    <span key={r.fiscal_year}>
                      {r.fiscal_year}:{" "}
                      {(Number(r.value) / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 0 })} cr
                    </span>
                  ))}
                </div>
              </div>
            ))}
        </div>
      )}
      {s.segments && s.segments.length > 0 && (
        <ul className="mt-2 text-sm">
          {s.segments.map((g, i) => (
            <li key={i} className="flex justify-between">
              <span>
                {g.segment ?? "Total"} · {g.metric.replace("SEGMENT.", "").replace(/_/g, " ")}
              </span>
              <span>{(Number(g.value) / 1e7).toLocaleString("en-IN", { maximumFractionDigits: 0 })} cr</span>
            </li>
          ))}
        </ul>
      )}
      {s.peers && (
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          {s.peers.available
            ? `${s.peers.peer_count} peer(s)${s.peers.thin ? " (thin group: fewer than the minimum for a reliable comparison)" : ""}: ${(s.peers.peers ?? []).map((p) => p.name).join(", ")}`
            : s.peers.reason}
        </p>
      )}
      {s.score && (
        <p className="mt-2 text-sm">
          {s.score.available
            ? `${s.score.methodology} score ${s.score.overall} (confidence ${s.score.confidence ?? "n/a"})`
            : `Score unavailable: ${s.score.reason ?? "not calculated"}`}
        </p>
      )}
      {[s.management_discussion, s.auditor_report].map(
        (n, i) =>
          n && (
            <blockquote
              key={i}
              className="mt-2 border-l-2 border-zinc-300 pl-3 text-xs text-zinc-600 dark:border-zinc-700 dark:text-zinc-400"
            >
              <strong>{n.heading}</strong> (p.{n.source.page ?? "?"}, quoted, not summarised)
              <br />
              {n.excerpt?.slice(0, 500)}
            </blockquote>
          ),
      )}
      <Gaps gaps={s.gaps} />
    </section>
  );
}

export default function ReportView({ report }: { report: Report }) {
  const c = report.content;
  const lim = c.limitations;
  return (
    <div className="space-y-4">
      <div
        className={`rounded-lg border p-4 text-sm ${
          c.meta.complete
            ? "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/40"
            : "border-amber-400 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/40"
        }`}
      >
        <p className="font-semibold">
          {c.meta.company} — {report.mode === "COMPLETE" ? "Complete research report" : "Available-evidence report"}{" "}
          v{report.version}
        </p>
        <p className="mt-1">{c.meta.banner}</p>
        <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
          Research readiness {c.readiness.score} ({c.readiness.eligibility.toLowerCase()}, confidence{" "}
          {c.readiness.confidence.toLowerCase()}). {c.readiness.note}. As of {c.meta.as_of}.
        </p>
      </div>
      {c.sections.map((s) => (
        <Section key={s.key} s={s} />
      ))}
      <section className="rounded-lg border border-zinc-300 p-4 dark:border-zinc-700">
        <h4 className="font-semibold">Limitations</h4>
        <dl className="mt-2 space-y-2 text-sm">
          {(
            [
              ["Critical items not available", lim.missing_critical],
              ["Important items not available", lim.missing_important],
              ["Optional items not available", lim.missing_optional],
              ["Calculations that could not be made", lim.unavailable_calculations],
              ["Incomplete valuation components", lim.incomplete_valuation],
            ] as [string, ReportGap[]][]
          )
            .filter(([, rows]) => rows.length)
            .map(([title, rows]) => (
              <div key={title}>
                <dt className="font-medium">{title}</dt>
                <dd className="text-zinc-600 dark:text-zinc-400">{rows.map((g) => g.label).join(", ")}</dd>
              </div>
            ))}
          {lim.unavailable_scores && (
            <div>
              <dt className="font-medium">Score</dt>
              <dd className="text-zinc-600 dark:text-zinc-400">{lim.unavailable_scores}</dd>
            </div>
          )}
          {lim.limits_on_conclusions.length > 0 && (
            <div>
              <dt className="font-medium">Limits on conclusions</dt>
              <dd>
                <ul className="list-disc pl-5 text-zinc-600 dark:text-zinc-400">
                  {lim.limits_on_conclusions.map((t) => (
                    <li key={t}>{t}</li>
                  ))}
                </ul>
              </dd>
            </div>
          )}
        </dl>
      </section>
    </div>
  );
}
