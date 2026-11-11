"use client";

import { useMemo, useState } from "react";

import { HBarChart } from "@/components/charts";
import { Badge, Card, CardHeader, EmptyState, Notice, type Tone } from "@/components/ui";
import { fmtValue, titleCase } from "@/lib/format";
import type { PeerRow, Report } from "@/services/research";

const POSITION: Record<string, { label: string; tone: Tone }> = {
  better: { label: "Better than median", tone: "ok" },
  worse: { label: "Worse than median", tone: "bad" },
  "in line": { label: "In line", tone: "neutral" },
  "above median": { label: "Above median", tone: "neutral" },
  "below median": { label: "Below median", tone: "neutral" },
  "at median": { label: "At median", tone: "neutral" },
};

/** The best comparable value in a row (only when the metric has a direction). */
function best(r: PeerRow): string | null {
  if (!r.better) return null;
  const all = [{ name: "This company", v: Number(r.company.value) }, ...r.peers.filter((p) => p.comparable).map((p) => ({ name: p.name, v: Number(p.value) }))];
  const sorted = all.sort((a, b) => (r.better === "higher" ? b.v - a.v : a.v - b.v));
  return sorted[0].name;
}

export default function PeersTab({ report, company }: { report: Report | null; company: string }) {
  const a = report?.content.analysis;
  const [group, setGroup] = useState("All");
  const pa = a?.available ? a.peers : undefined;
  const groups = useMemo(() => ["All", ...new Set((pa?.rows ?? []).map((r) => r.group))], [pa]);

  if (!pa) {
    return (
      <EmptyState title="Peer comparison needs a report">
        {report ? "This report version has no peer analysis. Generate a new version." : "Peer metrics are compared in the report. Generate one from the Reports tab."}
      </EmptyState>
    );
  }
  if (!pa.peers.length) {
    return <EmptyState title="No peer group for this company">No same-industry peers with enough data were found, so nothing is compared. Ineligible companies are not shown as peers.</EmptyState>;
  }
  const names = pa.peers.map((p) => p.name);
  const rows = pa.rows.filter((r) => group === "All" || r.group === group);
  const charts = pa.rows.filter((r) => r.rank && r.n_comparable >= 3).slice(0, 4);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader title="Peer group" subtitle="Same-industry companies closest in size. Only comparable figures are ranked." />
        <ul className="grid gap-px bg-line sm:grid-cols-2 lg:grid-cols-4">
          {pa.peers.map((p) => (
            <li key={p.name} className="bg-surface px-5 py-3"><p className="text-sm font-semibold text-ink">{p.name}</p><p className="text-xs text-muted">{p.reason}</p></li>
          ))}
        </ul>
        <div className="grid gap-4 border-t border-line p-5 sm:grid-cols-3">
          <div><p className="text-xs font-medium tracking-wide text-muted uppercase">Ranked metrics</p><p className="tnum text-xl font-semibold text-ink">{pa.summary.ranked_metrics}</p></div>
          <div><p className="text-xs font-medium tracking-wide text-ok uppercase">Better than median</p><p className="mt-0.5 text-[13px] text-ink">{pa.summary.better.join(", ") || "none"}</p></div>
          <div><p className="text-xs font-medium tracking-wide text-bad uppercase">Worse than median</p><p className="mt-0.5 text-[13px] text-ink">{pa.summary.worse.join(", ") || "none"}</p></div>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Comparison"
          subtitle={`${company} against its peers, same period, basis and unit.`}
          actions={
            <div role="group" aria-label="Metric group" className="flex flex-wrap gap-1">
              {groups.map((g) => (
                <button key={g} type="button" aria-pressed={group === g} onClick={() => setGroup(g)} className={`rounded-full border px-3 py-1 text-xs font-medium ${group === g ? "border-brand bg-brand-soft text-brand" : "border-line-strong text-muted hover:bg-surface-2"}`}>{g}</button>
              ))}
            </div>
          }
        />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-[13px]">
            <thead className="bg-surface-2 text-left text-xs text-muted">
              <tr>
                <th className="sticky left-0 bg-surface-2 px-4 py-2 font-medium">Metric</th>
                <th className="px-3 py-2 text-right font-medium">{company}</th>
                {names.map((n) => <th key={n} className="px-3 py-2 text-right font-medium">{n}</th>)}
                <th className="px-3 py-2 text-right font-medium">Peer median</th>
                <th className="px-3 py-2 font-medium">Position</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((r) => {
                const bestName = best(r);
                const by = Object.fromEntries(r.peers.map((p) => [p.name, p]));
                const pos = r.position ? POSITION[r.position] : null;
                return (
                  <tr key={r.key} className="hover:bg-surface-2/60">
                    <th scope="row" className="sticky left-0 bg-surface px-4 py-2.5 text-left font-medium text-ink">
                      {r.label}
                      <span className="block text-[11px] font-normal text-faint">{r.company.period_end} · {(r.basis ?? "").toLowerCase()}{r.basis_note ? " · other basis" : ""}</span>
                    </th>
                    <td className={`tnum px-3 py-2.5 text-right font-semibold ${bestName === "This company" ? "text-ok" : "text-ink"}`}>{fmtValue(r.company.value, r.company.unit)}</td>
                    {names.map((n) => {
                      const p = by[n];
                      if (p?.comparable) return <td key={n} className={`tnum px-3 py-2.5 text-right ${bestName === n ? "font-semibold text-ok" : "text-ink"}`}>{fmtValue(p.value, r.company.unit)}</td>;
                      if (p?.value) return <td key={n} className="tnum px-3 py-2.5 text-right text-faint" title={`Not compared: ${p.reason}`}>{fmtValue(p.value, r.company.unit)}*</td>;
                      return <td key={n} className="px-3 py-2.5 text-right text-faint" title="Not available">–</td>;
                    })}
                    <td className="tnum px-3 py-2.5 text-right text-muted">{r.median ? fmtValue(r.median, r.company.unit) : "–"}</td>
                    <td className="px-3 py-2.5">
                      {pos ? <Badge tone={pos.tone}>{pos.label}{r.rank ? ` · ${r.rank}/${r.of}` : ""}</Badge> : <span className="text-xs text-muted" title={r.note}>not ranked</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="border-t border-line px-5 py-3 text-xs text-muted">
          Green marks the best comparable value where a direction exists. Grey values with * are a different period, basis or unit: shown for reference, never ranked. “–” means no value is stored. {pa.rules}.
        </p>
      </Card>

      {charts.length > 0 && (
        <Card>
          <CardHeader title="Where the company stands" subtitle="Metrics with at least three comparable peers." />
          <div className="grid gap-8 p-5 lg:grid-cols-2">
            {charts.map((r) => (
              <HBarChart key={r.key} title={r.label} unit={`${r.company.unit === "percent" ? "percent" : r.company.unit ?? ""} · ${r.company.period_end}`}
                rows={[{ label: company, value: Number(r.company.value), highlight: true }, ...r.peers.filter((p) => p.comparable).map((p) => ({ label: p.name, value: Number(p.value) }))].sort((x, y) => y.value - x.value)} />
            ))}
          </div>
        </Card>
      )}

      {pa.unavailable_metrics.length > 0 && <Notice tone="warn" title="No company value stored">{pa.unavailable_metrics.map(titleCase).join(", ")}.</Notice>}
      {pa.not_available && <Notice tone="neutral">{pa.not_available}.</Notice>}
    </div>
  );
}
